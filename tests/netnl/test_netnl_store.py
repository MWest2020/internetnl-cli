from __future__ import annotations

import sqlite3
import stat

import pytest

from netnl import store


@pytest.fixture
def conn(tmp_path):
    c = store.connect(tmp_path / "netnl.sqlite3")
    store.migrate(c)
    yield c
    c.close()


def test_migrate_is_idempotent(conn):
    store.migrate(conn)
    store.migrate(conn)
    tables = {
        row["name"]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }
    assert {"credentials", "requests", "audit", "supporter_issuance"} <= tables


def test_journal_mode_is_wal(conn):
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"


def test_audit_rejects_update_and_delete(conn):
    store.record_audit(
        conn, at="2026-01-01T00:00:00+00:00", credential="alice", event="submit"
    )
    with pytest.raises(sqlite3.IntegrityError, match="audit is append-only"):
        conn.execute("UPDATE audit SET event = 'x'")
    with pytest.raises(sqlite3.IntegrityError, match="audit is append-only"):
        conn.execute("DELETE FROM audit")
    # Confirm nothing was actually removed or changed.
    row = conn.execute("SELECT event FROM audit").fetchone()
    assert row["event"] == "submit"


def test_requests_rows_are_updatable(conn):
    cred_id = store.add_credential(
        conn,
        username="alice",
        password_hash="h",
        salt="s",
        created_at="2026-01-01T00:00:00+00:00",
    )
    store.insert_request(
        conn,
        facade_id="f" * 32,
        upstream_id="u" * 32,
        credential_id=cred_id,
        request_type="web",
        domain_count=1,
        submitted_at="2026-01-01T00:00:00+00:00",
        last_status="registering",
    )
    store.update_status(conn, "f" * 32, "done", "2026-01-01T00:05:00+00:00")
    row = store.get_request(conn, "f" * 32)
    assert row["last_status"] == "done"
    assert row["finished_at"] == "2026-01-01T00:05:00+00:00"


def test_get_request_missing_returns_none(conn):
    assert store.get_request(conn, "0" * 32) is None


def test_get_request_for_credential_filters_by_owner(conn):
    a = store.add_credential(
        conn, username="a", password_hash="h", salt="s", created_at="2026-01-01T00:00:00+00:00"
    )
    b = store.add_credential(
        conn, username="b", password_hash="h", salt="s", created_at="2026-01-01T00:00:00+00:00"
    )
    store.insert_request(
        conn,
        facade_id="f" * 32,
        upstream_id="u" * 32,
        credential_id=a,
        request_type="web",
        domain_count=1,
        submitted_at="2026-01-01T00:00:00+00:00",
        last_status="registering",
    )
    assert store.get_request_for_credential(conn, "f" * 32, a) is not None
    assert store.get_request_for_credential(conn, "f" * 32, b) is None


def test_non_terminal_requests(conn):
    cred_id = store.add_credential(
        conn, username="a", password_hash="h", salt="s", created_at="2026-01-01T00:00:00+00:00"
    )
    store.insert_request(
        conn,
        facade_id="1" * 32,
        upstream_id="u" * 32,
        credential_id=cred_id,
        request_type="web",
        domain_count=1,
        submitted_at="2026-01-01T00:00:00+00:00",
        last_status="running",
    )
    store.insert_request(
        conn,
        facade_id="2" * 32,
        upstream_id="v" * 32,
        credential_id=cred_id,
        request_type="web",
        domain_count=1,
        submitted_at="2026-01-01T00:00:00+00:00",
        last_status="done",
    )
    non_terminal = store.non_terminal_requests(conn, cred_id)
    assert [row["facade_id"] for row in non_terminal] == ["1" * 32]


def test_count_submits_since_only_counts_submit_events_in_window(conn):
    store.record_audit(conn, at="2026-01-01T00:00:00+00:00", credential="a", event="submit")
    store.record_audit(conn, at="2026-01-01T00:30:00+00:00", credential="a", event="submit")
    store.record_audit(conn, at="2026-01-01T00:45:00+00:00", credential="a", event="user-add")
    store.record_audit(conn, at="2025-12-31T23:00:00+00:00", credential="a", event="submit")
    store.record_audit(conn, at="2026-01-01T00:15:00+00:00", credential="other", event="submit")

    count = store.count_submits_since(conn, "a", "2026-01-01T00:00:00+00:00")
    assert count == 2


