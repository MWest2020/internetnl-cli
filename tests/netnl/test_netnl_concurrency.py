"""Round-1 fix verification: B1 (cross-tenant isolation under real
concurrency) and B2 (rate/concurrency limits cannot be raced past).

These tests drive the facade with real OS threads through `TestClient`
(itself backed by FastAPI's threadpool for sync route handlers, exactly the
production execution model design.md describes) — not sequential calls
dressed up as a concurrency test.
"""

from __future__ import annotations

import json
import re
import threading
import time

from starlette.testclient import TestClient

from fakes import REGISTER_REPLY, FakeOpener
from internetnl_cli.client import HttpResponse

from conftest import add_test_credential, basic_auth_header, queue_json
from netnl import store
from netnl.api import create_app
from netnl.settings import load

_UPSTREAM_ID_RE = re.compile(r"/requests/([0-9a-f]{32})")


class TaggingOpener:
    """A thread-safe fake upstream.

    Each `submit` call is tagged with the *domain* the caller sent (a
    tenant's own choice, unlike the shared facade-to-upstream credential,
    which is identical for every tenant and so cannot be used to tell them
    apart). Status/results calls look the tag back up by the upstream id
    encoded in the URL, so a test can assert that a lookup for tenant N's
    own facade id only ever returns tenant N's own tag — proving B1 — and
    an optional per-call delay lets many submits' upstream calls overlap in
    time, which is what makes B2's race reproducible.
    """

    def __init__(self, delay: float = 0.0) -> None:
        self._lock = threading.Lock()
        self._counter = 0
        self._tags: dict[str, str] = {}
        self.submit_calls = 0
        self._delay = delay

    def __call__(self, method, url, body, headers, timeout) -> HttpResponse:
        if method == "POST":
            if self._delay:
                time.sleep(self._delay)
            payload = json.loads(body)
            tag = payload["domains"][0]
            with self._lock:
                self._counter += 1
                self.submit_calls += 1
                upstream_id = f"{self._counter:032x}"
                self._tags[upstream_id] = tag
            return HttpResponse(status=200, body=json.dumps(_reply(upstream_id, tag)).encode())

        match = _UPSTREAM_ID_RE.search(url)
        assert match, url
        upstream_id = match.group(1)
        with self._lock:
            tag = self._tags.get(upstream_id, "<unknown>")
        reply = _reply(upstream_id, tag)
        if url.rstrip("/").endswith("/results"):
            reply["domains"] = {tag: {"status": "ok", "scoring": {"percentage": 100}}}
        return HttpResponse(status=200, body=json.dumps(reply).encode())


def _reply(upstream_id: str, tag: str) -> dict:
    return {
        "api_version": "2.6.0",
        "request": {
            "request_id": upstream_id,
            "name": tag,
            "request_type": "web",
            "status": "done",
            "submit_date": "2026-01-01T00:00:00+00:00",
            "finished_date": "2026-01-01T00:05:00+00:00",
        },
    }


def _build_app(settings_env, tmp_path, db_name, **env_overrides):
    env = dict(settings_env)
    env.update(env_overrides)
    env["NETNL_DB"] = str(tmp_path / db_name)
    settings = load(env)
    return settings


# --- B1: isolation under concurrency ---------------------------------------


def test_isolation_holds_under_concurrent_status_and_results_lookups(settings_env, tmp_path):
    settings = _build_app(settings_env, tmp_path, "isolation.sqlite3")
    opener = TaggingOpener()
    app = create_app(settings, opener=opener)

    n_tenants = 8
    tags = [f"tenant-{i}.example" for i in range(n_tenants)]
    creds = [(f"tenant-{i}", f"secret-{i}") for i in range(n_tenants)]
    for username, password in creds:
        add_test_credential(app, username, password)

    setup_client = TestClient(app, raise_server_exceptions=False)
    facade_ids = []
    for i, (username, password) in enumerate(creds):
        resp = setup_client.post(
            "/requests",
            json={"type": "web", "domains": [tags[i]]},
            headers=basic_auth_header(username, password),
        )
        assert resp.status_code == 200
        facade_ids.append(resp.json()["request"]["request_id"])

    # All facade ids must be distinct — a collision would itself be a leak.
    assert len(set(facade_ids)) == n_tenants

    errors: list[str] = []
    errors_lock = threading.Lock()

    def worker(i: int) -> None:
        # A dedicated `TestClient` per thread: it wraps the same `app`
        # instance (the thing actually under test), avoiding any doubt
        # about `httpx.Client`'s own thread-safety clouding the result.
        thread_client = TestClient(app, raise_server_exceptions=False)
        headers = basic_auth_header(*creds[i])
        try:
            for _ in range(15):
                status_resp = thread_client.get(f"/requests/{facade_ids[i]}", headers=headers)
                results_resp = thread_client.get(
                    f"/requests/{facade_ids[i]}/results", headers=headers
                )
                assert status_resp.status_code == 200, status_resp.text
                assert results_resp.status_code == 200, results_resp.text
                assert status_resp.json()["request"]["name"] == tags[i], status_resp.json()
                assert status_resp.json()["request"]["request_id"] == facade_ids[i]
                domains = results_resp.json()["domains"]
                assert list(domains.keys()) == [tags[i]], domains
        except AssertionError as exc:
            with errors_lock:
                errors.append(f"tenant {i}: {exc}")

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_tenants)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert errors == [], "\n".join(errors)


