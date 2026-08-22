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
from netnl.replies import NOTICE, error_body
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


def _translate_api_error(exc: ApiError, status: int | None, host: str) -> NetnlHTTPError:
    """One helper for every upstream call: `ApiError`'s status is not
    reliably recoverable from the message alone, so the opener wrapper
    above records the raw HTTP status and this function maps it.

    Upstream 401/403 are the operator's problem, not the tenant's — they
    map to 502 `upstream-error` so a tenant never mistrusts its own facade
    credential (design.md, "Upstream credential never leaves the server").
    """
    if status in (401, 403):
        return NetnlHTTPError(
            502, "upstream-error", f"the upstream instance at {host} rejected the facade credential"
        )
    if status == 400:
        return NetnlHTTPError(400, "bad-request", f"the upstream instance at {host} rejected the request")
    if status == 404:
        return NetnlHTTPError(
            404, "unknown-request", f"the upstream instance at {host} does not know this request"
        )
    if status == 429:
        return NetnlHTTPError(
            429, "rate-limited", f"the upstream instance at {host} is rate-limiting the facade"
        )
    if status == 500:
        return NetnlHTTPError(
            500, "server-error", f"the upstream instance at {host} reported an internal error"
        )
    return NetnlHTTPError(502, "upstream-error", f"unexpected reply from the upstream instance at {host}")


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

    conn = store.connect(settings.db)
    store.migrate(conn)

    clock = now or (lambda: datetime.now(timezone.utc))

    app.state.settings = settings
    app.state.client = client
    app.state.conn = conn
    app.state.now = clock
    app.state.metadata_cache = None  # {"payload": dict, "at": datetime} | None

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
        return JSONResponse(error_body("server-error", "an unexpected error occurred"), status_code=500)

    @app.get("/metadata/report")
    def get_metadata_report() -> dict:
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
    def submit(body: SubmitRequest, credential=Depends(auth.authenticate)) -> dict:
        conn = app.state.conn
        current = app.state.now()

        # Size, then rate, then concurrency — every check runs before any
        # upstream call; a size/rate rejection never touches upstream.
        limits.check_size(body.domains, settings)
        limits.check_rate(conn, credential["username"], settings, current)
        limits.check_concurrency(conn, credential["id"], client, settings)

        reply = call_upstream(client, client.submit, body.domains, body.type, body.name)

        facade_id = secrets.token_hex(16)
        upstream_request = reply["request"]
        upstream_id = upstream_request["request_id"]
        status = upstream_request["status"]
        submitted_at = store.utcnow_iso(lambda: current)

        # Both writes happen before the reply is sent, per design.md's
        # "Submission is audited" scenario.
        store.insert_request(
            conn,
            facade_id=facade_id,
            upstream_id=upstream_id,
            credential_id=credential["id"],
            request_type=body.type,
            domain_count=len(body.domains),
            submitted_at=submitted_at,
            last_status=status,
        )
        store.record_audit(
            conn,
            at=submitted_at,
            credential=credential["username"],
            event="submit",
            facade_id=facade_id,
            domain_count=len(body.domains),
        )

        out = copy.deepcopy(reply)
        out["request"]["request_id"] = facade_id
        return out

    @app.get("/requests/{request_id}")
    def get_status(request_id: str, credential=Depends(auth.authenticate)) -> dict:
        conn = app.state.conn
        row = _owned_request_or_404(conn, request_id, credential)

        reply = call_upstream(client, client.status, row["upstream_id"])
        upstream_request = reply["request"]
        store.update_status(
            conn, request_id, upstream_request["status"], upstream_request.get("finished_date")
        )

        out = copy.deepcopy(reply)
        out["request"]["request_id"] = request_id
        return out

    @app.get("/requests/{request_id}/results")
    def get_results(request_id: str, credential=Depends(auth.authenticate)) -> dict:
        conn = app.state.conn
        row = _owned_request_or_404(conn, request_id, credential)

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
