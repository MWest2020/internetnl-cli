"""Anonymous, single-domain demo runs (`/demo/*`), opt-in via
`NETNL_DEMO_ENABLED=1`. See `openspec/changes/add-demo-run/design.md` for
the ten pinned decisions (D1-D15) this module implements; in short: one
bare domain, one borrowed credential row nobody ever authenticates as, and
three independent bounds layered in front of the exact same reservation
machinery the authenticated v2 subset already uses — the demo tenant's own
rate/concurrency cap (via `limits.reserve_submission`, called with a
`dataclasses.replace`d `Settings`), a per-IP-bucket hourly cap, and a
per-domain cooldown.

`register_routes` takes `client` and `call_upstream` as plain arguments
rather than importing anything from `netnl.api` — this module is imported
at `netnl.api` module-load time (for the shared header helper below), so
importing `netnl.api` back from here would be a real import cycle, not the
load-time-deferred kind `netnl.limits` works around with a local import.

Two in-memory structures below (`_ip_accepted`, `_domain_cooldowns`) are
process-global. They look unbounded but are not: only an *accepted* run —
one that has already passed `limits.reserve_submission`'s own atomic,
globally-capped reservation — ever adds an entry, and every call sweeps out
expired entries before reading or writing. This mirrors `netnl.auth`'s
failed-authentication aggregator (see that module's docstring): "sweep on
every call, bounded because only a capped event grows it", the same shape,
applied to acceptance instead of failure.
"""

from __future__ import annotations

import copy
import dataclasses
import ipaddress
import secrets
import threading
from datetime import datetime, timedelta
from typing import Callable

from fastapi import Depends, FastAPI, Request, Response
from pydantic import BaseModel, ConfigDict

from netnl import limits, store
from netnl.errors import NetnlHTTPError
from netnl.replies import API_VERSION
from netnl.settings import DemoSettings, Settings

# D12: fixed, never visitor-influenced — lets an operator reading the
# upstream instance's own dashboard tell demo traffic from tenant traffic
# apart without needing this facade's audit trail at all.
DEMO_UPSTREAM_NAME = "netnl-demo"

# D14: the one literal, directly-showable message for any domain rejection
# (shape or anti-SSRF) — written for a visitor reading a form, not an API
# consumer, unlike the tenant-facing wording in `limits.py`.
_BAD_DOMAIN_MSG = "enter a bare domain like example.nl, not a URL"

_UNAVAILABLE_MSG = "the live demo is temporarily unavailable; please try again shortly"
_COOLDOWN_MSG = "this domain was checked recently; please try again later"
_IP_LIMITED_MSG = "too many demo runs from this network recently; please try again later"


class DemoRequest(BaseModel):
    """`extra="forbid"` makes an extra field, a `type` field, or a list
    `domain` a structural impossibility, not merely a runtime rejection
    (D1)."""

    model_config = ConfigDict(extra="forbid")

    domain: str


# --- per-IP bucket (D4) -----------------------------------------------------

_ip_lock = threading.Lock()
_ip_accepted: dict[str, list[datetime]] = {}

_UNATTRIBUTED_IP_KEY = "unattributed"


def _client_ip_key(request: Request, header_name: str) -> str:
    """First comma-separated token of the configured header, `ipaddress`-
    validated, generalised to `/32` (IPv4) or `/64` (IPv6) — the bucket
    key. A missing or unparseable value falls into one shared bucket
    rather than being given its own identity or bypassing the limit
    outright (D4).
    """
    raw = request.headers.get(header_name)
    if not raw:
        return _UNATTRIBUTED_IP_KEY
    first = raw.split(",")[0].strip()
    try:
        addr = ipaddress.ip_address(first)
    except ValueError:
        return _UNATTRIBUTED_IP_KEY
    prefix = 32 if addr.version == 4 else 64
    network = ipaddress.ip_network(f"{addr}/{prefix}", strict=False)
    return str(network)


def _sweep_ip_buckets_locked(now: datetime) -> None:
    cutoff = now - timedelta(hours=1)
    for key in list(_ip_accepted):
        kept = [moment for moment in _ip_accepted[key] if moment >= cutoff]
        if kept:
            _ip_accepted[key] = kept
        else:
            del _ip_accepted[key]


def _ip_over_limit(key: str, now: datetime, per_hour: int) -> bool:
    with _ip_lock:
        _sweep_ip_buckets_locked(now)
        return len(_ip_accepted.get(key, [])) >= per_hour


def _record_ip_accept(key: str, now: datetime) -> None:
    with _ip_lock:
        _ip_accepted.setdefault(key, []).append(now)


# --- per-domain cooldown (D5) ------------------------------------------------

_cooldown_lock = threading.Lock()
_domain_cooldowns: dict[str, datetime] = {}


