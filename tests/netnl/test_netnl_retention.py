from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from fakes import REGISTER_REPLY

from conftest import DEMO_ORIGIN, DEMO_TENANT, queue_json
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
    assert (
        row["domain_count"]
        == counts["requests_deleted"] + counts["reserving_deleted"] + counts["audit_deleted"]
    )


def test_stranded_reservation_older_than_grace_is_pruned_and_frees_slot(settings_env, tmp_path):
    """Round-2 fix (security-LOW, pinned): a `reserving` row whose upstream
    submit never completed must not pin a concurrency slot forever — see
    design.md, "Audit" (reserving-prune), and the spec scenario "A stranded
    reservation frees its slot".
    """
    from netnl.api import create_app
    from starlette.testclient import TestClient
    from conftest import add_test_credential, basic_auth_header
    from fakes import FakeOpener
    from netnl.settings import load

    env = dict(settings_env)
    env["NETNL_MAX_CONCURRENT"] = "1"
    env["NETNL_DB"] = str(tmp_path / "reserving-prune.sqlite3")
    settings = load(env)
    fake_opener = FakeOpener()
    app = create_app(settings, opener=fake_opener)
    add_test_credential(app, "tenant", "secret")
    client = TestClient(app, raise_server_exceptions=False)
    headers = basic_auth_header("tenant", "secret")

    conn = store.connect(settings.db)
    try:
        cred = store.find_credential(conn, "tenant")

        now = datetime.now(timezone.utc)
        stale_submitted_at = store.utcnow_iso(
            lambda: now - timedelta(seconds=settings.reserving_grace_seconds + 1)
        )
        store.insert_reserving_request(
            conn,
            facade_id="s" * 32,
            credential_id=cred["id"],
            request_type="web",
            domain_count=1,
            submitted_at=stale_submitted_at,
        )

        # With only one concurrency slot and the stranded reservation
        # occupying it, a fresh submit is blocked.
        blocked = client.post(
            "/requests", json={"type": "web", "domains": ["example.nl"]}, headers=headers
        )
        assert blocked.status_code == 429
        assert len(fake_opener.calls) == 0

        counts = retention.prune(conn, settings, now)
        assert counts["reserving_deleted"] == 1
        assert store.get_request(conn, "s" * 32) is None

        # The slot is free again: the same submit now succeeds.
        queue_json(fake_opener, REGISTER_REPLY)
        freed = client.post(
            "/requests", json={"type": "web", "domains": ["example.nl"]}, headers=headers
        )
        assert freed.status_code == 200
    finally:
        conn.close()


def test_pruning_a_stranded_reservation_audits_it_with_facade_id(app, settings, conn, tenant):
    """Round-2 fix (finding 5): the row being pruned might correspond to an
    upstream submit that actually succeeded (the facade only ever lost
    track of its `upstream_id`) — the prune must leave enough behind in
    `audit` for an operator to reconstruct which tenant and submission that
    was, even though the run itself becomes unreachable through the facade.
    """
    cred = store.find_credential(conn, tenant["username"])
    now = datetime.now(timezone.utc)
    stale_submitted_at = store.utcnow_iso(
        lambda: now - timedelta(seconds=settings.reserving_grace_seconds + 1)
    )
    store.insert_reserving_request(
        conn,
        facade_id="o" * 32,
        credential_id=cred["id"],
        request_type="web",
        domain_count=3,
        submitted_at=stale_submitted_at,
    )

    counts = retention.prune(conn, settings, now)
    assert counts["reserving_deleted"] == 1

    row = conn.execute(
        "SELECT * FROM audit WHERE event = 'reserving-pruned'"
    ).fetchone()
    assert row is not None
    assert row["facade_id"] == "o" * 32
    assert row["credential"] == tenant["username"]
    assert row["domain_count"] == 3
    assert row["detail"] == stale_submitted_at


