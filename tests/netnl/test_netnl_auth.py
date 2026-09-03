"""Round-2 fixes verified here: finding 1a (no scrypt for a header that
never even tries to authenticate), finding 1b (a bounded, non-blocking cap
on concurrent scrypt verifications, 503 on saturation) and finding 2
(failed-auth audit trail, aggregated per minute to bound its own growth).
"""

from __future__ import annotations

import base64
import threading
import time

from conftest import basic_auth_header

from netnl import auth


def test_missing_authorization_header_is_401_without_scrypt(client, monkeypatch):
    calls = []
    monkeypatch.setattr(auth, "hash_password", lambda *a, **k: calls.append(1) or "x")

    resp = client.get("/metadata/report")

    assert resp.status_code == 401
    assert resp.json()["error"]["label"] == "unauthorised"
    assert calls == []  # no scrypt call at all — nothing to enumerate


def test_malformed_authorization_header_variants_are_401_without_scrypt(client, monkeypatch):
    calls = []
    monkeypatch.setattr(auth, "hash_password", lambda *a, **k: calls.append(1) or "x")

    no_colon = base64.b64encode(b"just-a-username-no-colon").decode()
    headers_variants = [
        {"Authorization": "Bearer some-token"},
        {"Authorization": "Basic not-valid-base64!!!"},
        {"Authorization": f"Basic {no_colon}"},
        {"Authorization": "Basic"},
    ]

    for headers in headers_variants:
        resp = client.get("/metadata/report", headers=headers)
        assert resp.status_code == 401, headers
        assert resp.json()["error"]["label"] == "unauthorised"

    assert calls == []


def test_unknown_username_costs_exactly_one_scrypt_call(client, monkeypatch):
    calls = []
    real_hash_password = auth.hash_password

    def counting_hash_password(password, salt):
        calls.append(salt)
        return real_hash_password(password, salt)

    monkeypatch.setattr(auth, "hash_password", counting_hash_password)

    resp = client.get(
        "/metadata/report", headers=basic_auth_header("no-such-user", "whatever")
    )

    assert resp.status_code == 401
    assert len(calls) == 1
    assert calls[0] == auth._DUMMY_SALT  # the fixed dummy salt, not a real one


def test_wrong_password_for_a_real_user_costs_exactly_one_scrypt_call(client, tenant, monkeypatch):
    calls = []
    real_verify = auth.verify

    def counting_verify(stored_hash, salt, password):
        calls.append(1)
        return real_verify(stored_hash, salt, password)

    monkeypatch.setattr(auth, "verify", counting_verify)

    resp = client.get(
        "/metadata/report", headers=basic_auth_header(tenant["username"], "wrong-password")
    )

    assert resp.status_code == 401
    assert len(calls) == 1


def test_scrypt_semaphore_caps_concurrency_and_503s_when_saturated(
    settings_env, tmp_path, monkeypatch
):
    from starlette.testclient import TestClient

    from conftest import add_test_credential
    from fakes import FakeOpener
    from netnl.api import create_app
    from netnl.settings import load

    env = dict(settings_env)
    env["NETNL_DB"] = str(tmp_path / "scrypt-cap.sqlite3")
    settings = load(env)
    app = create_app(settings, opener=FakeOpener())
    add_test_credential(app, "tenant", "secret")
    client = TestClient(app, raise_server_exceptions=False)

    release_event = threading.Event()
    lock = threading.Lock()
    in_flight = 0
    max_in_flight = 0
    real_hash_password = auth.hash_password

    def blocking_hash_password(password, salt):
        nonlocal in_flight, max_in_flight
        with lock:
            in_flight += 1
            max_in_flight = max(max_in_flight, in_flight)
        release_event.wait(timeout=5)
        with lock:
            in_flight -= 1
        return real_hash_password(password, salt)

    monkeypatch.setattr(auth, "hash_password", blocking_hash_password)

    n = auth._MAX_CONCURRENT_SCRYPT + 4
    responses: list = [None] * n

    def worker(i: int) -> None:
        thread_client = TestClient(app, raise_server_exceptions=False)
        responses[i] = thread_client.get(
            "/metadata/report", headers=basic_auth_header("tenant", "wrong-password")
        )

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    # Let every thread pile up on the semaphore before releasing them.
    deadline = time.monotonic() + 5
    while in_flight < auth._MAX_CONCURRENT_SCRYPT and time.monotonic() < deadline:
        time.sleep(0.01)
    release_event.set()
    for t in threads:
        t.join(timeout=5)

    assert all(r is not None for r in responses)
    status_codes = [r.status_code for r in responses]

    # The cap actually bounded concurrency ...
    assert max_in_flight <= auth._MAX_CONCURRENT_SCRYPT
    # ... and saturation was answered with 503, not silently queued.
    assert status_codes.count(503) >= 1
    assert all(code in (401, 503) for code in status_codes)

    overloaded = next(r for r in responses if r.status_code == 503)
    assert overloaded.json()["error"]["label"] == "overloaded"
    assert overloaded.headers["X-Netnl-Instance"] == settings.instance


