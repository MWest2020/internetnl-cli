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
the same time. This is a last line of defence for this process — see
docs/how-to/deploy-facade.md for the brute-force/rate-limiting an operator
is expected to run at the edge, which should absorb sustained abuse well
before it reaches this bound.

Round-3 fix (security-M1): the semaphore used to fail fast (non-blocking)
on saturation, which measured 23 spurious 503s on ordinary, legitimate
concurrent traffic in this project's own concurrency tests (a threadpool
of ~40 sync handlers can easily have more than `_MAX_CONCURRENT_SCRYPT`
requests reach `authenticate` at the same instant without anything being
wrong). `_guarded_scrypt` now waits up to `_SCRYPT_ACQUIRE_TIMEOUT` (a
short, bounded wait) for a slot before giving up — a queue is safe here
specifically *because* it is bounded: the only callers that can ever be
queued on this semaphore are requests already admitted into the ASGI
server's own worker threadpool (on the order of tens of threads, not an
unbounded number of TCP connections), so the worst case is "every worker
thread waits at most ~1s", not "unboundedly many callers pile up
forever". 503 `overloaded` is now reserved for *sustained* saturation —
the cap still stays full after the bounded wait — not for an ordinary
burst that clears within it.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import logging
import os
import secrets
import threading
from datetime import datetime, timezone

import sqlite3

from fastapi import Depends, Request

from netnl import store
from netnl.errors import NetnlHTTPError

_logger = logging.getLogger(__name__)

_SCRYPT_PARAMS = {"n": 2**14, "r": 8, "p": 1, "dklen": 32}
_DUMMY_SALT = bytes(16)

# Round-3 fix (security-M1, reviewer-m11): derived from the host's CPU
# count rather than hardcoded, floored at 4 and capped at 8 — scrypt is
# CPU/memory-bound, so the useful amount of *true* concurrency is bounded
# by cores, and a single-core-ish container gets a floor generous enough
# not to make every legitimate concurrent login queue. This is a
# **single-process** assumption (design.md, "Authentication cost is
# bounded"): each `netnl-serve` process gets its own semaphore and its own
# quota, so running N processes multiplies the effective process-wide cap
# by N — an operator running multiple workers/replicas should account for
# that when sizing the edge rate limit, not assume this number is a
# cluster-wide ceiling.
_MAX_CONCURRENT_SCRYPT = max(4, min(8, os.cpu_count() or 4))
# `Semaphore` (not `BoundedSemaphore`): every `acquire()` here is paired
# with exactly one `release()` in a `finally` below, so the extra
# over-release check `BoundedSemaphore` adds buys nothing.
_scrypt_semaphore = threading.Semaphore(_MAX_CONCURRENT_SCRYPT)

# Round-3 fix (security-M1): a short, bounded wait for a scrypt slot
# before answering 503 — see the module docstring for why a bounded queue
# here is safe rather than the DoS surface an *unbounded* one would be.
_SCRYPT_ACQUIRE_TIMEOUT = 1.0


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
    # Round-3 fix (security-M1): a `Retry-After` hint on the 503 — the
    # facade's own bounded wait (`_SCRYPT_ACQUIRE_TIMEOUT`) already gives a
    # concrete sense of scale for "shortly".
    return NetnlHTTPError(
        503,
        "overloaded",
        "too many concurrent authentication checks; try again shortly",
        headers={"Retry-After": "1"},
    )


def _guarded_scrypt(fn, *args):
    """Run a scrypt-costing callable behind `_scrypt_semaphore`.

    Round-3 fix (security-M1): waits up to `_SCRYPT_ACQUIRE_TIMEOUT` for a
    slot rather than failing immediately — see the module docstring for why
    a *bounded* wait here does not reopen the DoS this cap exists to close.
    Only sustained saturation (the cap still full after the wait) answers
    503 `overloaded`.
    """
    if not _scrypt_semaphore.acquire(timeout=_SCRYPT_ACQUIRE_TIMEOUT):
        raise _overloaded()
    try:
        return fn(*args)
    finally:
        _scrypt_semaphore.release()