def test_stranded_reservation_older_than_result_retention_is_still_audited(
    app, settings, conn, tenant
):
    """Round-3 fix (security-L3): a `reserving` row stranded for *longer*
    than the (much longer) result-retention window used to be deleted by
    the main retention delete before the stranded-reservation audit below
    it ever ran — silently losing the one thing that could reconstruct it.
    The main delete is now scoped to `upstream_id IS NOT NULL`, so a
    `reserving` row is only ever removed by the dedicated stranded-
    reservation path, always preceded by its audit, regardless of how far
    past even the result-retention cutoff it is.
    """
    cred = store.find_credential(conn, tenant["username"])
    now = datetime.now(timezone.utc)
    very_stale_submitted_at = store.utcnow_iso(
        lambda: now - timedelta(days=settings.result_retention_days + 1)
    )
    store.insert_reserving_request(
        conn,
        facade_id="v" * 32,
        credential_id=cred["id"],
        request_type="web",
        domain_count=2,
        submitted_at=very_stale_submitted_at,
    )

    counts = retention.prune(conn, settings, now)

    assert counts["requests_deleted"] == 0  # never touched by the main delete
    assert counts["reserving_deleted"] == 1  # removed by the dedicated path instead
    assert store.get_request(conn, "v" * 32) is None

    row = conn.execute(
        "SELECT * FROM audit WHERE event = 'reserving-pruned' AND facade_id = ?",
        ("v" * 32,),
    ).fetchone()
    assert row is not None  # the audit trail survived, not silently skipped
    assert row["credential"] == tenant["username"]
    assert row["domain_count"] == 2
    assert row["detail"] == very_stale_submitted_at


def test_stranded_reservation_audit_survives_a_missing_credential_row(app, settings, conn):
    """Round-3 fix (reviewer-m12): the stranded-reservation lookup
    `LEFT JOIN`s `credentials` with `COALESCE(..., '<unknown>')` — a
    missing `credentials` row must not silently drop that request from the
    audit trail the way an inner `JOIN` would.
    """
    cred = store.add_credential(
        conn,
        username="temp-owner",
        password_hash="h",
        salt="s",
        created_at=store.utcnow_iso(lambda: datetime.now(timezone.utc)),
    )
    now = datetime.now(timezone.utc)
    stale_submitted_at = store.utcnow_iso(
        lambda: now - timedelta(seconds=settings.reserving_grace_seconds + 1)
    )
    store.insert_reserving_request(
        conn,
        facade_id="m" * 32,
        credential_id=cred,
        request_type="web",
        domain_count=1,
        submitted_at=stale_submitted_at,
    )
    # Simulate the `credentials` row having gone missing by the time prune
    # runs (foreign-key enforcement is not the point under test here).
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute("DELETE FROM credentials WHERE id = ?", (cred,))

    counts = retention.prune(conn, settings, now)
    assert counts["reserving_deleted"] == 1

    row = conn.execute(
        "SELECT * FROM audit WHERE event = 'reserving-pruned' AND facade_id = ?",
        ("m" * 32,),
    ).fetchone()
    assert row is not None  # not silently dropped by an inner JOIN
    assert row["credential"] == "<unknown>"
    assert row["domain_count"] == 1


def test_reservation_within_grace_is_not_pruned(app, settings, conn, tenant):
    cred = store.find_credential(conn, tenant["username"])
    now = datetime.now(timezone.utc)
    fresh_submitted_at = store.utcnow_iso(
        lambda: now - timedelta(seconds=max(settings.reserving_grace_seconds - 60, 0))
    )
    store.insert_reserving_request(
        conn,
        facade_id="f" * 32,
        credential_id=cred["id"],
        request_type="web",
        domain_count=1,
        submitted_at=fresh_submitted_at,
    )

    counts = retention.prune(conn, settings, now)

    assert counts["reserving_deleted"] == 0
    assert store.get_request(conn, "f" * 32) is not None


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


# --- demo retention (openspec/changes/add-demo-run, T7, D11) ----------------


def test_demo_row_older_than_retention_is_pruned(demo_app, demo_settings):
    conn = store.connect(demo_settings.db)
    try:
        demo_cred = store.find_credential(conn, DEMO_TENANT)
        now = datetime.now(timezone.utc)
        stale = store.utcnow_iso(
            lambda: now - timedelta(hours=demo_settings.demo.retention_hours + 1)
        )
        store.insert_request(
            conn,
            facade_id="d" * 32,
            upstream_id="upstream-1",
            credential_id=demo_cred["id"],
            request_type="web",
            domain_count=1,
            submitted_at=stale,
            last_status="done",
        )

        counts = retention.prune(conn, demo_settings, now)

        assert counts["demo_deleted"] == 1
        assert store.get_request(conn, "d" * 32) is None
    finally:
        conn.close()


