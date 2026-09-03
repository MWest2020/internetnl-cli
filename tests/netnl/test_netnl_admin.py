from __future__ import annotations

import io

from starlette.testclient import TestClient

from fakes import REGISTER_REPLY, STATUS_RUNNING, FakeOpener

from conftest import DEMO_ORIGIN, DEMO_TENANT, basic_auth_header, queue_json
from netnl import admin, store
from netnl.api import create_app
from netnl.settings import load


def _run_admin(argv, env):
    stdout, stderr = io.StringIO(), io.StringIO()
    code = admin.main(argv, stdout=stdout, stderr=stderr, env=env)
    return code, stdout.getvalue(), stderr.getvalue()


def test_user_add_prints_exactly_one_password_line(settings_env):
    code, out, err = _run_admin(["user", "add", "alice"], settings_env)
    assert code == 0
    assert err == ""
    lines = out.splitlines()
    assert len(lines) == 1
    assert lines[0]  # non-empty password


def test_generated_password_not_stored_in_plain(settings_env):
    code, out, _ = _run_admin(["user", "add", "alice"], settings_env)
    assert code == 0
    password = out.strip()

    with open(settings_env["NETNL_DB"], "rb") as f:
        raw = f.read()
    assert password.encode() not in raw


def test_generated_password_works_end_to_end(settings_env):
    _, out, _ = _run_admin(["user", "add", "alice"], settings_env)
    password = out.strip()

    settings = load(settings_env)
    fake_opener = FakeOpener()
    queue_json(fake_opener, REGISTER_REPLY)
    app = create_app(settings, opener=fake_opener)
    client = TestClient(app, raise_server_exceptions=False)

    resp = client.post(
        "/requests",
        json={"type": "web", "domains": ["example.nl"]},
        headers=basic_auth_header("alice", password),
    )
    assert resp.status_code == 200


def test_user_add_duplicate_name_fails_without_printing_a_password(settings_env):
    _run_admin(["user", "add", "alice"], settings_env)
    code, out, err = _run_admin(["user", "add", "alice"], settings_env)
    assert code == 1
    assert out == ""
    assert "already exists" in err


def test_user_revoke_blocks_the_same_client_immediately(settings_env):
    _, out, _ = _run_admin(["user", "add", "alice"], settings_env)
    password = out.strip()

    settings = load(settings_env)
    fake_opener = FakeOpener()
    app = create_app(settings, opener=fake_opener)
    client = TestClient(app, raise_server_exceptions=False)
    headers = basic_auth_header("alice", password)

    code, _, _ = _run_admin(["user", "revoke", "alice"], settings_env)
    assert code == 0

    resp = client.get("/requests/" + "a" * 32, headers=headers)
    assert resp.status_code == 401
    assert resp.json()["error"]["label"] == "unauthorised"


def test_user_revoke_unknown_user_fails(settings_env):
    code, out, err = _run_admin(["user", "revoke", "nobody"], settings_env)
    assert code == 1
    assert "nobody" in err


# --- builder-review fix (S6=B3): the kill switch's missing other half -----------


def test_user_reissue_turns_a_revoked_user_back_on(settings_env):
    """The kill switch (`user revoke`) was one-directional: re-enabling a
    revoked username with `user add` failed ("already exists"). `reissue`
    works on the existing (revoked) row: fresh password, `revoked_at`
    cleared, no restart needed.
    """
    _run_admin(["user", "add", "alice"], settings_env)
    _run_admin(["user", "revoke", "alice"], settings_env)

    settings = load(settings_env)
    fake_opener = FakeOpener()
    app = create_app(settings, opener=fake_opener)
    client = TestClient(app, raise_server_exceptions=False)

    # Confirmed revoked before reissuing.
    revoked_resp = client.get(
        "/requests/" + "a" * 32, headers=basic_auth_header("alice", "whatever")
    )
    assert revoked_resp.status_code == 401

    code, out, err = _run_admin(["user", "reissue", "alice"], settings_env)
    assert code == 0
    assert err == ""
    new_password = out.strip()
    assert new_password  # non-empty, printed exactly once

    queue_json(fake_opener, REGISTER_REPLY)
    reissued_resp = client.post(
        "/requests",
        json={"type": "web", "domains": ["example.nl"]},
        headers=basic_auth_header("alice", new_password),
    )
    assert reissued_resp.status_code == 200


