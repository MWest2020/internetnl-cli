"""Anonymous, single-domain demo runs (`/demo/*`), opt-in via
`NETNL_DEMO_ENABLED=1`. See `openspec/changes/add-demo-run/design.md` for
the pinned decisions (D1-D15) this module implements; in short: one bare
domain, one borrowed credential row nobody ever authenticates as, and
several independent bounds layered in front of the exact same reservation
machinery the authenticated v2 subset already uses — the demo tenant's own
rate/concurrency cap (via `limits.reserve_submission`, called with a
`dataclasses.replace`d `Settings`), a per-IP-bucket hourly cap on *accepted*
submissions, a per-domain cooldown, and a per-IP-bucket hourly cap on
*polls* (status/results GETs).

`register_routes` takes `client` and `call_upstream` as plain arguments
rather than importing anything from `netnl.api` — this module is imported
at `netnl.api` module-load time (for the shared header helper below), so
importing `netnl.api` back from here would be a real import cycle, not the
load-time-deferred kind `netnl.limits` works around with a local import.

Three in-memory structures below (`_ip_accepted`, `_domain_cooldowns`,
`_poll_counts`) are process-global, one per running `netnl-serve` process
(not shared across replicas — see design.md's "Header trust and multiple
replicas" note). Each is swept of expired entries on every call and,
additionally, hard-capped at `_MAX_BUCKETS` distinct keys (an "overflow"
key merges any key past that cap): the per-IP and per-poll buckets are
claimed *before* the reservation that could still fail (see
`_try_claim_ip_slot`/`_try_claim_poll` below), so — unlike the strict "only
ever grows on an outcome gated by the global cap" story an earlier version
of this module could make — a flood of distinct source addresses, each
making exactly one request, can transiently grow these dicts before a
failed reservation releases its entry again. `_MAX_BUCKETS` is the actual
hard ceiling that keeps that transient growth bounded regardless of how
many distinct addresses an attacker can present, mirroring
`netnl.auth._auth_failure_buckets`'s own `_MAX_BUCKETS`/overflow-key shape
(see that module's docstring) rather than merely asserting boundedness by
construction.
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

# Builder-review fix (S5): the per-IP cap and the per-domain cooldown now
# share this one literal — before this fix they had distinct wording
# (a per-domain-specific "this domain was checked recently..." vs. a
# per-IP-specific "too many demo runs from this network..."), which let an
# already over-quota IP learn whether an *unrelated* domain was on cooldown
# by reading which message it got back. With one shared text, that channel
# is closed regardless of check order — but the check order below (per-IP
# claimed before the domain cooldown is ever touched) additionally means an
# over-quota IP never even attempts the domain claim, so it can no longer
# consume (and thus reveal, to a later prober, "hm, still on cooldown") a
# cooldown slot for a domain it was never actually going to run.
_TOO_MANY_RECENT_MSG = "too many demo runs recently from this network; please try again later"

# Builder-review fix (S4=B2): `limits.reserve_submission`'s own 429 names
# the operator-configured numbers ("rate limit of N submissions per hour
# reached", "%d runs already in progress; the limit is %d") — written for
# an authenticated tenant reading an API response, not for an anonymous
# visitor filling in a form. That wording must never reach a demo reply
# verbatim (D13); this is the one, separate, visitor-facing literal it is
# rewritten to.
_DEMO_BUSY_MSG = "the demo is busy right now; please try again shortly"

# Builder-review fix (M3): `_translate_api_error` (api.py) and
# `TransportError`'s own message both embed the upstream hostname — fine
# for an authenticated tenant, not for an anonymous visitor, for whom that
# hostname is an internal implementation detail with nothing to do with
# their own request. `_visitor_upstream_error` below rewrites *every*
# upstream-originated `NetnlHTTPError` reaching a demo route to one of
# these two fixed, host-free outcomes.
_UPSTREAM_UNREACHABLE_MSG = "the measurement instance is unreachable right now"

# Builder-review fix (M2): a per-IP bound on *polling* (GET status/
# results), a cost the per-IP submit cap above does nothing about.
_POLL_LIMITED_MSG = "too many status checks from this network recently; please try again later"


class DemoRequest(BaseModel):
    """`extra="forbid"` makes an extra field, a `type` field, or a list
    `domain` a structural impossibility, not merely a runtime rejection
    (D1)."""

    model_config = ConfigDict(extra="forbid")

    domain: str


# --- bounded bucket keying, shared by the per-IP, per-poll and per-domain
# --- structures below (builder-review fix, S12) -----------------------------

# A hard ceiling on the number of *distinct keys* any one structure below
# will ever hold at once, regardless of how many distinct client IPs or
# submitted domains an attacker can present — see the module docstring.
_MAX_BUCKETS = 4096
_OVERFLOW_KEY = "<demo-overflow>"


def _effective_key_locked(buckets: dict, key) -> object:
    """Caller already holds the lock guarding `buckets`. An already-tracked
    key keeps its own identity (so an existing accepted-visitor's count
    stays accurate); a genuinely new key is folded into the shared overflow
    key once `_MAX_BUCKETS` distinct keys already exist, rather than
    minting bucket number `_MAX_BUCKETS + 1`.
    """
    if key in buckets or len(buckets) < _MAX_BUCKETS:
        return key
    return _OVERFLOW_KEY


# --- per-IP bucket, accepted submissions (D4) --------------------------------

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


def _try_claim_ip_slot(key: str, now: datetime, per_hour: int) -> str | None:
    """Builder-review fix (S1=M1): sweep, check and insert in one lock
    hold — the previous shape (`_ip_over_limit` reading, then a separate
    `_record_ip_accept` writing, each under its own *independent* lock
    acquisition) left a check-then-act window: measured on a real uvicorn
    server, 12 parallel submits from one IP against a cap of 2 all read
    "under limit" before any of them had written their own entry, so all
    12 were accepted. Returns the *effective* key this call claimed a slot
    under (see `_effective_key_locked`) on success, `None` when the bucket
    was already full — never both inserting and signalling "reject".
    """
    with _ip_lock:
        _sweep_ip_buckets_locked(now)
        effective = _effective_key_locked(_ip_accepted, key)
        bucket = _ip_accepted.get(effective, [])
        if len(bucket) >= per_hour:
            return None
        bucket.append(now)
        _ip_accepted[effective] = bucket
        return effective


def _release_ip_slot(effective_key: str, now: datetime) -> None:
    """Undo a claim made by `_try_claim_ip_slot` for *this* request only —
    called when a later step (the atomic reservation, or the upstream call
    itself) ultimately fails, so a demo run that never actually happened
    does not still cost the visitor one of their per-IP slots (builder-
    review fix, S1=M1 reviewer-minor)."""
    with _ip_lock:
        bucket = _ip_accepted.get(effective_key)
        if not bucket:
            return
        try:
            bucket.remove(now)
        except ValueError:
            return
        if bucket:
            _ip_accepted[effective_key] = bucket
        else:
            del _ip_accepted[effective_key]


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


def _try_claim_domain(domain: str, now: datetime, cooldown_seconds: int) -> str | None:
    """Same atomic claim shape as `_try_claim_ip_slot`, for the same reason
    (S1=M1): sweep, check and insert in one lock hold, so two concurrent
    submits for the same domain can never both read "not on cooldown"
    before either has recorded its own acceptance.
    """
    with _cooldown_lock:
        _sweep_cooldowns_locked(now, cooldown_seconds)
        effective = _effective_key_locked(_domain_cooldowns, domain)
        if effective in _domain_cooldowns:
            return None
        _domain_cooldowns[effective] = now
        return effective


def _release_domain_claim(effective_key: str, now: datetime) -> None:
    """Undo a claim made by `_try_claim_domain` for this request only — see
    `_release_ip_slot`'s docstring for why."""
    with _cooldown_lock:
        if _domain_cooldowns.get(effective_key) == now:
            del _domain_cooldowns[effective_key]