def _parse_basic_auth(header: str | None) -> tuple[str, str] | None:
    # Round-3 fix (reviewer-m8): RFC 7617 defines the auth-scheme token
    # (`Basic`) as case-insensitive — `basic`, `BASIC`, `Basic` must all be
    # accepted. Only the scheme token itself is case-folded; the
    # credentials after it are decoded and compared byte-for-byte,
    # unaffected by this.
    if not header or len(header) < 6 or header[:6].lower() != "basic ":
        return None
    token = header[6:]
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

    Round-3 fix (security-L1): the fallback (`request.url.path`) is just as
    attacker-controlled as a raw username — put through the very same
    sanitizer/cap (`_sanitize_username`, despite its name: it is a generic
    printable-and-length-bounded filter) so this defensive branch cannot
    itself become an unbounded or control-character-laden write into
    `audit` if it were ever actually reached.
    """
    route = request.scope.get("route")
    path = getattr(route, "path", None)
    if isinstance(path, str):
        return path
    return _sanitize_username(request.url.path)


# Round-2 fix (finding 2): failed-auth audit records, aggregated in memory
# per (sanitised username or `None`, route) per wall-clock minute, so that
# an arbitrarily large burst of failures — the exact scenario finding 1
# is about bounding on the CPU/memory side — writes at most one audit row
# per distinct key per minute instead of one row per failure. Without
# this, the audit table itself would be exactly the kind of unbounded
# write sink finding 2 warns against: a flood of bad-credential requests
# would grow it as fast as an attacker can send them.
#
# Round-3 fix (security-H1a): a per-(username, route) bucket with *no cap
# on the number of distinct keys* defeats its own purpose the moment an
# attacker cycles through unique, throwaway usernames instead of retrying
# the same one — every unique username within the same minute mints its
# own bucket, so the dict (and, once flushed, `audit`) grows exactly as
# fast as the attacker can invent new usernames: measured at ~5.5M audit
# rows/day for that pattern, the opposite of the "bounded" claim this
# aggregator exists to make true. `_MAX_BUCKETS` puts a hard ceiling on the
# number of distinct buckets live at once; once reached, a *new* key for a
# route that already has buckets collapses into a single per-route
# overflow bucket (`_OVERFLOW_USERNAME`, route) with its own running
# count, instead of minting bucket number `_MAX_BUCKETS + 1`. The number of
# overflow buckets is bounded by this facade's (small, fixed) number of
# routes, so both the dict's size and the audit rows a single minute can
# ever produce are bounded by `_MAX_BUCKETS` plus that route count —
# never by how many distinct usernames an attacker tries. An operator
# reading an overflow row must not read its `credential` value
# (`_OVERFLOW_USERNAME`) as a real username, and more generally must never
# read *any* `auth-failure` row's `credential` as trustworthy tenant
# attribution: it is attacker-supplied input from the `Authorization`
# header of a request that, by definition, failed to authenticate — see
# design.md, "Audit".
#
# Round-3 fix (security-H1b): the previous flush ran one fsync'd,
# autocommit `INSERT` per stale bucket *while holding `_auth_failure_lock`*
# — measured at ~5.5s of total auth-processing stall for 10k buckets,
# repeatable every minute. The lock now guards only the (cheap, in-memory)
# pop of stale entries; every popped entry is then written in a single
# `BEGIN IMMEDIATE` + `executemany` transaction, outside the lock, so a
# large flush no longer serialises every other authentication attempt on
# the process behind it (measured: <100ms for 10k rows, one transaction).
#
# Round-3 fix (security-H1c): the audit row's `at` is the bucket's own
# `minute` (the failure window), not the flush's wall-clock time — a
# bucket flushed minutes or hours late (only a *later* failure or
# authenticated call triggers a flush) previously back-dated to whenever
# that trigger happened, silently shifting every failure timestamp
# forward.
#
# Round-3 fix (security-H1d): the whole flush runs in `try`/`except` — a
# failing write (e.g. a concurrent `BEGIN IMMEDIATE` from `prune`, or any
# other transient SQLite busy/lock condition) is logged with the
# aggregated counts and dropped, never re-raised. This function runs
# unconditionally on *every* request via `_sweep_stale_auth_failure_
# buckets` — including a perfectly valid, successfully-authenticated one —
# so letting a write failure here propagate would turn a good credential
# into a spurious 500.
#
# Bounding the aggregator's own memory, not just the audit table: every
# call sweeps out (and flushes to `audit`) every bucket whose minute is
# not the current one, before touching the current bucket. That caps the
# dict's size at "distinct (username, route) keys seen in the current
# one-minute window" (further capped by `_MAX_BUCKETS`, see above),
# regardless of how many distinct usernames were ever tried across all
# prior windows.
#
# A trade-off worth stating: the final window of an attack (or of the
# facade's lifetime) is only flushed when a *later* failure arrives to
# trigger the sweep — an attack that stops cold leaves its last <60s of
# tally unwritten. Acceptable: the goal is a detection signal for
# sustained abuse, not a byte-perfect count of a burst that already ended.
_auth_failure_lock = threading.Lock()
_auth_failure_buckets: dict[tuple[str | None, str], tuple[int, int]] = {}

# Round-3 fix (security-H1a): see the block comment above.
_MAX_BUCKETS = 512
_OVERFLOW_USERNAME = "<other>"


def _current_minute(now: datetime) -> int:
    return int(now.astimezone(timezone.utc).timestamp() // 60)


def _pop_stale_auth_failure_buckets_locked(
    current_minute: int,
) -> list[tuple[tuple[str | None, str], int, int]]:
    """Caller holds `_auth_failure_lock`. Pops every bucket whose minute is
    not `current_minute` and returns `(key, minute, count)` for each — pure
    in-memory dict work, no I/O, so the lock is held only for as long as
    that takes (round-3 fix, security-H1b), never for a database write.
    """
    stale_keys = [
        key for key, (minute, _count) in _auth_failure_buckets.items() if minute != current_minute
    ]
    return [(key, *_auth_failure_buckets.pop(key)) for key in stale_keys]


def _write_auth_failure_batch(
    conn: sqlite3.Connection,
    entries: list[tuple[tuple[str | None, str], int, int]],
) -> None:
    """Write every popped bucket in one transaction (round-3 fix,
    security-H1b) — never while `_auth_failure_lock` is held, and never
    raising (security-H1d): a failure here is logged, with the aggregated
    counts still visible in the log line, and the window's tally is
    dropped rather than retried or allowed to fail the caller's request.
    """
    if not entries:
        return
    rows = []
    for (username, path), minute, count in entries:
        # Round-3 fix (security-H1c): the failure window's own time, not
        # whenever this flush happens to run.
        at = store.utcnow_iso(lambda m=minute: datetime.fromtimestamp(m * 60, timezone.utc))
        # Round-3 fix (security-H1 / reviewer-L4): the count lives in
        # `detail`, not `domain_count` — `domain_count` is the number of
        # domains in a *submission*, an unrelated concept an `auth-failure`
        # row must not repurpose.
        rows.append((at, username, "auth-failure", None, None, f"{path} failures={count}"))
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.executemany(
            "INSERT INTO audit (at, credential, event, facade_id, domain_count, detail) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            rows,
        )
        conn.execute("COMMIT")
    except Exception as exc:  # sqlite3.OperationalError and friends
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        total_failures = sum(count for _key, _minute, count in entries)
        _logger.warning(
            "failed to persist %d aggregated auth-failure bucket(s) covering %d failed "
            "authentication attempt(s); this window's tally is dropped, not retried: %s",
            len(entries),
            total_failures,
            exc,
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
        popped = _pop_stale_auth_failure_buckets_locked(current_minute)
    _write_auth_failure_batch(conn, popped)


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
        popped = _pop_stale_auth_failure_buckets_locked(current_minute)
        if key not in _auth_failure_buckets and len(_auth_failure_buckets) >= _MAX_BUCKETS:
            # Round-3 fix (security-H1a): the cap is reached and this is a
            # brand new key — fold it into this route's overflow bucket
            # rather than growing the dict further.
            key = (_OVERFLOW_USERNAME, path)
        existing = _auth_failure_buckets.get(key)
        if existing is None or existing[0] != current_minute:
            _auth_failure_buckets[key] = (current_minute, 1)
        else:
            _auth_failure_buckets[key] = (current_minute, existing[1] + 1)
    _write_auth_failure_batch(conn, popped)


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