def test_isolation_holds_under_concurrent_submits(settings_env, tmp_path):
    """The submit path itself, hit by every tenant at once — the exact
    end-to-end scenario both reviewers reproduced (B1)."""
    settings = _build_app(
        settings_env, tmp_path, "isolation-submit.sqlite3", NETNL_MAX_CONCURRENT="1000"
    )
    opener = TaggingOpener()
    app = create_app(settings, opener=opener)

    n_tenants = 10
    creds = [(f"tenant-{i}", f"secret-{i}") for i in range(n_tenants)]
    for username, password in creds:
        add_test_credential(app, username, password)

    results: dict[int, tuple[int, dict]] = {}
    results_lock = threading.Lock()

    def worker(i: int) -> None:
        thread_client = TestClient(app, raise_server_exceptions=False)
        tag = f"submit-tenant-{i}.example"
        resp = thread_client.post(
            "/requests",
            json={"type": "web", "domains": [tag]},
            headers=basic_auth_header(*creds[i]),
        )
        with results_lock:
            results[i] = (resp.status_code, resp.json())

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_tenants)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert len(results) == n_tenants
    facade_ids = set()
    conn = store.connect(app.state.settings.db)
    try:
        for i, (status_code, body) in results.items():
            assert status_code == 200, body
            facade_id = body["request"]["request_id"]
            assert facade_id not in facade_ids, "duplicate/leaked facade id across tenants"
            facade_ids.add(facade_id)

            row = conn.execute(
                "SELECT credential_id, domain_count FROM requests WHERE facade_id = ?",
                (facade_id,),
            ).fetchone()
            assert row is not None
            expected_cred = conn.execute(
                "SELECT id FROM credentials WHERE username = ?", (f"tenant-{i}",)
            ).fetchone()["id"]
            assert row["credential_id"] == expected_cred, (
                f"tenant {i}'s request row is owned by a different credential"
            )
    finally:
        conn.close()


# --- B2: limits cannot be raced past -----------------------------------------


def test_concurrent_submits_cannot_exceed_rate_limit(settings_env, tmp_path):
    settings = _build_app(
        settings_env,
        tmp_path,
        "rate-race.sqlite3",
        NETNL_RATE_LIMIT="2",
        NETNL_MAX_CONCURRENT="1000",
    )
    # A generous delay: large enough that every thread's (fast, in-process)
    # reservation attempt has certainly happened before the first accepted
    # submit's upstream call returns and frees its slot — otherwise this
    # test would flake by conflating "the concurrency gauge is live" (by
    # design: a finished run frees its slot immediately) with "the
    # reservation itself raced", which is what B2 actually guards against.
    opener = TaggingOpener(delay=0.5)
    app = create_app(settings, opener=opener)
    add_test_credential(app, "tenant", "secret")
    headers = basic_auth_header("tenant", "secret")

    n = 12
    status_codes: list[int] = []
    lock = threading.Lock()

    def worker(i: int) -> None:
        thread_client = TestClient(app, raise_server_exceptions=False)
        resp = thread_client.post(
            "/requests", json={"type": "web", "domains": [f"race-{i}.example"]}, headers=headers
        )
        with lock:
            status_codes.append(resp.status_code)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert len(status_codes) == n
    accepted = status_codes.count(200)
    rejected = status_codes.count(429)
    assert accepted == 2, status_codes
    assert accepted + rejected == n, status_codes
    assert opener.submit_calls == 2  # never more upstream POSTs than the rate limit allows

    conn = store.connect(app.state.settings.db)
    try:
        audit_count = conn.execute(
            "SELECT COUNT(*) AS n FROM audit WHERE event = 'submit'"
        ).fetchone()["n"]
        assert audit_count == accepted  # exactly as many audit rows as accepted submits
    finally:
        conn.close()