# --- per-IP poll budget (M2) --------------------------------------------------

_poll_lock = threading.Lock()
_poll_counts: dict[str, list[datetime]] = {}


def _sweep_poll_buckets_locked(now: datetime) -> None:
    cutoff = now - timedelta(hours=1)
    for key in list(_poll_counts):
        kept = [moment for moment in _poll_counts[key] if moment >= cutoff]
        if kept:
            _poll_counts[key] = kept
        else:
            del _poll_counts[key]


def _try_claim_poll(key: str, now: datetime, per_hour: int) -> bool:
    """Unlike the per-IP submit bucket, there is nothing to release here on
    a later failure — a poll either happened (and counts) or was rejected
    outright (and was never counted in the first place); there is no
    "reservation" a poll could roll back.
    """
    with _poll_lock:
        _sweep_poll_buckets_locked(now)
        effective = _effective_key_locked(_poll_counts, key)
        bucket = _poll_counts.get(effective, [])
        if len(bucket) >= per_hour:
            return False
        bucket.append(now)
        _poll_counts[effective] = bucket
        return True


def reset_state() -> None:
    """Test-only: clear all three in-memory structures. This module's state
    is process-global, not scoped to a single app/test — mirrors
    `netnl.auth._auth_failure_buckets`'s own reset story (see
    `tests/netnl/conftest.py`, `_reset_auth_failure_aggregator`).
    """
    with _ip_lock:
        _ip_accepted.clear()
    with _cooldown_lock:
        _domain_cooldowns.clear()
    with _poll_lock:
        _poll_counts.clear()


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


