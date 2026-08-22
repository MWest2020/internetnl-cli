"""SQLite storage for the facade: credentials, requests, append-only audit.

Single file, WAL mode. All timestamps are `utcnow_iso()`-formatted strings
(fixed `+00:00` offset) so lexicographic comparison equals chronological
comparison. Every function that compares "now" against stored data accepts
an injectable `now` for tests.
"""

from __future__ import annotations

import pathlib
import sqlite3
from datetime import datetime, timezone
from typing import Callable

_TERMINAL_STATUSES = {"done", "error", "cancelled"}

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
    upstream_id TEXT NOT NULL,
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
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


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