def test_user_reissue_works_on_a_never_revoked_user_too(settings_env):
    """`reissue --force` is not conditioned on the row being revoked — it
    re-keys whatever is there, active or not (round-4 builder-review fix,
    N3: without `--force`, reissuing an active row is refused — see
    `test_user_reissue_of_an_active_user_needs_force` below)."""
    _, first_out, _ = _run_admin(["user", "add", "alice"], settings_env)
    first_password = first_out.strip()

    code, second_out, err = _run_admin(["user", "reissue", "--force", "alice"], settings_env)
    assert code == 0
    assert err == ""
    second_password = second_out.strip()
    assert second_password != first_password

    settings = load(settings_env)
    fake_opener = FakeOpener()
    app = create_app(settings, opener=fake_opener)
    client = TestClient(app, raise_server_exceptions=False)

    # The old password no longer works...
    old_resp = client.get(
        "/requests/" + "a" * 32, headers=basic_auth_header("alice", first_password)
    )
    assert old_resp.status_code == 401

    # ...the new one does.
    queue_json(fake_opener, REGISTER_REPLY)
    new_resp = client.post(
        "/requests",
        json={"type": "web", "domains": ["example.nl"]},
        headers=basic_auth_header("alice", second_password),
    )
    assert new_resp.status_code == 200


def test_user_reissue_unknown_user_fails_without_printing_a_password(settings_env):
    code, out, err = _run_admin(["user", "reissue", "nobody"], settings_env)
    assert code == 1
    assert out == ""
    assert "nobody" in err


def test_user_reissue_of_an_active_user_needs_force(settings_env):
    """Round-4 builder-review fix (N3): re-keying a currently *active* row
    immediately invalidates that credential's live password for anyone
    using it — a much bigger blast radius than the intended
    kill-switch-reversal use (re-keying an already-revoked row). Refused
    without `--force`, with an explanation; no password printed, and the
    original password still works."""
    _, first_out, _ = _run_admin(["user", "add", "alice"], settings_env)
    first_password = first_out.strip()

    code, out, err = _run_admin(["user", "reissue", "alice"], settings_env)
    assert code == 1
    assert out == ""
    assert "alice" in err
    assert "--force" in err
    assert "not revoked" in err

    settings = load(settings_env)
    fake_opener = FakeOpener()
    app = create_app(settings, opener=fake_opener)
    client = TestClient(app, raise_server_exceptions=False)
    queue_json(fake_opener, REGISTER_REPLY)
    resp = client.post(
        "/requests",
        json={"type": "web", "domains": ["example.nl"]},
        headers=basic_auth_header("alice", first_password),
    )
    assert resp.status_code == 200  # unchanged: reissue was refused


def test_user_reissue_of_a_revoked_user_does_not_need_force(settings_env):
    _run_admin(["user", "add", "alice"], settings_env)
    _run_admin(["user", "revoke", "alice"], settings_env)

    code, out, err = _run_admin(["user", "reissue", "alice"], settings_env)
    assert code == 0
    assert err == ""
    assert out.strip()


def test_audit_contains_user_reissue(settings_env):
    _run_admin(["user", "add", "alice"], settings_env)
    _run_admin(["user", "revoke", "alice"], settings_env)
    _run_admin(["user", "reissue", "alice"], settings_env)

    conn = store.connect(settings_env["NETNL_DB"])
    events = [
        row["event"] for row in conn.execute("SELECT event FROM audit ORDER BY id").fetchall()
    ]
    assert "user-reissue" in events


def test_audit_user_reissue_detail_records_previous_revoked_at(settings_env):
    """Round-4 builder-review fix (N3): the audit row records what
    `revoked_at` was immediately before the reissue — otherwise the audit
    trail cannot tell "this re-keyed a revoked (kill-switched) row" apart
    from "this re-keyed a live one" after the fact."""
    _run_admin(["user", "add", "alice"], settings_env)
    _run_admin(["user", "revoke", "alice"], settings_env)
    _run_admin(["user", "reissue", "alice"], settings_env)

    conn = store.connect(settings_env["NETNL_DB"])
    row = conn.execute(
        "SELECT detail FROM audit WHERE event = 'user-reissue' AND credential = 'alice'"
    ).fetchone()
    assert row is not None
    assert row["detail"].startswith("previous-revoked-at=")
    assert row["detail"] != "previous-revoked-at=none"

    _run_admin(["user", "add", "bob"], settings_env)
    _run_admin(["user", "reissue", "--force", "bob"], settings_env)
    row_bob = conn.execute(
        "SELECT detail FROM audit WHERE event = 'user-reissue' AND credential = 'bob'"
    ).fetchone()
    assert row_bob is not None
    assert row_bob["detail"] == "previous-revoked-at=none"