def test_concurrent_submits_cannot_exceed_concurrency_limit(settings_env, tmp_path):
    settings = _build_app(
        settings_env,
        tmp_path,
        "concurrency-race.sqlite3",
        NETNL_MAX_CONCURRENT="2",
        NETNL_RATE_LIMIT="1000",
    )
    # A generous delay: large enough that every thread's (fast, in-process)
    # reservation attempt has certainly happened before the first accepted
    # submit's upstream call returns and frees its slot — otherwise this
    # test would flake by conflating "the concurrency gauge is live" (by
    # design: a finished run frees its slot immediately) with "the
    # reservation itself raced", which is what B2 actually guards against.
    opener = TaggingOpener(delay=0.5)
    app = create_app(settings, opener=opener)
    add_test_credential(app, "tenant", "secret")
    headers = basic_auth_header("tenant", "secret")

    n = 10
    status_codes: list[int] = []
    lock = threading.Lock()

    def worker(i: int) -> None:
        thread_client = TestClient(app, raise_server_exceptions=False)
        resp = thread_client.post(
            "/requests", json={"type": "web", "domains": [f"conc-{i}.example"]}, headers=headers
        )
        with lock:
            status_codes.append(resp.status_code)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert len(status_codes) == n
    accepted = status_codes.count(200)
    rejected = status_codes.count(429)
    assert accepted == 2, status_codes
    assert accepted + rejected == n, status_codes
    assert opener.submit_calls == 2

    conn = store.connect(app.state.settings.db)
    try:
        audit_count = conn.execute(
            "SELECT COUNT(*) AS n FROM audit WHERE event = 'submit'"
        ).fetchone()["n"]
        assert audit_count == accepted
    finally:
        conn.close()


# --- reserving-row edge case (defensive: the crash scenario in design.md) --


def test_reserving_row_is_retrievable_by_its_owner_without_a_crash(settings_env, tmp_path):
    settings = _build_app(settings_env, tmp_path, "reserving.sqlite3")
    fake_opener = FakeOpener()
    app = create_app(settings, opener=fake_opener)
    add_test_credential(app, "tenant", "secret")

    conn = store.connect(app.state.settings.db)
    try:
        credential_id = conn.execute(
            "SELECT id FROM credentials WHERE username = 'tenant'"
        ).fetchone()["id"]
        store.insert_reserving_request(
            conn,
            facade_id="a" * 32,
            credential_id=credential_id,
            request_type="web",
            domain_count=1,
            submitted_at=store.utcnow_iso(),
        )
    finally:
        conn.close()

    client = TestClient(app, raise_server_exceptions=False)
    headers = basic_auth_header("tenant", "secret")

    status_resp = client.get("/requests/" + "a" * 32, headers=headers)
    assert status_resp.status_code == 200
    assert status_resp.json()["request"]["status"] == "reserving"
    assert status_resp.json()["request"]["request_id"] == "a" * 32

    results_resp = client.get("/requests/" + "a" * 32 + "/results", headers=headers)
    assert results_resp.status_code == 200
    assert results_resp.json()["request"]["status"] == "reserving"
    assert results_resp.json()["domains"] == {}

    # A reserving row is still non-terminal, so it counts toward
    # concurrency (design.md): with the default limit of 2, one more slot
    # remains.
    queue_json(fake_opener, REGISTER_REPLY)
    resp = client.post(
        "/requests",
        json={"type": "web", "domains": ["fills-second-slot.example"]},
        headers=headers,
    )
    assert resp.status_code == 200


def test_reserving_row_is_still_owner_only(settings_env, tmp_path):
    settings = _build_app(settings_env, tmp_path, "reserving-owner.sqlite3")
    app = create_app(settings, opener=FakeOpener())
    add_test_credential(app, "tenant", "secret")
    add_test_credential(app, "someone-else", "other-secret")

    conn = store.connect(app.state.settings.db)
    try:
        credential_id = conn.execute(
            "SELECT id FROM credentials WHERE username = 'tenant'"
        ).fetchone()["id"]
        store.insert_reserving_request(
            conn,
            facade_id="b" * 32,
            credential_id=credential_id,
            request_type="web",
            domain_count=1,
            submitted_at=store.utcnow_iso(),
        )
    finally:
        conn.close()

    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get(
        "/requests/" + "b" * 32, headers=basic_auth_header("someone-else", "other-secret")
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["label"] == "unknown-request"
