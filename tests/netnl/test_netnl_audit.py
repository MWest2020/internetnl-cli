from __future__ import annotations

import pathlib
import sqlite3

from fakes import REGISTER_REPLY

from conftest import queue_json


def test_submission_is_audited_before_reply(client, app, fake_opener, tenant):
    conn = app.state.conn
    before = conn.execute(
        "SELECT COUNT(*) AS n FROM audit WHERE event = 'submit'"
    ).fetchone()["n"]
    assert before == 0

    queue_json(fake_opener, REGISTER_REPLY)
    resp = client.post(
        "/requests", json={"type": "web", "domains": ["example.nl"]}, headers=tenant["headers"]
    )
    assert resp.status_code == 200
    facade_id = resp.json()["request"]["request_id"]

    row = conn.execute("SELECT * FROM audit WHERE event = 'submit'").fetchone()
    assert row is not None
    assert row["credential"] == tenant["username"]
    assert row["facade_id"] == facade_id
    assert row["domain_count"] == 1


def test_rejected_submit_leaves_no_submit_record(client, app, fake_opener, tenant):
    # Oversized: rejected before any audit write.
    domains = ["a{}.nl".format(i) for i in range(app.state.settings.max_domains + 1)]
    resp = client.post("/requests", json={"type": "web", "domains": domains}, headers=tenant["headers"])
    assert resp.status_code == 400

    count = app.state.conn.execute(
        "SELECT COUNT(*) AS n FROM audit WHERE event = 'submit'"
    ).fetchone()["n"]
    assert count == 0


def test_direct_update_and_delete_on_audit_fail(app):
    conn = app.state.conn
    conn.execute(
        "INSERT INTO audit (at, credential, event, facade_id, domain_count) VALUES (?, ?, ?, ?, ?)",
        ("2026-01-01T00:00:00+00:00", "x", "submit", "f" * 32, 1),
    )
    try:
        conn.execute("UPDATE audit SET event = 'tampered'")
        assert False, "expected sqlite3.IntegrityError"
    except sqlite3.IntegrityError as exc:
        assert "audit is append-only" in str(exc)
    try:
        conn.execute("DELETE FROM audit")
        assert False, "expected sqlite3.IntegrityError"
    except sqlite3.IntegrityError as exc:
        assert "audit is append-only" in str(exc)


def test_no_update_or_delete_path_on_audit_in_source(app):
    """The only exception is the retention/prune path (built in B6), which
    must suspend the triggers explicitly — grep the current source tree
    (B1-B5) for any other `UPDATE audit` / `DELETE FROM audit` statement.
    """
    src_root = pathlib.Path(__file__).resolve().parents[2] / "src" / "netnl"
    offenders = []
    for path in sorted(src_root.glob("*.py")):
        if path.name == "retention.py":
            continue
        text = path.read_text().upper()
        if "UPDATE AUDIT" in text or "DELETE FROM AUDIT" in text:
            offenders.append(path.name)
    assert offenders == []


def test_no_domain_names_are_stored_anywhere(client, app, fake_opener, tenant):
    queue_json(fake_opener, REGISTER_REPLY)
    resp = client.post(
        "/requests",
        json={"type": "web", "domains": ["never-store-this-domain.example"]},
        headers=tenant["headers"],
    )
    assert resp.status_code == 200

    db_path = pathlib.Path(app.state.settings.db)
    candidates = [db_path, db_path.with_name(db_path.name + "-wal")]
    raw = b"".join(p.read_bytes() for p in candidates if p.exists())
    assert b"never-store-this-domain" not in raw