def test_duplicate_username_raises_integrity_error(conn):
    store.add_credential(
        conn, username="a", password_hash="h", salt="s", created_at="2026-01-01T00:00:00+00:00"
    )
    with pytest.raises(sqlite3.IntegrityError):
        store.add_credential(
            conn,
            username="a",
            password_hash="h2",
            salt="s2",
            created_at="2026-01-01T00:00:00+00:00",
        )


def test_revoke_and_find_credential(conn):
    store.add_credential(
        conn, username="a", password_hash="h", salt="s", created_at="2026-01-01T00:00:00+00:00"
    )
    assert store.revoke_credential(conn, "a", "2026-01-02T00:00:00+00:00") is True
    row = store.find_credential(conn, "a")
    assert row["revoked_at"] == "2026-01-02T00:00:00+00:00"
    assert store.revoke_credential(conn, "unknown", "2026-01-02T00:00:00+00:00") is False


def test_list_credentials(conn):
    store.add_credential(
        conn, username="b", password_hash="h", salt="s", created_at="2026-01-01T00:00:00+00:00"
    )
    store.add_credential(
        conn, username="a", password_hash="h", salt="s", created_at="2026-01-01T00:00:00+00:00"
    )
    names = [row["username"] for row in store.list_credentials(conn)]
    assert names == ["a", "b"]


def test_db_file_created_with_mode_0600(tmp_path):
    """Round-1 fix (m8): the database file holds password hashes, the
    id-map and the audit trail — it must never be world/group-readable."""
    path = tmp_path / "perm.sqlite3"
    c = store.connect(path)
    c.close()
    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == 0o600


def test_existing_db_file_mode_is_left_untouched(tmp_path):
    """Opening an already-existing file must not silently change a mode an
    operator deliberately set."""
    path = tmp_path / "existing.sqlite3"
    path.touch()
    path.chmod(0o640)
    c = store.connect(path)
    c.close()
    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == 0o640


def test_utcnow_iso_is_lexicographically_comparable():
    from datetime import datetime, timezone

    early = store.utcnow_iso(lambda: datetime(2026, 1, 1, tzinfo=timezone.utc))
    late = store.utcnow_iso(lambda: datetime(2026, 1, 2, tzinfo=timezone.utc))
    assert early < late
    assert early.endswith("+00:00")


# --- round-3: migrating a pre-round-2 database in place --------------------


def _create_pre_round2_schema(path) -> None:
    """The shape a database created before the `audit.detail` column
    existed would actually have: no `detail` column, but the append-only
    triggers already in place (they predate `detail`)."""
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(
            """
            CREATE TABLE credentials (
                id INTEGER PRIMARY KEY,
                username TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                salt TEXT NOT NULL,
                created_at TEXT NOT NULL,
                revoked_at TEXT
            );
            CREATE TABLE requests (
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
            CREATE TABLE audit (
                id INTEGER PRIMARY KEY,
                at TEXT NOT NULL,
                credential TEXT,
                event TEXT NOT NULL,
                facade_id TEXT,
                domain_count INTEGER
            );
            CREATE TRIGGER audit_no_update
            BEFORE UPDATE ON audit
            BEGIN
                SELECT RAISE(ABORT, 'audit is append-only');
            END;
            CREATE TRIGGER audit_no_delete
            BEFORE DELETE ON audit
            BEGIN
                SELECT RAISE(ABORT, 'audit is append-only');
            END;
            """
        )
        conn.execute(
            "INSERT INTO audit (at, credential, event, facade_id, domain_count) "
            "VALUES (?, ?, ?, ?, ?)",
            ("2026-01-01T00:00:00+00:00", "alice", "submit", "f" * 32, 1),
        )
        conn.commit()
    finally:
        conn.close()


