from __future__ import annotations

import io

from starlette.testclient import TestClient

from fakes import REGISTER_REPLY, FakeOpener

from conftest import basic_auth_header, queue_json
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
