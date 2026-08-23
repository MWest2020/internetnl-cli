"""FastAPI app: batch API v2 subset, provenance headers, error discipline.

Every reply — success and error — carries `X-Netnl-Instance` and
`X-Netnl-Notice`. Every error reply is v2-shaped:
`{"api_version", "error": {"label", "msg"}}`. Unmapped paths/methods answer
501 `not-implemented` rather than a framework-default page.
"""

from __future__ import annotations

import copy
import secrets
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Callable, Literal

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from internetnl_cli.client import Opener, is_valid_request_id, urllib_opener
from internetnl_cli.errors import ApiError, TransportError

from netnl import auth, limits, store, upstream
from netnl.errors import NetnlHTTPError
from netnl.replies import API_VERSION, NOTICE, error_body
from netnl.settings import Settings


class SubmitRequest(BaseModel):
    type: Literal["web", "mail"]
    domains: list[str] = Field(min_length=1)
    name: str | None = None


def _owned_request_or_404(conn, request_id: str, credential) -> sqlite3.Row:
    """A foreign or malformed id is indistinguishable from an unknown one —
    both are the same 404, so credential B can never tell credential A's
    request exists (design.md, "Tenant isolation").
    """
    if is_valid_request_id(request_id):
        row = store.get_request_for_credential(conn, request_id, credential["id"])
        if row is not None:
            return row
    raise NetnlHTTPError(404, "unknown-request", "this request_id does not exist for the user")

# Per-thread, reset at the start of every `_upstream()` call. Starlette runs
# a sync route handler and everything it calls (including the opener) on the
# *same* worker thread, so this is visible where it is read — inside that
# same handler, never across a request boundary — without the pitfalls of a
# contextvar (whose mutations inside `run_in_threadpool` do not propagate
# back to the coroutine that awaited it) or of a plain shared attribute
# (which a concurrent request on another thread could clobber).
_thread_state = threading.local()


def _status_tracking_opener(opener: Opener) -> Opener:
    def _wrapped(method, url, body, headers, timeout):
        response = opener(method, url, body, headers, timeout)
        _thread_state.last_status = response.status
        return response

    return _wrapped


# Round-1 fix (M5): known upstream statuses keep their existing label and
# message. Any other non-2xx status is passed through with its *real*
# status — not forced to 502 — under the generic "upstream-error" label, so
# e.g. an upstream 503 reaches the tenant as 503, not a misleading 502.
_KNOWN_UPSTREAM_LABELS = {
    400: ("bad-request", "the upstream instance at {host} rejected the request"),
    404: ("unknown-request", "the upstream instance at {host} does not know this request"),
    429: ("rate-limited", "the upstream instance at {host} is rate-limiting the facade"),
    500: ("server-error", "the upstream instance at {host} reported an internal error"),
}


def _translate_api_error(exc: ApiError, status: int | None, host: str) -> NetnlHTTPError:
    """One helper for every upstream call: `ApiError`'s status is not
    reliably recoverable from the message alone, so the opener wrapper
    above records the raw HTTP status and this function maps it.

    Upstream 401/403 are the operator's problem, not the tenant's — they
    map to 502 `upstream-error` so a tenant never mistrusts its own facade
    credential (design.md, "Upstream credential never leaves the server").
    A `status` of `None`, or a 2xx status that still produced an `ApiError`
    (a malformed 200 reply), means there is no real upstream status to pass
    through — that is also a 502 `upstream-error`.
    """
    if status in (401, 403):
        return NetnlHTTPError(
            502, "upstream-error", f"the upstream instance at {host} rejected the facade credential"
        )
    if status is None or 200 <= status < 300:
        return NetnlHTTPError(502, "upstream-error", f"unexpected reply from the upstream instance at {host}")
    label, template = _KNOWN_UPSTREAM_LABELS.get(
        status, ("upstream-error", "the upstream instance at {host} reported HTTP " + str(status))
    )
    return NetnlHTTPError(status, label, template.format(host=host))


def _reserving_reply(row: sqlite3.Row) -> dict:
    """The reply body for a row still `reserving` — upstream was never
    contacted (crash between the reservation commit and finalize; see
    design.md, "Concurrency and storage"). Owner-only, `request_id` is the
    facade id since no upstream id exists yet.
    """
    return {
        "api_version": API_VERSION,
        "request": {
            "request_id": row["facade_id"],
            "name": None,
            "request_type": row["request_type"],
            "status": store.RESERVING,
            "submit_date": row["submitted_at"],
            "finished_date": None,
        },
    }