def _sweep_cooldowns_locked(now: datetime, cooldown_seconds: int) -> None:
    expired = [
        domain
        for domain, accepted_at in _domain_cooldowns.items()
        if (now - accepted_at).total_seconds() >= cooldown_seconds
    ]
    for domain in expired:
        del _domain_cooldowns[domain]


def _domain_on_cooldown(domain: str, now: datetime, cooldown_seconds: int) -> bool:
    with _cooldown_lock:
        _sweep_cooldowns_locked(now, cooldown_seconds)
        return domain in _domain_cooldowns


def _record_domain_cooldown(domain: str, now: datetime) -> None:
    with _cooldown_lock:
        _domain_cooldowns[domain] = now


def reset_state() -> None:
    """Test-only: clear both in-memory structures. This module's state is
    process-global, not scoped to a single app/test — mirrors `netnl.auth.
    _auth_failure_buckets`'s own reset story (see `tests/netnl/conftest.py`,
    `_reset_auth_failure_aggregator`).
    """
    with _ip_lock:
        _ip_accepted.clear()
    with _cooldown_lock:
        _domain_cooldowns.clear()


# --- origin and shared response headers (D6, D7, D8) ------------------------


def check_origin(request: Request, demo_cfg: DemoSettings) -> None:
    """A *present* Origin that does not match the one configured origin is
    refused outright — 403 `forbidden-origin` — on an actual demo route. An
    absent Origin is allowed through: plenty of legitimate non-browser
    callers (curl, `scripts/acceptance.sh`-style probes) never send one.
    Never applied to the `OPTIONS` preflight routes (D8) — those always
    answer 204; the browser's own CORS enforcement does the blocking based
    on whether `demo_response_headers` below added the CORS headers.
    """
    origin = request.headers.get("origin")
    if origin is not None and origin != demo_cfg.allowed_origin:
        raise NetnlHTTPError(403, "forbidden-origin", "this origin is not allowed to use the demo")


def demo_response_headers(request: Request, settings: Settings) -> dict[str, str]:
    """Headers added to every `/demo/*` reply (D6, D7): `Cache-Control:
    no-store` and `Vary: Origin` unconditionally; `Access-Control-Allow-
    Origin` (the literal configured origin, never an echo of a mismatched
    request Origin) and `Access-Control-Expose-Headers` only when the
    request's `Origin` is absent or matches it exactly — never paired with
    `Access-Control-Allow-Credentials`. Empty for any path outside
    `/demo/*`, or when the demo family is not configured at all.

    Called from two places: the `demo_headers` middleware in `api.py`
    (registered *last*, so it wraps every other middleware including the
    body-size short-circuit — D7), and `api.handle_unexpected`, which sits
    *outside* the whole middleware stack (Starlette's `ServerErrorMiddleware`
    — the same reason that handler already adds the provenance/security
    headers by hand) and so must call this helper itself for a `/demo/*`
    path to carry these headers on a 500 too.
    """
    demo_cfg = settings.demo
    if demo_cfg is None or not request.url.path.startswith("/demo/"):
        return {}
    headers = {"Cache-Control": "no-store", "Vary": "Origin"}
    origin = request.headers.get("origin")
    if origin is None or origin == demo_cfg.allowed_origin:
        headers["Access-Control-Allow-Origin"] = demo_cfg.allowed_origin
        headers["Access-Control-Expose-Headers"] = "X-Netnl-Instance, X-Netnl-Notice"
    return headers


# --- the borrowed credential (D3, kill switch) ------------------------------


def _available_demo_credential(conn, demo_cfg: DemoSettings):
    """The demo's entire kill switch: an operator revokes (or never issues)
    `NETNL_DEMO_TENANT`'s credential row and every demo request answers 503
    `demo-unavailable` from here on — no restart, no configuration change.
    """
    credential = store.find_credential(conn, demo_cfg.tenant)
    if credential is None or credential["revoked_at"] is not None:
        raise NetnlHTTPError(503, "demo-unavailable", _UNAVAILABLE_MSG)
    return credential


# --- domain handling (D14) ---------------------------------------------------


def _normalize_domain(raw: str) -> str:
    """The *only* normalisation applied (D14) — no other rewriting."""
    return raw.strip().lower()


def _validate_domain(domain: str, settings: Settings) -> None:
    """Reuses `limits.check_domains` verbatim for shape and anti-SSRF
    checks (never reimplemented here) but translates *any* failure from it
    into the single literal, directly-showable D14 message — the tenant-
    facing wording in `limits.py` ("the facade only accepts a public,
    multi-label hostname...") is written for an API consumer, not a
    visitor filling in a form.
    """
    try:
        limits.check_domains([domain], settings)
    except NetnlHTTPError as exc:
        raise NetnlHTTPError(400, "bad-request", _BAD_DOMAIN_MSG) from exc


