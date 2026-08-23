from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from fakes import REGISTER_REPLY

from conftest import queue_json
from netnl import retention, store


def test_prune_removes_expired_requests_and_api_answers_404(
    client, app, fake_opener, tenant, settings, conn
):
    queue_json(fake_opener, REGISTER_REPLY)
    resp = client.post(
        "/requests", json={"type": "web", "domains": ["example.nl"]}, headers=tenant["headers"]
    )
    facade_id = resp.json()["request"]["request_id"]

    old = store.utcnow_iso(lambda: datetime(2000, 1, 1, tzinfo=timezone.utc))
    conn.execute("UPDATE requests SET submitted_at = ? WHERE facade_id = ?", (old, facade_id))

    retention.prune(conn, settings, datetime.now(timezone.utc))

    assert store.get_request(conn, facade_id) is None

    lookup = client.get(f"/requests/{facade_id}", headers=tenant["headers"])
    assert lookup.status_code == 404
    assert lookup.json()["error"]["label"] == "unknown-request"


def test_prune_keeps_recent_audit_and_removes_old(app, settings, conn):
    now = datetime.now(timezone.utc)
    recent = store.utcnow_iso(lambda: now - timedelta(days=1))
    old = store.utcnow_iso(lambda: now - timedelta(days=settings.audit_retention_days + 1))
    store.record_audit(conn, at=recent, credential="a", event="submit")
    store.record_audit(conn, at=old, credential="a", event="submit")

    retention.prune(conn, settings, now)

    ats = [row["at"] for row in conn.execute("SELECT at FROM audit").fetchall()]
    assert recent in ats
    assert old not in ats


def test_prune_writes_its_own_audit_record(app, settings, conn):
    counts = retention.prune(conn, settings, datetime.now(timezone.utc))
    row = conn.execute("SELECT * FROM audit WHERE event = 'prune'").fetchone()
    assert row is not None
    assert row["domain_count"] == counts["requests_deleted"] + counts["audit_deleted"]


def test_manual_delete_on_audit_still_fails_after_a_successful_prune(app, settings, conn):
    retention.prune(conn, settings, datetime.now(timezone.utc))
    with pytest.raises(sqlite3.IntegrityError, match="audit is append-only"):
        conn.execute("DELETE FROM audit")


class _GuardedConnection:
    """Forwards to a real connection, except that `guard(sql)` gets a look
    at every statement first — used to inject a failure at a precise point
    without needing to patch the (immutable) `sqlite3.Connection` type.
    """

    def __init__(self, real: sqlite3.Connection, guard) -> None:
        self._real = real
        self._guard = guard

    def execute(self, sql, *args, **kwargs):
        self._guard(sql)
        return self._real.execute(sql, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._real, name)


def test_trigger_restored_after_prune(app, settings, conn):
    """A failure between DROP TRIGGER and its recreation must not leave the
    append-only guard missing."""
    state = {"raised": False}

    def guard(sql):
        if (
            not state["raised"]
            and isinstance(sql, str)
            and "CREATE TRIGGER IF NOT EXISTS audit_no_delete" in sql
        ):
            state["raised"] = True
            raise RuntimeError("simulated failure between DROP and CREATE")

    proxy = _GuardedConnection(conn, guard)

    with pytest.raises(RuntimeError):
        retention.prune(proxy, settings, datetime.now(timezone.utc))

    # A `DELETE` with nothing to delete never fires a per-row trigger, so
    # give it a row to actually try to remove.
    store.record_audit(
        conn, at="2026-01-01T00:00:00+00:00", credential="a", event="submit"
    )
    with pytest.raises(sqlite3.IntegrityError, match="audit is append-only"):
        conn.execute("DELETE FROM audit")