def call_upstream(client, fn: Callable, *args, **kwargs):
    """Call an upstream-facing `BatchClient` method, translating any
    `ApiError`/`TransportError` into a `NetnlHTTPError` before it can reach
    a generic handler — this is the only path into the upstream instance
    from a route handler.
    """
    _thread_state.last_status = None
    try:
        return fn(*args, **kwargs)
    except ApiError as exc:
        status = getattr(_thread_state, "last_status", None)
        raise _translate_api_error(exc, status, client.endpoint_host) from exc
    except TransportError as exc:
        raise NetnlHTTPError(502, "upstream-unreachable", str(exc)) from exc


def create_app(settings: Settings, *, opener: Opener | None = None, now: Callable[[], datetime] | None = None) -> FastAPI:
    # No docs/openapi/redoc routes: those would leak framework-specific
    # paths outside the v2 shape this facade otherwise guarantees.
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    raw_opener = opener or urllib_opener
    client = upstream.build_client(settings, opener=_status_tracking_opener(raw_opener))

    # Round-1 fix (B1): schema migration uses its own short-lived
    # connection, closed immediately — it is not kept around as a shared
    # connection. Every request opens (and closes) its own connection via
    # `store.get_conn`; see that dependency and design.md, "Concurrency and
    # storage".
    migration_conn = store.connect(settings.db)
    try:
        store.migrate(migration_conn)
    finally:
        migration_conn.close()

    clock = now or (lambda: datetime.now(timezone.utc))

    app.state.settings = settings
    app.state.client = client
    app.state.now = clock
    app.state.metadata_cache = None  # {"payload": dict, "at": datetime} | None

    @app.middleware("http")
    async def enforce_body_size(request: Request, call_next):
        # Round-1 fix (M6): a total request-body size cap, checked from
        # `Content-Length` before the body is ever read or parsed, so a
        # megabyte-scale payload cannot reach pydantic's JSON parser.
        raw_length = request.headers.get("content-length")
        if raw_length is not None:
            try:
                length = int(raw_length)
            except ValueError:
                length = None
            if length is not None and length > settings.max_body_bytes:
                return JSONResponse(
                    error_body(
                        "bad-request",
                        f"request body of {length} bytes exceeds the "
                        f"{settings.max_body_bytes}-byte limit",
                    ),
                    status_code=400,
                )
        return await call_next(request)

    @app.middleware("http")
    async def provenance_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Netnl-Instance"] = settings.instance
        response.headers["X-Netnl-Notice"] = NOTICE
        return response

    @app.exception_handler(NetnlHTTPError)
    async def handle_netnl_http_error(request: Request, exc: NetnlHTTPError) -> JSONResponse:
        headers = {"WWW-Authenticate": 'Basic realm="netnl"'} if exc.status == 401 else None
        return JSONResponse(error_body(exc.label, exc.msg), status_code=exc.status, headers=headers)

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_exception(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        # Covers routing 404s and method-405s: any v2 path/method this
        # instance does not proxy. Unknown *request ids* never arrive here
        # — those routes exist and raise `NetnlHTTPError(404, "unknown-
        # request", ...)` explicitly.
        return JSONResponse(
            error_body("not-implemented", "this batch API v2 path is not proxied by this instance"),
            status_code=501,
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        fields = sorted(
            {".".join(str(part) for part in error["loc"] if part != "body") for error in exc.errors()}
        )
        msg = f"invalid request body: {', '.join(fields)}" if fields else "invalid request body"
        return JSONResponse(error_body("bad-request", msg), status_code=400)

    @app.exception_handler(Exception)
    async def handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        # Round-1 fix (M4): this handler is Starlette's `ServerErrorMiddleware`
        # handler (registered for the `Exception` class), which sits *outside*
        # the `@app.middleware("http")` stack above — `provenance_headers`
        # never runs on this path, so the headers are added here directly,
        # ensuring every error path carries them.
        return JSONResponse(
            error_body("server-error", "an unexpected error occurred"),
            status_code=500,
            headers={"X-Netnl-Instance": settings.instance, "X-Netnl-Notice": NOTICE},
        )

    @app.get("/health")
    def get_health() -> dict:
        # Anonymous by design (no `Depends(auth.authenticate)`, no DB
        # connection dependency): a K8s liveness/readiness probe must have a
        # target that needs no credential. It touches neither `client` (the
        # upstream instance) nor the store, and returns a fixed body — no
        # `api_version`, upstream host or credential can leak from a route
        # that does nothing but return a constant (design.md, "Facade image
        # and liveness"; spec.md, "Authenticated surface"). It is not part
        # of the v2 measurement subset.
        return {"status": "ok"}

    @app.get("/metadata/report")
    def get_metadata_report(credential=Depends(auth.authenticate)) -> dict:
        # Round-1 fix (B3): authenticated like every other route in the v2
        # subset — `auth.authenticate` runs (and can reject with 401) before
        # this body executes, so an anonymous caller never reaches the cache
        # or upstream.
        cache = app.state.metadata_cache
        current = app.state.now()
        if cache is not None:
            age = (current - cache["at"]).total_seconds()
            if 0 <= age < settings.metadata_ttl:
                return cache["payload"]
        payload = call_upstream(client, client.metadata_report)
        app.state.metadata_cache = {"payload": payload, "at": current}
        return payload

    @app.post("/requests")
    def submit(
        body: SubmitRequest,
        credential=Depends(auth.authenticate),
        conn=Depends(store.get_conn),
    ) -> dict:
        current = app.state.now()

        # Size and domain-shape checks run before any database or upstream
        # work — a rejection here never touches either (M6).
        limits.check_size(body.domains, settings)
        limits.check_domains(body.domains, settings)

        # Refresh stale non-terminal rows before reserving: kept outside the
        # write-lock transaction below (design.md, "Limits").
        limits.refresh_stale_non_terminal(conn, credential["id"], client, settings)

        facade_id = secrets.token_hex(16)
        submitted_at = store.utcnow_iso(lambda: current)

        # Round-1 fix (B2/M7): reserve rate + concurrency + the audit row
        # atomically, inside one `BEGIN IMMEDIATE` transaction, before
        # upstream is ever contacted — see `limits.reserve_submission`.
        limits.reserve_submission(
            conn,
            credential=credential,
            settings=settings,
            now=current,
            facade_id=facade_id,
            request_type=body.type,
            domain_count=len(body.domains),
            submitted_at=submitted_at,
        )

        # Only after the reservation committed — and outside the write
        # lock — is upstream contacted. If this raises, the reserved row
        # stays `reserving` (counts toward concurrency until pruned; see
        # design.md) rather than being left half-written.
        reply = call_upstream(client, client.submit, body.domains, body.type, body.name)

        upstream_request = reply["request"]
        upstream_id = upstream_request["request_id"]
        status = upstream_request["status"]
        store.finalize_reservation(conn, facade_id, upstream_id=upstream_id, status=status)

        out = copy.deepcopy(reply)
        out["request"]["request_id"] = facade_id
        return out

    @app.get("/requests/{request_id}")
    def get_status(
        request_id: str, credential=Depends(auth.authenticate), conn=Depends(store.get_conn)
    ) -> dict:
        row = _owned_request_or_404(conn, request_id, credential)
        if row["upstream_id"] is None:
            # Still `reserving` — upstream was never contacted (or a crash
            # happened between commit and finalize). Owner-only, per
            # design.md; nothing to ask upstream yet.
            return _reserving_reply(row)

        reply = call_upstream(client, client.status, row["upstream_id"])
        upstream_request = reply["request"]
        store.update_status(
            conn, request_id, upstream_request["status"], upstream_request.get("finished_date")
        )

        out = copy.deepcopy(reply)
        out["request"]["request_id"] = request_id
        return out

    @app.get("/requests/{request_id}/results")
    def get_results(
        request_id: str, credential=Depends(auth.authenticate), conn=Depends(store.get_conn)
    ) -> dict:
        row = _owned_request_or_404(conn, request_id, credential)
        if row["upstream_id"] is None:
            out = _reserving_reply(row)
            out["domains"] = {}
            return out

        reply = call_upstream(client, client.results, row["upstream_id"])
        upstream_request = reply["request"]
        store.update_status(
            conn, request_id, upstream_request["status"], upstream_request.get("finished_date")
        )

        out = copy.deepcopy(reply)
        out["request"]["request_id"] = request_id
        # `domains` is carried over from `reply` untouched by the deep copy
        # above — no key added, removed, reordered or rewritten.
        return out

    return app
