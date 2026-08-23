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
    assert {"credentials", "requests", "audit"} <= tables


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