def _reserving_reply(row) -> dict:
    """Identical in shape to `api._reserving_reply` — a row still
    `reserving` (upstream never contacted, or a crash between commit and
    finalize). Not shared code (it is a five-line dict literal); kept a
    separate copy here rather than importing `netnl.api` back, which would
    be a real cycle (see the module docstring).
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


def register_routes(app: FastAPI, settings: Settings, client, call_upstream: Callable) -> None:
    """Registers the `/demo/*` route family — only when `settings.demo` is
    not `None` (D2): otherwise this is a no-op and the paths do not exist,
    falling through to the ordinary 501 `not-implemented` catch-all like
    any other unmapped path.
    """
    demo_cfg = settings.demo
    if demo_cfg is None:
        return

    @app.post("/demo/requests")
    def demo_submit(body: DemoRequest, request: Request, conn=Depends(store.get_conn)) -> dict:
        domain = _normalize_domain(body.domain)
        check_origin(request, demo_cfg)
        credential = _available_demo_credential(conn, demo_cfg)

        current = app.state.now()

        if _domain_on_cooldown(domain, current, demo_cfg.domain_cooldown_seconds):
            raise NetnlHTTPError(429, "rate-limited", _COOLDOWN_MSG)

        ip_key = _client_ip_key(request, demo_cfg.client_ip_header)
        if _ip_over_limit(ip_key, current, demo_cfg.per_ip_per_hour):
            raise NetnlHTTPError(429, "rate-limited", _IP_LIMITED_MSG)

        _validate_domain(domain, settings)

        # D3: the demo tenant's own rate/concurrency numbers gate this
        # submission — not the operator's own tenant defaults — via the
        # exact same atomic reservation transaction the v2 subset uses.
        # `max_domains=1` is enforced structurally by the request body
        # shape already (D1); pinning it here too documents the intent
        # rather than relying only on that.
        demo_settings = dataclasses.replace(
            settings,
            rate_limit=demo_cfg.max_per_hour,
            max_concurrent=demo_cfg.max_concurrent,
            max_domains=1,
        )
        facade_id = secrets.token_hex(16)
        submitted_at = store.utcnow_iso(lambda: current)
        limits.reserve_submission(
            conn,
            credential=credential,
            settings=demo_settings,
            now=current,
            facade_id=facade_id,
            request_type="web",
            domain_count=1,
            submitted_at=submitted_at,
        )

        # Only on a successful reservation (D4/D5: only accepted runs
        # count) are the per-IP and per-domain-cooldown entries recorded.
        _record_ip_accept(ip_key, current)
        _record_domain_cooldown(domain, current)

        reply = call_upstream(client, client.submit, [domain], "web", DEMO_UPSTREAM_NAME)

        upstream_request = reply["request"]
        upstream_id = upstream_request["request_id"]
        status = upstream_request["status"]
        store.finalize_reservation(conn, facade_id, upstream_id=upstream_id, status=status)

        out = copy.deepcopy(reply)
        out["request"]["request_id"] = facade_id
        return out

    @app.get("/demo/requests/{request_id}")
    def demo_status(request_id: str, request: Request, conn=Depends(store.get_conn)) -> dict:
        check_origin(request, demo_cfg)
        credential = _available_demo_credential(conn, demo_cfg)
        row = store.owned_request_or_404(conn, request_id, credential["id"])
        if row["upstream_id"] is None:
            return _reserving_reply(row)

        reply = call_upstream(client, client.status, row["upstream_id"])
        upstream_request = reply["request"]
        store.update_status(
            conn, request_id, upstream_request["status"], upstream_request.get("finished_date")
        )

        out = copy.deepcopy(reply)
        out["request"]["request_id"] = request_id
        return out

    @app.get("/demo/requests/{request_id}/results")
    def demo_results(request_id: str, request: Request, conn=Depends(store.get_conn)) -> dict:
        check_origin(request, demo_cfg)
        credential = _available_demo_credential(conn, demo_cfg)
        row = store.owned_request_or_404(conn, request_id, credential["id"])
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
        return out

    # D8: explicit OPTIONS routes, unconditionally 204 — without these the
    # 501 not-implemented catch-all would answer a browser's own preflight
    # and break every demo call from a real browser. Never subject to the
    # 403 `forbidden-origin` check above: a mismatched-origin preflight
    # still gets 204, just without the CORS headers `demo_response_headers`
    # would otherwise add on a match, leaving the browser to enforce the
    # resulting block itself.
    @app.options("/demo/requests")
    @app.options("/demo/requests/{request_id}")
    @app.options("/demo/requests/{request_id}/results")
    def demo_preflight() -> Response:
        return Response(status_code=204)