def test_failed_auth_is_audited_aggregated_per_minute(client, conn, tenant, clock):
    bad_headers = basic_auth_header(tenant["username"], "wrong-password")
    for _ in range(5):
        resp = client.get("/metadata/report", headers=bad_headers)
        assert resp.status_code == 401

    # Nothing is written yet: the current minute's bucket has not rolled
    # over, so there is nothing to flush.
    still_unflushed = conn.execute(
        "SELECT COUNT(*) AS n FROM audit WHERE event = 'auth-failure'"
    ).fetchone()["n"]
    assert still_unflushed == 0

    clock.advance(61)
    resp = client.get("/metadata/report", headers=bad_headers)
    assert resp.status_code == 401

    rows = conn.execute("SELECT * FROM audit WHERE event = 'auth-failure'").fetchall()
    assert len(rows) == 1  # one summarising row, not five
    assert rows[0]["credential"] == tenant["username"]
    assert rows[0]["domain_count"] == 5  # the aggregated count from the first window
    assert rows[0]["detail"] == "/metadata/report"
    assert rows[0]["facade_id"] is None


def _trigger_sweep(client, fake_opener, tenant):
    """A *successful* authenticated call — proves the sweep of stale
    aggregator buckets runs on any authenticated traffic, not only on the
    next failure (see `_sweep_stale_auth_failure_buckets`'s docstring)."""
    from fakes import METADATA_REPLY

    from conftest import queue_json

    queue_json(fake_opener, METADATA_REPLY)
    resp = client.get("/metadata/report", headers=tenant["headers"])
    assert resp.status_code == 200


def test_missing_header_failure_is_audited_with_null_credential(
    client, fake_opener, conn, tenant, clock
):
    for _ in range(3):
        resp = client.get("/metadata/report")
        assert resp.status_code == 401

    clock.advance(61)
    _trigger_sweep(client, fake_opener, tenant)

    rows = conn.execute("SELECT * FROM audit WHERE event = 'auth-failure'").fetchall()
    assert len(rows) == 1
    assert rows[0]["credential"] is None
    assert rows[0]["domain_count"] == 3
    assert rows[0]["detail"] == "/metadata/report"


def test_password_never_appears_in_audit_row(client, fake_opener, conn, tenant, clock):
    secret_password = "super-secret-do-not-log-me"
    resp = client.get(
        "/metadata/report", headers=basic_auth_header(tenant["username"], secret_password)
    )
    assert resp.status_code == 401

    clock.advance(61)
    _trigger_sweep(client, fake_opener, tenant)

    row = conn.execute("SELECT * FROM audit WHERE event = 'auth-failure'").fetchone()
    assert row is not None
    for value in row.keys():
        assert secret_password not in str(row[value])


def test_username_is_sanitized_before_audit(client, app, fake_opener, conn, tenant, clock):
    dirty_username = "tenant\x01\x02" + ("x" * 200)
    token = base64.b64encode(f"{dirty_username}:wrong".encode()).decode()

    resp = client.get("/metadata/report", headers={"Authorization": f"Basic {token}"})
    assert resp.status_code == 401

    clock.advance(61)
    _trigger_sweep(client, fake_opener, tenant)

    row = conn.execute(
        "SELECT * FROM audit WHERE event = 'auth-failure' AND credential IS NOT NULL"
    ).fetchone()
    assert row is not None
    assert "\x01" not in row["credential"]
    assert "\x02" not in row["credential"]
    assert len(row["credential"]) <= auth._MAX_LOGGED_USERNAME_LEN