def test_migrate_upgrades_pre_round2_schema_in_place(tmp_path):
    """Round-3 fix (reviewer-M6): build a database with the pre-round-2
    schema (`audit` with no `detail` column, triggers already present), run
    `store.migrate` twice (idempotency), and confirm the column and both
    triggers are present and enforcing, with the pre-existing row intact
    and back-filled with `detail = NULL` rather than dropped or rewritten.
    """
    path = tmp_path / "pre-round2.sqlite3"
    _create_pre_round2_schema(path)

    conn = store.connect(path)
    try:
        store.migrate(conn)
        store.migrate(conn)  # idempotent, per its own docstring

        columns = {row["name"] for row in conn.execute("PRAGMA table_info(audit)")}
        assert "detail" in columns

        row = conn.execute("SELECT * FROM audit WHERE credential = 'alice'").fetchone()
        assert row is not None
        assert row["event"] == "submit"
        assert row["facade_id"] == "f" * 32
        assert row["domain_count"] == 1
        assert row["detail"] is None

        with pytest.raises(sqlite3.IntegrityError, match="audit is append-only"):
            conn.execute("UPDATE audit SET event = 'tampered'")
        with pytest.raises(sqlite3.IntegrityError, match="audit is append-only"):
            conn.execute("DELETE FROM audit")
    finally:
        conn.close()


# --- round-3: the `ALTER TABLE ... ADD COLUMN` upgrade tolerates a race ----


def test_ensure_audit_detail_column_tolerates_concurrent_alter_race(tmp_path):
    """Round-3 fix (security-L2): `migrate()` runs on every process
    startup, so two processes racing the same upgrade can both see
    `detail` missing and both attempt the `ALTER`; SQLite lets only one
    through. `_ensure_audit_detail_column` must not treat the loser's
    `OperationalError` as fatal once the column is actually present.
    """
    from netnl.store import _ensure_audit_detail_column

    path = tmp_path / "alter-race.sqlite3"
    conn = store.connect(path)
    try:
        conn.executescript(
            "CREATE TABLE audit (id INTEGER PRIMARY KEY, at TEXT NOT NULL, credential TEXT, "
            "event TEXT NOT NULL, facade_id TEXT, domain_count INTEGER)"
        )

        class _RacingConnection:
            """Simulates another connection's identical `ALTER` winning
            the race: the column really does get added (so the PRAGMA
            re-check the fix performs actually finds it), but this call
            still sees `OperationalError`, exactly like SQLite's own
            "duplicate column name" for the loser of a real race."""

            def __init__(self, real):
                self._real = real

            def execute(self, sql, *args, **kwargs):
                if isinstance(sql, str) and "ALTER TABLE audit" in sql:
                    self._real.execute(sql)
                    raise sqlite3.OperationalError("duplicate column name: detail")
                return self._real.execute(sql, *args, **kwargs)

            def __getattr__(self, name):
                return getattr(self._real, name)

        _ensure_audit_detail_column(_RacingConnection(conn))  # must not raise

        columns = {row["name"] for row in conn.execute("PRAGMA table_info(audit)")}
        assert "detail" in columns
    finally:
        conn.close()


def test_ensure_audit_detail_column_reraises_a_genuine_failure(tmp_path):
    """A non-race `OperationalError` (the column still genuinely missing
    afterwards) must still propagate — the re-check must not swallow a
    real failure."""
    from netnl.store import _ensure_audit_detail_column

    path = tmp_path / "alter-fail.sqlite3"
    conn = store.connect(path)
    try:
        conn.executescript(
            "CREATE TABLE audit (id INTEGER PRIMARY KEY, at TEXT NOT NULL, credential TEXT, "
            "event TEXT NOT NULL, facade_id TEXT, domain_count INTEGER)"
        )

        class _AlwaysFailingConnection:
            def __init__(self, real):
                self._real = real

            def execute(self, sql, *args, **kwargs):
                if isinstance(sql, str) and "ALTER TABLE audit" in sql:
                    raise sqlite3.OperationalError("disk I/O error")
                return self._real.execute(sql, *args, **kwargs)

            def __getattr__(self, name):
                return getattr(self._real, name)

        with pytest.raises(sqlite3.OperationalError, match="disk I/O error"):
            _ensure_audit_detail_column(_AlwaysFailingConnection(conn))
    finally:
        conn.close()


