"""SQLite storage for the facade: credentials, requests, append-only audit.

Single file, WAL mode. All timestamps are `utcnow_iso()`-formatted strings
(fixed `+00:00` offset) so lexicographic comparison equals chronological
comparison. Every function that compares "now" against stored data accepts
an injectable `now` for tests.
"""

from __future__ import annotations

import os
import pathlib
import sqlite3
from datetime import datetime, timezone
from typing import Callable

from fastapi import Request

from internetnl_cli.client import is_valid_request_id

from netnl.errors import NetnlHTTPError

# Public (not `_`-prefixed): `netnl.demo` needs this set too, to answer a
# status poll for an already-terminal row straight from the store rather
# than re-contacting upstream for a result that cannot change any more
# (builder-review fix M2).
TERMINAL_STATUSES = {"done", "error", "cancelled"}

# A row reserved inside the atomic reserve-then-submit transaction (see
# `reserve_submission` in `limits.py`) before upstream has been contacted.
# Not a terminal status: it counts toward concurrency and is retrievable
# only by its owner (design.md, "Concurrency and storage").
RESERVING = "reserving"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS credentials (
    id INTEGER PRIMARY KEY,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    salt TEXT NOT NULL,
    created_at TEXT NOT NULL,
    revoked_at TEXT
);

CREATE TABLE IF NOT EXISTS requests (
    id INTEGER PRIMARY KEY,
    facade_id TEXT NOT NULL UNIQUE,
    upstream_id TEXT,
    credential_id INTEGER NOT NULL REFERENCES credentials(id),
    request_type TEXT NOT NULL,
    domain_count INTEGER NOT NULL,
    submitted_at TEXT NOT NULL,
    last_status TEXT NOT NULL,
    finished_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_requests_credential_status
    ON requests (credential_id, last_status);

CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY,
    at TEXT NOT NULL,
    credential TEXT,
    event TEXT NOT NULL,
    facade_id TEXT,
    domain_count INTEGER,
    -- Round-2 fix (findings 2 and 5): free-form, event-specific context —
    -- the HTTP route for an `auth-failure` event, or the original
    -- `submitted_at` for a `reserving-pruned` event — that does not need
    -- its own column per event type. Never holds a password or any other
    -- credential secret.
    detail TEXT
);

CREATE INDEX IF NOT EXISTS idx_audit_credential_event_at
    ON audit (credential, event, at);
"""

_CREATE_TRIGGERS = """
CREATE TRIGGER IF NOT EXISTS audit_no_update
BEFORE UPDATE ON audit
BEGIN
    SELECT RAISE(ABORT, 'audit is append-only');
END;

CREATE TRIGGER IF NOT EXISTS audit_no_delete
BEFORE DELETE ON audit
BEGIN
    SELECT RAISE(ABORT, 'audit is append-only');
