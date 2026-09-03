"""HTTP Basic authentication for the facade's own tenants.

Passwords are hashed with stdlib `hashlib.scrypt`, per-credential salt,
compared with `hmac.compare_digest`. An unknown username still costs one
scrypt computation (against a fixed dummy salt) before being rejected, so
"unknown user" and "wrong password" take the same time — that is the one
timing property worth paying for (it defeats username enumeration via a
timing side channel). A request that never even presents something
scrypt-shaped (round-2 fix, finding 1a — see `_parse_basic_auth` and its
caller below) is rejected without touching scrypt at all: there is no
username to enumerate against a blank or malformed header, so the "look
the same" property has nothing to protect there, and paying scrypt's cost
anyway would just be a free amplifier for an unauthenticated caller.

Round-2 fix (finding 1b): scrypt's cost (`_SCRYPT_PARAMS` below, ~tens of
milliseconds and ~16 MB per call) is deliberately expensive to resist
offline brute force, which makes an *unbounded number of concurrent*
verifications a cheap way to pin every worker thread in CPU/memory at
once. `_scrypt_semaphore` bounds how many scrypt computations may run at
the same time; a caller that cannot get a slot immediately is told to
retry rather than queued (see `_guarded_scrypt`). This is a last line of
defence for this process — see docs/how-to/deploy-facade.md for the
brute-force/rate-limiting an operator is expected to run at the edge,
which should absorb sustained abuse well before it reaches this bound.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import secrets
import threading
from datetime import datetime, timezone

import sqlite3

from fastapi import Depends, Request

from netnl import store
from netnl.errors import NetnlHTTPError

_SCRYPT_PARAMS = {"n": 2**14, "r": 8, "p": 1, "dklen": 32}
_DUMMY_SALT = bytes(16)

# Round-2 fix (finding 1b): a small, fixed cap — deliberately not
# tunable via the environment, since the point is a hard backstop on this
# process's own resource use, not a knob an operator needs to reach for.
# `Semaphore` (not `BoundedSemaphore`): every `acquire()` here is paired
# with exactly one `release()` in a `finally` below, so the extra
# over-release check `BoundedSemaphore` adds buys nothing.
_MAX_CONCURRENT_SCRYPT = 8
_scrypt_semaphore = threading.Semaphore(_MAX_CONCURRENT_SCRYPT)


def new_salt() -> bytes:
    return secrets.token_bytes(16)


def hash_password(password: str, salt: bytes) -> str:
    return hashlib.scrypt(password.encode("utf-8"), salt=salt, **_SCRYPT_PARAMS).hex()


def verify(stored_hash: str, salt: bytes, password: str) -> bool:
    candidate = hash_password(password, salt)
    return hmac.compare_digest(candidate, stored_hash)


def new_password() -> str:
    return secrets.token_urlsafe(24)


def _unauthorized() -> NetnlHTTPError:
    return NetnlHTTPError(401, "unauthorised", "invalid credentials")


def _overloaded() -> NetnlHTTPError:
    # 503, not 429: this is not the caller's own rate/quota being exceeded
    # (that is `limits.py`'s 429 `rate-limited`, a per-credential concept —
    # this rejection can hit a caller who has never made a request before).
    # It is the server saying "temporarily out of capacity for this kind of
    # work, try again shortly" — the textbook meaning of 503, and it plays
    # well with the CLI's existing error handling: `internetnl_cli.client`
    # treats every non-2xx status uniformly as an `ApiError` (see
    # `errors.py`), so this does not need special-case handling there to
    # avoid crashing anything; it just surfaces as a clear, retryable
    # server-error message instead of a misleading "your credentials are
    # wrong" 401 or a misleading "you are over your own limit" 429.
    return NetnlHTTPError(
        503,
        "overloaded",
        "too many concurrent authentication checks; try again shortly",
    )


def _guarded_scrypt(fn, *args):
    """Run a scrypt-costing callable behind `_scrypt_semaphore`.

    Non-blocking: a saturated semaphore fails fast with 503 rather than
    queueing the caller behind seven others' scrypt calls, which would
    just move the DoS from "CPU/memory exhaustion" to "every worker thread
    blocked waiting its turn" — see the module docstring, finding 1b.
    """
    if not _scrypt_semaphore.acquire(blocking=False):
        raise _overloaded()
    try:
        return fn(*args)
    finally:
        _scrypt_semaphore.release()


def _parse_basic_auth(header: str | None) -> tuple[str, str] | None:
    if not header or not header.startswith("Basic "):
        return None
    token = header[len("Basic "):]
    try:
        decoded = base64.b64decode(token, validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return None
    if ":" not in decoded:
        return None
    username, _, password = decoded.partition(":")
    return username, password


# Round-2 fix (finding 2): usernames in the `Authorization` header are
# attacker-controlled and must never be trusted enough to store or log
# verbatim. Printable-only (defence against log/audit injection via
# control characters) and length-capped (an unbounded string is itself a
# cheap way to grow the in-memory aggregator below, or an audit row,
# without limit) — long or odd enough to fail this is still recognisably
# "not a real username" to whoever reads the audit trail.
_MAX_LOGGED_USERNAME_LEN = 64


def _sanitize_username(username: str) -> str:
    cleaned = "".join(ch for ch in username if ch.isprintable())
    return cleaned[:_MAX_LOGGED_USERNAME_LEN]


def _route_path(request: Request) -> str:
    """The matched route's *template* (`/requests/{request_id}`), not the
    interpolated URL — the latter can carry an attacker-chosen,
    unbounded-length `request_id` segment straight into an audit row.
    Falls back to the raw path if routing metadata is ever unavailable
    (defensive; not expected to trigger in practice — every route this
    dependency guards is registered with a fixed template).
    """
    route = request.scope.get("route")
    path = getattr(route, "path", None)
    return path if isinstance(path, str) else request.url.path


# Round-2 fix (finding 2): failed-auth audit records, aggregated in memory
# per (sanitised username or `None`, route) per wall-clock minute, so that
# an arbitrarily large burst of failures — the exact scenario finding 1
# is about bounding on the CPU/memory side — writes at most one audit row
# per distinct key per minute instead of one row per failure. Without
# this, the audit table itself would be exactly the kind of unbounded
# write sink finding 2 warns against: a flood of bad-credential requests
# would grow it as fast as an attacker can send them.
#
# Bounding the aggregator's own memory, not just the audit table: every
# call sweeps out (and flushes to `audit`) every bucket whose minute is
# not the current one, before touching the current bucket. That caps the
# dict's size at "distinct (username, route) keys seen in the current
# one-minute window", regardless of how many distinct usernames were ever
# tried across all prior windows — an attacker cycling through unique
# throwaway usernames cannot make this grow without bound, only grow
# within a single one-minute window before it is flushed and dropped.
#
# A trade-off worth stating: the final window of an attack (or of the
# facade's lifetime) is only flushed when a *later* failure arrives to
# trigger the sweep — an attack that stops cold leaves its last <60s of
# tally unwritten. Acceptable: the goal is a detection signal for
# sustained abuse, not a byte-perfect count of a burst that already ended.
_auth_failure_lock = threading.Lock()
_auth_failure_buckets: dict[tuple[str | None, str], tuple[int, int]] = {}


def _current_minute(now: datetime) -> int:
    return int(now.astimezone(timezone.utc).timestamp() // 60)


def _flush_stale_auth_failure_buckets_locked(
    conn: sqlite3.Connection, now: datetime, current_minute: int
) -> None:
    """Caller holds `_auth_failure_lock`."""
    stale_keys = [
        key for key, (minute, _count) in _auth_failure_buckets.items() if minute != current_minute
    ]
    for key in stale_keys:
        minute, count = _auth_failure_buckets.pop(key)
        username, path = key
        store.record_audit(
            conn,
            at=store.utcnow_iso(lambda: now),
            credential=username,
            event="auth-failure",
            facade_id=None,
            domain_count=count,
            detail=path,
        )


def _sweep_stale_auth_failure_buckets(conn: sqlite3.Connection, now: datetime) -> None:
    """Called unconditionally from `authenticate`, on *every* call —
    success or failure — not only when the current call itself fails.
    Otherwise the last active window of a burst that stops (an attacker
    gives up, or simply the facade's only traffic for a while is
    legitimate) would sit unflushed until another failure happens to come
    along, which could be a long time or never. Piggybacking the sweep on
    any authenticated traffic gets that detection signal into `audit`
    promptly without adding a background thread or scheduler.
    """
    current_minute = _current_minute(now)
    with _auth_failure_lock:
        _flush_stale_auth_failure_buckets_locked(conn, now, current_minute)


def _record_auth_failure(
    conn: sqlite3.Connection, now: datetime, username: str | None, path: str
) -> None:
    sanitized = _sanitize_username(username) if username is not None else None
    key = (sanitized, path)
    current_minute = _current_minute(now)
    with _auth_failure_lock:
        # Defensive re-sweep: harmless (and cheap — the dict is normally
        # already clean from the unconditional sweep at the top of
        # `authenticate`) but keeps this function correct even if ever
        # called from somewhere that skipped that sweep.
        _flush_stale_auth_failure_buckets_locked(conn, now, current_minute)
        existing = _auth_failure_buckets.get(key)
        if existing is None or existing[0] != current_minute:
            _auth_failure_buckets[key] = (current_minute, 1)
        else:
            _auth_failure_buckets[key] = (current_minute, existing[1] + 1)


def authenticate(request: Request, conn: sqlite3.Connection = Depends(store.get_conn)):
    """FastAPI dependency: validate `Authorization: Basic`, return the
    `credentials` row on success.

    Deliberately does not use FastAPI's `HTTPBasic` — that raises its own
    non-v2-shaped error. Every rejection path raises `NetnlHTTPError`
    (401 `unauthorised` for a bad credential, 503 `overloaded` if the
    scrypt cap is saturated — see `_overloaded` above), whose handler adds
    the `WWW-Authenticate` header for the 401 case.

    `conn` comes from `store.get_conn`, the per-request connection (round-1
    fix B1) — never a connection shared across requests or threads. Route
    handlers that also declare `Depends(store.get_conn)` get the very same
    connection back (FastAPI caches dependencies per request).
    """
    now = request.app.state.now()
    path = _route_path(request)
    _sweep_stale_auth_failure_buckets(conn, now)

    parsed = _parse_basic_auth(request.headers.get("authorization"))
    if parsed is None:
        # Round-2 fix (finding 1a): no header, or one that is not even
        # shaped like `Basic base64(user:pass)`, carries no username to
        # enumerate — there is nothing here for the "unknown user" vs.
        # "wrong password" timing property to protect, so this is a fast
        # 401 with no scrypt call at all. Still audited (finding 2): a
        # flood of requests with no credentials at all is itself worth a
        # detection signal, and this path costs nothing to audit since it
        # never touches scrypt or the semaphore.
        _record_auth_failure(conn, now, None, path)
        raise _unauthorized()

    username, password = parsed
    credential = store.find_credential(conn, username)
    if credential is None:
        _guarded_scrypt(hash_password, password, _DUMMY_SALT)
        _record_auth_failure(conn, now, username, path)
        raise _unauthorized()

    salt = bytes.fromhex(credential["salt"])
    ok = _guarded_scrypt(verify, credential["password_hash"], salt, password)
    if not ok or credential["revoked_at"] is not None:
        _record_auth_failure(conn, now, username, path)
        raise _unauthorized()

    return credential
