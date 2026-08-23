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

_TERMINAL_STATUSES = {"done", "error", "cancelled"}

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
    domain_count INTEGER
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


def connect(path: str | pathlib.Path) -> sqlite3.Connection:
    """Open one connection to the database file.

    Round-1 fix (B1): callers MUST open one of these per request and close
    it when done (see `get_conn` below) — never share a connection across
    threads. `check_same_thread` is left at its safe default (`True`): a
    shared `check_same_thread=False` connection let CPython's per-connection
    statement cache and cursor state interleave rows between concurrent
    requests, leaking one tenant's row (and `upstream_id`) to another. WAL
    mode makes many short-lived connections cheap.
    """
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Round-1 fix (m8): create the file mode 0600 (owner-only) before any
    # data is written — it holds password hashes, the id-map and the audit
    # trail. `os.open`'s mode only takes effect when the file is actually
    # created; opening an existing file leaves its mode untouched.
    fd = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o600)
    os.close(fd)
    conn = sqlite3.connect(str(path), isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def get_conn(request: Request):
    """FastAPI dependency: one connection per request, closed when the
    request is done. Multiple `Depends(get_conn)` in the same request
    (directly, or via `auth.authenticate`) resolve to the very same
    connection — FastAPI caches dependencies per request — so this is still
    exactly one connection, never a connection shared across requests or
    threads (design.md, "Concurrency and storage").
    """
    conn = connect(request.app.state.settings.db)
    try:
        yield conn
    finally:
        conn.close()


def migrate(conn: sqlite3.Connection) -> None:
    """Idempotent: safe to call on every startup."""
    conn.executescript(_SCHEMA)
    conn.executescript(_CREATE_TRIGGERS)


# --- audit -------------------------------------------------------------

def record_audit(
    conn: sqlite3.Connection,
    *,
    at: str,
    credential: str | None,
    event: str,
    facade_id: str | None = None,
    domain_count: int | None = None,
) -> None:
    """The only place in this package that writes to `audit`.

    (The prune path in `retention.py` deletes old rows via a dedicated,
    trigger-suspending transaction — see design.md's audit section.)
    """
    conn.execute(
        "INSERT INTO audit (at, credential, event, facade_id, domain_count) "
        "VALUES (?, ?, ?, ?, ?)",
        (at, credential, event, facade_id, domain_count),
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
    placeholders = ",".join("?" for _ in _TERMINAL_STATUSES)
    rows = conn.execute(
        f"SELECT * FROM requests WHERE credential_id = ? "
        f"AND last_status NOT IN ({placeholders})",
        (credential_id, *sorted(_TERMINAL_STATUSES)),
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


def list_credentials(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(conn.execute("SELECT * FROM credentials ORDER BY username").fetchall())


def find_credential(conn: sqlite3.Connection, username: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM credentials WHERE username = ?", (username,)
    ).fetchone()