def _preflight_headers(request: Request, demo_cfg: DemoSettings) -> dict[str, str]:
    """Builder-review fix (S7): the three headers an actual cross-origin
    `POST` (with a JSON `Content-Type`) needs a browser's own preflight to
    have answered before it will even attempt the real request —
    previously missing entirely, which meant a browser could never
    complete a `POST /demo/requests` with `Content-Type: application/json`
    from a matching origin at all. Only added on an *origin match* (or no
    `Origin` at all, e.g. a non-browser preflight probe) — on a mismatch,
    `demo_preflight` still answers 204 with none of this, same as it
    already withholds the D6 CORS headers on a mismatch.
    """
    origin = request.headers.get("origin")
    if origin is not None and origin != demo_cfg.allowed_origin:
        return {}
    return {
        "Access-Control-Allow-Methods": "POST, GET, OPTIONS",
        "Access-Control-Allow-Headers": "content-type",
        "Access-Control-Max-Age": "600",
    }


# --- the borrowed credential (D3, kill switch) ------------------------------


def _available_demo_credential(conn, demo_cfg: DemoSettings):
    """The demo's kill switch: an operator revokes `NETNL_DEMO_TENANT`'s
    credential row and every demo request answers 503 `demo-unavailable`
    from here on — no restart, no configuration change. `netnl-admin user
    reissue <name>` is the matching "turn it back on" lever (works on the
    row whether it is currently revoked or not — see `admin.py`).
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


def _visitor_upstream_error(exc: NetnlHTTPError) -> NetnlHTTPError:
    """Builder-review fix (M3): every upstream-originated `NetnlHTTPError`
    reaching a demo route (`call_upstream`'s `_translate_api_error`
    messages, and `TransportError`'s own message) embeds the upstream
    hostname (`api.py`, `_KNOWN_UPSTREAM_LABELS`; `internetnl_cli.client`'s
    `f"... while contacting {host}"`) — meaningful for an authenticated
    tenant, an internal implementation detail for an anonymous visitor.
    Collapses to one of two fixed, host-free outcomes:

    - `label == "upstream-unreachable"` (a `TransportError` — upstream
      could not be reached at the network level at all) reuses the exact
      503 `demo-unavailable` outcome the kill switch itself already uses:
      from a demo visitor's point of view "upstream is unreachable" and
      "the borrowed credential was revoked" both mean the same thing —
      "the demo cannot run right now" — and deserve the identical,
      already-documented literal.
    - Anything else `call_upstream` produced (401/403 translated to 502,
      a malformed 2xx, or a real upstream status passed through — 400,
      404, 429, 500, ...) becomes a fixed 502 `upstream-error` with
      `_UPSTREAM_UNREACHABLE_MSG`. The demo visitor never sees which real
      upstream status caused it — that distinction (e.g. "upstream itself
      is being rate-limited") is meaningless to them and needlessly
      narrows what "something is wrong right now" could mean.
    """
    if exc.label == "upstream-unreachable":
        return NetnlHTTPError(503, "demo-unavailable", _UNAVAILABLE_MSG)
    return NetnlHTTPError(502, "upstream-error", _UPSTREAM_UNREACHABLE_MSG)


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


def _status_reply_from_row(row) -> dict:
    """Builder-review fix (M2): the reply for a status poll of a row whose
    *stored* status is already terminal (`done`/`error`/`cancelled`) —
    built entirely from the store, with no upstream call at all. A
    terminal status cannot change any further, so re-asking upstream on
    every subsequent poll of the same id buys nothing but cost: an
    unbounded number of anonymous polls of one finished id previously cost
    one upstream call each. `name` is `None`, same convention
    `_reserving_reply` already uses: this facade only ever surfaces a
    `name` value it just received from upstream in the same response, not
    one reconstructed from what was stored.
    """
    return {
        "api_version": API_VERSION,
        "request": {
            "request_id": row["facade_id"],
            "name": None,
            "request_type": row["request_type"],
            "status": row["last_status"],
            "submit_date": row["submitted_at"],
            "finished_date": row["finished_at"],
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

    def _demo_call_upstream(fn: Callable, *args):
        """`call_upstream`, with `_visitor_upstream_error` applied to any
        `NetnlHTTPError` it raises — see that function's docstring (M3).
        """
        try:
            return call_upstream(client, fn, *args)
        except NetnlHTTPError as exc:
            raise _visitor_upstream_error(exc) from exc

    def _enforce_poll_budget(request: Request, current: datetime) -> None:
        ip_key = _client_ip_key(request, demo_cfg.client_ip_header)
        if not _try_claim_poll(ip_key, current, demo_cfg.polls_per_ip_per_hour):
            raise NetnlHTTPError(429, "rate-limited", _POLL_LIMITED_MSG)

    @app.post("/demo/requests")
    def demo_submit(body: DemoRequest, request: Request, conn=Depends(store.get_conn)) -> dict:
        # Builder-review fix (S5): reordered to origin -> availability ->
        # shape/validation -> per-IP -> cooldown (see `_TOO_MANY_RECENT_MSG`
        # above for why per-IP is claimed strictly before the domain
        # cooldown is ever touched).
        check_origin(request, demo_cfg)
        credential = _available_demo_credential(conn, demo_cfg)

        domain = _normalize_domain(body.domain)
        _validate_domain(domain, settings)

        current = app.state.now()
        ip_key = _client_ip_key(request, demo_cfg.client_ip_header)

        # Builder-review fix (S1=M1): atomic claim-then-reserve, not
        # check-then-act — see `_try_claim_ip_slot`'s docstring for the
        # measured race this closes.
        claimed_ip = _try_claim_ip_slot(ip_key, current, demo_cfg.per_ip_per_hour)
        if claimed_ip is None:
            raise NetnlHTTPError(429, "rate-limited", _TOO_MANY_RECENT_MSG)

        claimed_domain = _try_claim_domain(domain, current, demo_cfg.domain_cooldown_seconds)
        if claimed_domain is None:
            _release_ip_slot(claimed_ip, current)
            raise NetnlHTTPError(429, "rate-limited", _TOO_MANY_RECENT_MSG)

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

        # Builder-review fix (S2=B1): refresh stale non-terminal rows
        # *before* reserving, exactly like the tenant path (`api.py`'s
        # `submit`, called just before its own `reserve_submission`) —
        # without this, a demo run whose upstream status went terminal
        # without ever being polled kept occupying a concurrency slot for
        # up to `NETNL_DEMO_RETENTION_HOURS` (24h by default), able to
        # starve every later visitor with 429s in the meantime.
        limits.refresh_stale_non_terminal(conn, credential["id"], client, demo_settings)

        facade_id = secrets.token_hex(16)
        submitted_at = store.utcnow_iso(lambda: current)
        try:
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
        except NetnlHTTPError:
            # Builder-review fix (S4=B2): never let `reserve_submission`'s
            # tenant-facing 429 (it names the configured numbers) reach a
            # demo visitor verbatim (D13). Also release the per-IP/cooldown
            # claims made above (S1=M1 reviewer-minor): a submission that
            # never actually got a reservation must not still cost the
            # visitor one of their bounded slots.
            _release_domain_claim(claimed_domain, current)
            _release_ip_slot(claimed_ip, current)
            raise NetnlHTTPError(429, "rate-limited", _DEMO_BUSY_MSG)

        try:
            reply = _demo_call_upstream(client.submit, [domain], "web", DEMO_UPSTREAM_NAME)
        except NetnlHTTPError:
            # Same reasoning as the reservation-failure branch above: an
            # upstream failure after a successful reservation produced no
            # result for this visitor, so it must not burn either bound.
            _release_domain_claim(claimed_domain, current)
            _release_ip_slot(claimed_ip, current)
            raise

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
        current = app.state.now()
        # Builder-review fix (M2): bounds anonymous polling before it can
        # ever reach the store lookup or upstream below.
        _enforce_poll_budget(request, current)

        row = store.owned_request_or_404(conn, request_id, credential["id"])
        if row["upstream_id"] is None:
            return _reserving_reply(row)
        if row["last_status"] in store.TERMINAL_STATUSES:
            # Builder-review fix (M2): a terminal stored status cannot
            # change any further — answer from the store, no upstream call.
            return _status_reply_from_row(row)

        reply = _demo_call_upstream(client.status, row["upstream_id"])
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
        current = app.state.now()
        # Builder-review fix (M2): same poll budget as `demo_status` — a
        # terminal row's *results*, unlike its status, are always fetched
        # from upstream (passthrough; this facade never stores the
        # `domains` payload), so this is the only bound on repeat calls
        # here.
        _enforce_poll_budget(request, current)

        row = store.owned_request_or_404(conn, request_id, credential["id"])
        if row["upstream_id"] is None:
            out = _reserving_reply(row)
            out["domains"] = {}
            return out

        reply = _demo_call_upstream(client.results, row["upstream_id"])
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
    def demo_preflight(request: Request) -> Response:
        response = Response(status_code=204)
        # Builder-review fix (S7): without these, a browser's own preflight
        # never grants permission for the actual cross-origin `POST` (with
        # `Content-Type: application/json`) the demo page needs to make —
        # see `_preflight_headers`'s docstring.
        for name, value in _preflight_headers(request, demo_cfg).items():
            response.headers[name] = value
        return response