# --- supporter issuance (openspec/changes/add-supporter-issuance, T3) ------


def test_find_issuance_missing_returns_none(conn):
    assert store.find_issuance(conn, "txn-unknown") is None


def test_insert_and_find_issuance(conn):
    store.insert_issuance(
        conn,
        txn_id="txn-1",
        username="supporter-aaaa1111",
        state="pending",
        attempts=0,
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00",
    )
    row = store.find_issuance(conn, "txn-1")
    assert row is not None
    assert row["username"] == "supporter-aaaa1111"
    assert row["state"] == "pending"
    assert row["attempts"] == 0


def test_txn_id_is_unique(conn):
    store.insert_issuance(
        conn,
        txn_id="txn-dup",
        username="a",
        state="pending",
        attempts=0,
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00",
    )
    with pytest.raises(sqlite3.IntegrityError):
        store.insert_issuance(
            conn,
            txn_id="txn-dup",
            username="b",
            state="pending",
            attempts=0,
            created_at="2026-01-01T00:00:00+00:00",
            updated_at="2026-01-01T00:00:00+00:00",
        )


def test_update_issuance_changes_state_username_and_attempts_but_not_created_at(conn):
    store.insert_issuance(
        conn,
        txn_id="txn-2",
        username="supporter-old",
        state="failed",
        attempts=1,
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00",
    )
    store.update_issuance(
        conn,
        "txn-2",
        username="supporter-new",
        state="pending",
        attempts=1,
        updated_at="2026-01-01T00:05:00+00:00",
    )
    row = store.find_issuance(conn, "txn-2")
    assert row["username"] == "supporter-new"
    assert row["state"] == "pending"
    assert row["created_at"] == "2026-01-01T00:00:00+00:00"
    assert row["updated_at"] == "2026-01-01T00:05:00+00:00"


def test_count_issuances_since_counts_by_created_at_only(conn):
    store.insert_issuance(
        conn,
        txn_id="txn-old",
        username="a",
        state="delivered",
        attempts=0,
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00",
    )
    store.insert_issuance(
        conn,
        txn_id="txn-new",
        username="b",
        state="pending",
        attempts=0,
        created_at="2026-01-01T00:30:00+00:00",
        updated_at="2026-01-01T00:30:00+00:00",
    )
    # A retry of the new row must not double-count it: created_at is
    # untouched by `update_issuance`.
    store.update_issuance(
        conn, "txn-new", username="c", state="failed", attempts=1,
        updated_at="2026-01-01T00:31:00+00:00",
    )

    count = store.count_issuances_since(conn, "2026-01-01T00:15:00+00:00")
    assert count == 1


def test_supporter_issuance_rows_are_updatable_unlike_audit(conn):
    """Unlike `audit`, `supporter_issuance` carries no append-only trigger
    — it must be a plain, updatable table."""
    store.insert_issuance(
        conn,
        txn_id="txn-3",
        username="a",
        state="pending",
        attempts=0,
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00",
    )
    store.update_issuance(
        conn, "txn-3", username="a", state="delivered", attempts=0,
        updated_at="2026-01-01T00:01:00+00:00",
    )
    assert store.find_issuance(conn, "txn-3")["state"] == "delivered"


# --- shared credential minting (netnl/issue.py) -----------------------------


def test_issue_credential_returns_plaintext_and_persists_only_the_hash(conn):
    from netnl import auth, issue

    password = issue.issue_credential(conn, username="alice", created_at="2026-01-01T00:00:00+00:00")
    assert isinstance(password, str) and len(password) > 0

    row = store.find_credential(conn, "alice")
    assert row is not None
    assert row["password_hash"] != password  # never stored in plaintext
    assert auth.verify(row["password_hash"], bytes.fromhex(row["salt"]), password) is True


def test_issue_credential_raises_integrity_error_on_duplicate_username(conn):
    from netnl import issue

    issue.issue_credential(conn, username="bob", created_at="2026-01-01T00:00:00+00:00")
    with pytest.raises(sqlite3.IntegrityError):
        issue.issue_credential(conn, username="bob", created_at="2026-01-01T00:00:00+00:00")
