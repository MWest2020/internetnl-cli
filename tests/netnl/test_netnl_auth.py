"""Round-2 fixes verified here: finding 1a (no scrypt for a header that
never even tries to authenticate), finding 1b (a bounded, non-blocking cap
on concurrent scrypt verifications, 503 on saturation) and finding 2
(failed-auth audit trail, aggregated per minute to bound its own growth).
"""

from __future__ import annotations

import base64
import logging
import sqlite3
import threading
import time

from conftest import basic_auth_header, queue_json

from netnl import auth, store


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
    # Round-3 fix (security-M1): the semaphore now waits up to
    # `_SCRYPT_ACQUIRE_TIMEOUT` for a slot before giving up, so an
    # over-cap thread only 503s once saturation has *lasted* longer than
    # that bounded wait — hold the cap saturated past it before releasing,
    # so this test still exercises genuine sustained saturation rather
    # than a burst that clears within the wait (which the round-3 fix
    # deliberately no longer 503s).
    time.sleep(auth._SCRYPT_ACQUIRE_TIMEOUT + 0.5)
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
    # Round-3 fix (security-H1 / reviewer-L4): the count lives in `detail`,
    # not `domain_count` — that column means "domains in a submission" and
    # must not be repurposed as a failure tally.
    assert rows[0]["domain_count"] is None
    assert rows[0]["detail"] == "/metadata/report failures=5"
    assert rows[0]["facade_id"] is None
    # Round-3 fix (security-H1c): `at` is the failure window's own minute
    # (the clock's value when the failures happened), not the later moment
    # the flush actually ran (61+ seconds after).
    assert rows[0]["at"] == "2026-01-01T00:00:00+00:00"


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
    assert rows[0]["domain_count"] is None
    assert rows[0]["detail"] == "/metadata/report failures=3"


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


def test_basic_scheme_is_case_insensitive(client, tenant, fake_opener):
    """Round-3 fix (reviewer-m8): RFC 7617 defines the `Basic` auth-scheme
    token as case-insensitive."""
    from fakes import METADATA_REPLY

    token = base64.b64encode(f"{tenant['username']}:{tenant['password']}".encode()).decode()
    for scheme in ("basic", "BASIC", "Basic", "BaSiC"):
        queue_json(fake_opener, METADATA_REPLY)
        resp = client.get(
            "/metadata/report", headers={"Authorization": f"{scheme} {token}"}
        )
        assert resp.status_code == 200, (scheme, resp.text)


# --- round-3: the auth-failure aggregator is bounded, fast and resilient --


def test_bucket_count_is_capped_regardless_of_unique_usernames(conn, clock):
    """Round-3 fix (security-H1a): 3000 distinct usernames failing within
    the same one-minute window must not mint 3000 buckets (and, once
    flushed, 3000 audit rows) — the dict, and what it can ever flush, is
    bounded by `_MAX_BUCKETS` plus the (here: one) distinct route hit.
    """
    for i in range(3000):
        auth._record_auth_failure(conn, clock(), f"user-{i}", "/metadata/report")

    assert len(auth._auth_failure_buckets) <= auth._MAX_BUCKETS + 1

    clock.advance(61)
    auth._sweep_stale_auth_failure_buckets(conn, clock())

    rows = conn.execute("SELECT * FROM audit WHERE event = 'auth-failure'").fetchall()
    assert 0 < len(rows) <= auth._MAX_BUCKETS + 1
    assert len(rows) < 3000

    overflow_rows = [r for r in rows if r["credential"] == auth._OVERFLOW_USERNAME]
    assert len(overflow_rows) == 1
    assert "failures=" in overflow_rows[0]["detail"]


class _LockCheckingConnection:
    """Forwards every call to a real connection, recording any `execute`/
    `executemany` that happens while `lock` is held by *this* thread — used
    to prove the aggregator's DB write runs outside `_auth_failure_lock`
    (round-3 fix, security-H1b)."""

    def __init__(self, real, lock, held_during):
        self._real = real
        self._lock = lock
        self._held_during = held_during

    def _check(self, sql):
        if not self._lock.acquire(blocking=False):
            self._held_during.append(sql)
        else:
            self._lock.release()

    def execute(self, sql, *args, **kwargs):
        self._check(sql)
        return self._real.execute(sql, *args, **kwargs)

    def executemany(self, sql, *args, **kwargs):
        self._check(sql)
        return self._real.executemany(sql, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._real, name)


def test_flush_of_many_buckets_is_one_fast_transaction_outside_the_lock(conn, clock):
    """Round-3 fix (security-H1b): flushing 10k stale buckets must (a) run
    as a single transaction, not one autocommit INSERT per bucket
    (measured before the fix: ~5.5s for 10k; batched: well under 100ms),
    and (b) never touch the database while `_auth_failure_lock` is held.
    """
    old_minute = auth._current_minute(clock()) - 5
    auth._auth_failure_buckets.update(
        {(f"user-{i}", "/metadata/report"): (old_minute, 1) for i in range(10_000)}
    )

    held_during: list = []
    proxy = _LockCheckingConnection(conn, auth._auth_failure_lock, held_during)

    start = time.perf_counter()
    auth._sweep_stale_auth_failure_buckets(proxy, clock())
    elapsed = time.perf_counter() - start

    assert held_during == [], "a DB statement ran while the aggregator lock was held"
    assert elapsed < 0.1, f"flushing 10k buckets took {elapsed:.3f}s (expected one transaction)"

    n = conn.execute(
        "SELECT COUNT(*) AS n FROM audit WHERE event = 'auth-failure'"
    ).fetchone()["n"]
    assert n == 10_000


def test_failing_auth_failure_flush_does_not_fail_the_request(app, client, fake_opener, tenant, clock, caplog):
    """Round-3 fix (security-H1d): a failing aggregator write (simulated
    here as `executemany` raising, standing in for e.g. a concurrent
    `BEGIN IMMEDIATE` from `prune`) must never turn a legitimate,
    successfully-authenticated request into a 500 — it is logged and the
    window's tally is dropped instead.
    """
    from fakes import METADATA_REPLY

    resp = client.get(
        "/metadata/report", headers=basic_auth_header(tenant["username"], "wrong-password")
    )
    assert resp.status_code == 401
    clock.advance(61)

    class _FailingExecuteMany:
        def __init__(self, real):
            self._real = real

        def executemany(self, *args, **kwargs):
            raise sqlite3.OperationalError("database is locked")

        def __getattr__(self, name):
            return getattr(self._real, name)

    def override_conn():
        real = store.connect(app.state.settings.db)
        try:
            yield _FailingExecuteMany(real)
        finally:
            real.close()

    app.dependency_overrides[store.get_conn] = override_conn
    try:
        queue_json(fake_opener, METADATA_REPLY)
        with caplog.at_level(logging.WARNING, logger="netnl.auth"):
            resp2 = client.get("/metadata/report", headers=tenant["headers"])
        assert resp2.status_code == 200, resp2.text
    finally:
        app.dependency_overrides.pop(store.get_conn, None)

    assert any("failed to persist" in message for message in caplog.messages)

    real_conn = store.connect(app.state.settings.db)
    try:
        rows = real_conn.execute(
            "SELECT * FROM audit WHERE event = 'auth-failure'"
        ).fetchall()
        assert rows == []  # the tally was dropped, not silently retried later
    finally:
        real_conn.close()