END;
"""


def utcnow_iso(now: Callable[[], datetime] | None = None) -> str:
    moment = now() if now is not None else datetime.now(timezone.utc)
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds")


def connect(path: str | pathlib.Path, *, allow_cross_thread: bool = False) -> sqlite3.Connection:
    """Open one connection to the database file.

    Round-1 fix (B1): callers MUST open one of these per request and close
    it when done (see `get_conn` below) — never share a connection across
    *requests*. That rule is about sharing a connection between independent
    requests/tenants and is unaffected by `allow_cross_thread` below — see
    that parameter's docstring for the distinct, narrower thing it relaxes.

    `allow_cross_thread` (round-4 fix, N1) defaults to `False`, i.e.
    sqlite3's own safe default `check_same_thread=True`. Every caller
    *except* `get_conn` (admin.py, the schema-migration connection in
    api.py's `create_app`, retention/prune, and every test that opens its
    own inspection connection) opens a connection and uses it entirely from
    the one thread that opened it — no threadpool involved — so the
    strict default is exactly right for them and must stay that way: this
    parameter exists to be passed `True` from exactly one call site
    (`get_conn`), never from a connection any of these other callers hand
    out or share.

    Why `get_conn` needs it: FastAPI resolves a sync generator dependency
    (`get_conn` is one) by running its body up to `yield` inside one
    `run_in_threadpool` call; the sync route handler and any other sync
    `Depends` that receive the yielded connection (e.g.
    `auth.authenticate`) run inside a *separate* `run_in_threadpool` call;
    the generator's cleanup after the response runs inside a third. Each of
    those three can land on a different real OS worker thread from
    anyio's threadpool — `TestClient` does not reproduce this (its portal
    serialises the dispatch), but a real uvicorn server does. With
    `check_same_thread=True` (the default `connect()` still uses
    everywhere else), using the connection from a worker thread other than
    the one that opened it raises sqlite3's `ProgrammingError`, surfaced to
    the caller as a generic 500 — measured on a real uvicorn server: 0
    failures at 2 concurrent requests, 14/24 at 4, 85/96 at 16 (see
    `tests/netnl/test_netnl_real_server.py`).

    This is safe to relax *only* for this one connection, and only because
    of what "cross-thread" means here: the connection is still never used
    by two threads *at the same time* — FastAPI always finishes one
    `run_in_threadpool` call (and, with it, whatever use it made of the
    connection) before starting the next one for the *same* request, so
    uses are sequential, just not thread-affine. That is a fundamentally
    different shape from the case the round-1 comment above forbids — one
    connection genuinely *shared and used concurrently across multiple
    requests* — which is what reintroduced the statement-cache/cursor-
    interleaving cross-tenant leak that fix closed. `allow_cross_thread`
    must never be set on a connection more than one request could ever
    touch.
    """
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Round-1 fix (m8): create the file mode 0600 (owner-only) before any
    # data is written — it holds password hashes, the id-map and the audit
    # trail. `os.open`'s mode only takes effect when the file is actually
    # created; opening an existing file leaves its mode untouched.
    fd = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o600)
    os.close(fd)
    conn = sqlite3.connect(
        str(path), isolation_level=None, check_same_thread=not allow_cross_thread
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    # Reviewer-minor (round-2): under write contention (two credentials'
    # `BEGIN IMMEDIATE` reservation transactions overlapping), SQLite's
    # default busy behaviour is to fail immediately with SQLITE_BUSY, which
    # would otherwise surface as a spurious 500 instead of the connection
    # simply waiting its turn for the (very short-lived) write lock. 5000ms
    # comfortably covers a reservation transaction's lifetime.
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def get_conn(request: Request):
    """FastAPI dependency: one connection per request, closed when the
    request is done. Multiple `Depends(get_conn)` in the same request
    (directly, or via `auth.authenticate`) resolve to the very same
    connection — FastAPI caches dependencies per request — so this is still
    exactly one connection, never a connection shared across requests
    (design.md, "Concurrency and storage").

    Round-4 fix (N1): opened with `allow_cross_thread=True` — the
    dependency's `yield`, the route handler that uses the connection, and
    this generator's own cleanup can each run on a different real worker
    thread (see `connect`'s docstring for exactly why that is safe here,
    and only here). Without this, a real (non-`TestClient`) uvicorn server
    under concurrent load intermittently raised sqlite3's
    `ProgrammingError` as a generic 500 the instant two of those three
    steps landed on different threads.
    """
    conn = connect(request.app.state.settings.db, allow_cross_thread=True)
    try:
        yield conn
    finally:
        conn.close()


def migrate(conn: sqlite3.Connection) -> None:
    """Idempotent: safe to call on every startup."""
    conn.executescript(_SCHEMA)
    _ensure_audit_detail_column(conn)
    conn.executescript(_CREATE_TRIGGERS)


def _ensure_audit_detail_column(conn: sqlite3.Connection) -> None:
    """`CREATE TABLE IF NOT EXISTS` above is a no-op against a database
    created before the `detail` column existed — this adds it to an
    existing `audit` table, once, so an operator upgrading in place does
    not need a manual migration step.

    Round-3 fix (security-L2): `migrate()` runs on every process startup
    (see `api.py`), so two processes starting at once can both see
    `detail` missing and both attempt the `ALTER`; SQLite lets only one
    through and raises `OperationalError` (typically "duplicate column
    name") for the other. Re-checking `PRAGMA table_info` after a caught
    `OperationalError` distinguishes that harmless race (the column is now
    there — someone else's `ALTER` won) from a genuine failure (the column
    is still missing — re-raise).
    """
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(audit)")}
    if "detail" in columns:
        return
    try:
        conn.execute("ALTER TABLE audit ADD COLUMN detail TEXT")
    except sqlite3.OperationalError:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(audit)")}
        if "detail" not in columns:
            raise


# --- audit -------------------------------------------------------------

def record_audit(
    conn: sqlite3.Connection,
    *,
    at: str,
    credential: str | None,
    event: str,
    facade_id: str | None = None,
    domain_count: int | None = None,
    detail: str | None = None,
) -> None:
    """The single-row path for writing to `audit`.

    (The prune path in `retention.py` deletes old rows via a dedicated,
    trigger-suspending transaction — see design.md's audit section.
    `netnl.auth._write_auth_failure_batch` is the one other sanctioned
    writer: it batches many aggregated auth-failure rows into a single
    `executemany` INSERT rather than one call per row here, for the same
    column set and append-only guarantees — see design.md, "Audit".)
    """
    conn.execute(
        "INSERT INTO audit (at, credential, event, facade_id, domain_count, detail) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (at, credential, event, facade_id, domain_count, detail),
    )


# --- requests ------------------------------------------------------------

def insert_request(
    conn: sqlite3.Connection,
    *,
    facade_id: str,
    upstream_id: str,
    credential_id: int,
    request_type: str,
    domain_count: int,
    submitted_at: str,
    last_status: str,
) -> None:
    conn.execute(
        "INSERT INTO requests "
        "(facade_id, upstream_id, credential_id, request_type, domain_count, "
        " submitted_at, last_status, finished_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, NULL)",
        (
            facade_id,
            upstream_id,
            credential_id,
            request_type,
            domain_count,
            submitted_at,
            last_status,
        ),
    )


def insert_reserving_request(
    conn: sqlite3.Connection,
    *,
    facade_id: str,
    credential_id: int,
    request_type: str,
    domain_count: int,
    submitted_at: str,
) -> None:
    """Round-1 fix (B2): the "reserve" half of reserve-then-submit — issued
    inside the caller's `BEGIN IMMEDIATE` transaction, before upstream is
    ever contacted. `upstream_id` is NULL until `finalize_reservation`.
    """
    conn.execute(
        "INSERT INTO requests "
        "(facade_id, upstream_id, credential_id, request_type, domain_count, "
        " submitted_at, last_status, finished_at) "
        "VALUES (?, NULL, ?, ?, ?, ?, ?, NULL)",
        (facade_id, credential_id, request_type, domain_count, submitted_at, RESERVING),
    )


def finalize_reservation(
    conn: sqlite3.Connection, facade_id: str, *, upstream_id: str, status: str
) -> None:
    """The "submit" half: called after upstream accepted the request,
    outside the reservation transaction/lock."""
    conn.execute(
        "UPDATE requests SET upstream_id = ?, last_status = ? WHERE facade_id = ?",
        (upstream_id, status, facade_id),
    )


def get_request(conn: sqlite3.Connection, facade_id: str) -> sqlite3.Row | None:
    row = conn.execute(
        "SELECT * FROM requests WHERE facade_id = ?", (facade_id,)
    ).fetchone()
    return row


def get_request_for_credential(
    conn: sqlite3.Connection, facade_id: str, credential_id: int
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM requests WHERE facade_id = ? AND credential_id = ?",
        (facade_id, credential_id),
    ).fetchone()


def owned_request_or_404(
    conn: sqlite3.Connection, facade_id: str, credential_id: int
) -> sqlite3.Row:
    """A foreign or malformed id is indistinguishable from an unknown one —
    both are the same 404, so credential B can never tell credential A's
    request exists (design.md, "Tenant isolation").

    Shared by every owner-scoped route: the authenticated v2 subset
    (`GET /requests/{id}`, `GET /requests/{id}/results`) and the anonymous
    demo family (`GET /demo/requests/{id}`, `GET
    /demo/requests/{id}/results`, scoped to the demo credential) both call
    this — lifted here, unchanged in behaviour, from what was previously a
    private helper in `api.py`.
    """
    if is_valid_request_id(facade_id):
        row = get_request_for_credential(conn, facade_id, credential_id)
        if row is not None:
            return row
    raise NetnlHTTPError(404, "unknown-request", "this request_id does not exist for the user")


def update_status(
    conn: sqlite3.Connection,
    facade_id: str,
    status: str,
    finished_at: str | None,
) -> None:
    conn.execute(
        "UPDATE requests SET last_status = ?, finished_at = ? WHERE facade_id = ?",
        (status, finished_at, facade_id),
    )


def non_terminal_requests(conn: sqlite3.Connection, credential_id: int) -> list[sqlite3.Row]:
    placeholders = ",".join("?" for _ in TERMINAL_STATUSES)
    rows = conn.execute(
        f"SELECT * FROM requests WHERE credential_id = ? "
        f"AND last_status NOT IN ({placeholders})",
        (credential_id, *sorted(TERMINAL_STATUSES)),
    ).fetchall()
    return list(rows)


def count_submits_since(conn: sqlite3.Connection, credential: str, cutoff_iso: str) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM audit WHERE credential = ? AND event = 'submit' AND at >= ?",
        (credential, cutoff_iso),
    ).fetchone()
    return int(row["n"])


# --- credentials -----------------------------------------------------------

def add_credential(
    conn: sqlite3.Connection,
    *,
    username: str,
    password_hash: str,
    salt: str,
    created_at: str,
) -> int:
    cur = conn.execute(
        "INSERT INTO credentials (username, password_hash, salt, created_at, revoked_at) "
        "VALUES (?, ?, ?, ?, NULL)",
        (username, password_hash, salt, created_at),
    )
    return int(cur.lastrowid)


def revoke_credential(conn: sqlite3.Connection, username: str, revoked_at: str) -> bool:
    cur = conn.execute(
        "UPDATE credentials SET revoked_at = ? WHERE username = ? AND revoked_at IS NULL",
        (revoked_at, username),
    )
    return cur.rowcount > 0


def reissue_credential(
    conn: sqlite3.Connection, username: str, *, password_hash: str, salt: str
) -> bool:
    """Builder-review fix (S6=B3): re-key an *existing* row in place — a
    fresh password/salt and `revoked_at` cleared — rather than requiring a
    brand new row. Unlike `add_credential`, this works whether the row is
    currently revoked or not: it is the operator's one lever to turn a
    kill-switched surface (`netnl-admin user revoke ...`) back on without
    running into `add`'s own "user already exists" refusal. Returns `False`
    when no row with this username exists at all (nothing to reissue).
    """
    cur = conn.execute(
        "UPDATE credentials SET password_hash = ?, salt = ?, revoked_at = NULL "
        "WHERE username = ?",
        (password_hash, salt, username),
    )
    return cur.rowcount > 0


def list_credentials(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(conn.execute("SELECT * FROM credentials ORDER BY username").fetchall())


def find_credential(conn: sqlite3.Connection, username: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM credentials WHERE username = ?", (username,)
    ).fetchone()