def test_demo_row_within_retention_is_kept(demo_app, demo_settings):
    conn = store.connect(demo_settings.db)
    try:
        demo_cred = store.find_credential(conn, DEMO_TENANT)
        now = datetime.now(timezone.utc)
        fresh = store.utcnow_iso(
            lambda: now - timedelta(hours=max(demo_settings.demo.retention_hours - 1, 0))
        )
        store.insert_request(
            conn,
            facade_id="e" * 32,
            upstream_id="upstream-2",
            credential_id=demo_cred["id"],
            request_type="web",
            domain_count=1,
            submitted_at=fresh,
            last_status="done",
        )

        counts = retention.prune(conn, demo_settings, now)

        assert counts["demo_deleted"] == 0
        assert store.get_request(conn, "e" * 32) is not None
    finally:
        conn.close()


def test_tenant_row_is_unaffected_by_the_demo_scoped_delete(demo_app, demo_settings):
    """A tenant row 3 days old outlives the demo's 24-hour window by a wide
    margin, but the demo-scoped delete is credential-scoped — it must
    never touch anyone else's rows."""
    from conftest import add_test_credential

    add_test_credential(demo_app, "tenant", "tenant-secret")
    conn = store.connect(demo_settings.db)
    try:
        tenant_cred = store.find_credential(conn, "tenant")
        now = datetime.now(timezone.utc)
        three_days_old = store.utcnow_iso(lambda: now - timedelta(days=3))
        store.insert_request(
            conn,
            facade_id="f" * 32,
            upstream_id="upstream-3",
            credential_id=tenant_cred["id"],
            request_type="web",
            domain_count=1,
            submitted_at=three_days_old,
            last_status="done",
        )

        counts = retention.prune(conn, demo_settings, now)

        assert counts["demo_deleted"] == 0
        assert counts["requests_deleted"] == 0  # well within NETNL_RESULT_RETENTION_DAYS too
        assert store.get_request(conn, "f" * 32) is not None
    finally:
        conn.close()


def test_prune_without_demo_config_has_a_zero_demo_deleted_and_no_other_change(
    app, settings, conn
):
    """`settings.demo is None` (the default) must produce output
    byte-identical to before this change: `demo_deleted` is always present
    in the return value but is 0, and never affects the other counters or
    the `prune` audit record's total.
    """
    counts = retention.prune(conn, settings, datetime.now(timezone.utc))
    assert counts["demo_deleted"] == 0

    row = conn.execute("SELECT * FROM audit WHERE event = 'prune'").fetchone()
    assert row["domain_count"] == (
        counts["requests_deleted"] + counts["reserving_deleted"] + counts["audit_deleted"]
    )


# --- supporter issuance retention (openspec/changes/add-supporter-issuance, T3) --


def test_issuance_row_older_than_audit_cutoff_is_pruned(app, settings, conn):
    now = datetime.now(timezone.utc)
    old = store.utcnow_iso(lambda: now - timedelta(days=settings.audit_retention_days + 1))
    store.insert_issuance(
        conn,
        txn_id="txn-old",
        username="supporter-aaaa0000",
        state="delivered",
        attempts=0,
        created_at=old,
        updated_at=old,
    )

    counts = retention.prune(conn, settings, now)

    assert counts["issuance_deleted"] == 1
    assert store.find_issuance(conn, "txn-old") is None


def test_issuance_row_within_audit_cutoff_is_kept(app, settings, conn):
    now = datetime.now(timezone.utc)
    recent = store.utcnow_iso(lambda: now - timedelta(days=1))
    store.insert_issuance(
        conn,
        txn_id="txn-recent",
        username="supporter-bbbb0000",
        state="delivered",
        attempts=0,
        created_at=recent,
        updated_at=recent,
    )

    counts = retention.prune(conn, settings, now)

    assert counts["issuance_deleted"] == 0
    assert store.find_issuance(conn, "txn-recent") is not None


def test_issuance_deleted_is_zero_without_the_bridge_ever_used(app, settings, conn):
    counts = retention.prune(conn, settings, datetime.now(timezone.utc))
    assert counts["issuance_deleted"] == 0