def test_reissued_password_not_stored_in_plain(settings_env):
    _run_admin(["user", "add", "alice"], settings_env)
    _run_admin(["user", "revoke", "alice"], settings_env)
    _, out, _ = _run_admin(["user", "reissue", "alice"], settings_env)
    password = out.strip()

    with open(settings_env["NETNL_DB"], "rb") as f:
        raw = f.read()
    assert password.encode() not in raw


def test_demo_kill_switch_round_trips_via_reissue(settings_env):
    """The demo-specific round-trip end-to-end: revoke the demo tenant
    (kill switch engaged, every demo request 503s), then `reissue` it
    (kill switch released, no restart) — see docs/how-to/demo-run.md.
    """
    env = dict(settings_env)
    env["NETNL_DEMO_ENABLED"] = "1"
    env["NETNL_DEMO_ALLOWED_ORIGIN"] = DEMO_ORIGIN
    env["NETNL_DEMO_TENANT"] = DEMO_TENANT

    code, _, _ = _run_admin(["user", "add", DEMO_TENANT], env)
    assert code == 0

    settings = load(env)
    fake_opener = FakeOpener()
    app = create_app(settings, opener=fake_opener)
    client = TestClient(app, raise_server_exceptions=False)
    demo_headers = {"Origin": DEMO_ORIGIN}

    queue_json(fake_opener, REGISTER_REPLY)
    before_revoke = client.post(
        "/demo/requests", json={"domain": "example.nl"}, headers=demo_headers
    )
    assert before_revoke.status_code == 200

    code, _, _ = _run_admin(["user", "revoke", DEMO_TENANT], env)
    assert code == 0

    revoked_resp = client.post(
        "/demo/requests", json={"domain": "second.nl"}, headers=demo_headers
    )
    assert revoked_resp.status_code == 503
    assert revoked_resp.json()["error"]["label"] == "demo-unavailable"

    code, reissue_out, _ = _run_admin(["user", "reissue", DEMO_TENANT], env)
    assert code == 0
    assert reissue_out.strip()  # printed once, thrown away by the operator

    # The reissued tenant's own `refresh_stale_non_terminal` call refreshes
    # the still-non-terminal "example.nl" row before this reservation.
    queue_json(fake_opener, STATUS_RUNNING)
    queue_json(fake_opener, REGISTER_REPLY)
    reissued_resp = client.post(
        "/demo/requests", json={"domain": "third.nl"}, headers=demo_headers
    )
    assert reissued_resp.status_code == 200


def test_user_list_never_shows_hash_or_salt(settings_env):
    _run_admin(["user", "add", "alice"], settings_env)
    _run_admin(["user", "add", "bob"], settings_env)
    _run_admin(["user", "revoke", "bob"], settings_env)

    code, out, _ = _run_admin(["user", "list"], settings_env)
    assert code == 0
    assert "alice" in out
    assert "active" in out
    assert "bob" in out

    conn = store.connect(settings_env["NETNL_DB"])
    for row in store.list_credentials(conn):
        assert row["password_hash"] not in out
        assert row["salt"] not in out


def test_audit_contains_user_add_and_user_revoke(settings_env):
    _run_admin(["user", "add", "alice"], settings_env)
    _run_admin(["user", "revoke", "alice"], settings_env)

    conn = store.connect(settings_env["NETNL_DB"])
    events = [
        row["event"] for row in conn.execute("SELECT event FROM audit ORDER BY id").fetchall()
    ]
    assert "user-add" in events
    assert "user-revoke" in events


# --- prune output, with and without the demo family (T7, D11) ---------------


def test_prune_output_without_demo_config_has_no_demo_line(settings_env):
    code, out, err = _run_admin(["prune"], settings_env)
    assert code == 0
    assert err == ""
    assert "demo" not in out.lower()
    lines = out.splitlines()
    assert len(lines) == 2  # byte-identical shape to before this change
    assert lines[0].startswith("requests pruned: ")
    assert lines[1].startswith("audit records pruned: ")


def test_prune_output_with_demo_config_adds_a_demo_line(settings_env):
    env = dict(settings_env)
    env["NETNL_DEMO_ENABLED"] = "1"
    env["NETNL_DEMO_ALLOWED_ORIGIN"] = DEMO_ORIGIN
    env["NETNL_DEMO_TENANT"] = DEMO_TENANT

    code, out, err = _run_admin(["prune"], env)
    assert code == 0
    lines = out.splitlines()
    assert len(lines) == 3
    assert lines[2] == "demo requests pruned: 0"
