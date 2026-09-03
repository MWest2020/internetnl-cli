"""The anonymous `/demo/*` route family (openspec/changes/add-demo-run).

T3 (submit) and T4 (status/results, ownership) from that change's
tasks.md. CORS/preflight live in `test_netnl_demo_cors.py`; the privacy
proof lives in `test_netnl_demo_privacy.py`; retention/admin output in
`test_netnl_retention.py`/`test_netnl_admin.py`.
"""

from __future__ import annotations

import json

import pytest

from fakes import REGISTER_REPLY, RESULTS_REPLY, STATUS_DONE, STATUS_RUNNING, FakeOpener

from conftest import DEMO_ORIGIN, DEMO_TENANT, basic_auth_header, queue_json
from netnl import auth, store


def _demo_headers(origin: str | None = DEMO_ORIGIN, ip: str | None = None) -> dict:
    headers = {}
    if origin is not None:
        headers["Origin"] = origin
    if ip is not None:
        headers["CF-Connecting-IP"] = ip
    return headers


# --- disabled by default -----------------------------------------------------


def test_demo_disabled_by_default_post(client):
    resp = client.post("/demo/requests", json={"domain": "example.nl"})
    assert resp.status_code == 501
    assert resp.json()["error"]["label"] == "not-implemented"


def test_demo_disabled_by_default_get(client):
    resp = client.get("/demo/requests/" + "0" * 32)
    assert resp.status_code == 501


def test_demo_disabled_by_default_results(client):
    resp = client.get("/demo/requests/" + "0" * 32 + "/results")
    assert resp.status_code == 501


def test_demo_disabled_by_default_options(client):
    # Without the demo family, OPTIONS on this path is just another
    # unmapped method/path — same 501 catch-all.
    resp = client.options("/demo/requests")
    assert resp.status_code == 501


# --- happy path ---------------------------------------------------------------


def test_demo_submit_returns_a_facade_id(demo_client, fake_opener):
    queue_json(fake_opener, REGISTER_REPLY)
    resp = demo_client.post(
        "/demo/requests", json={"domain": "example.nl"}, headers=_demo_headers()
    )
    assert resp.status_code == 200
    body = resp.json()
    request_id = body["request"]["request_id"]
    assert len(request_id) == 32
    assert all(c in "0123456789abcdef" for c in request_id)


def test_probe_of_all_zero_id_is_404_when_enabled(demo_client, fake_opener):
    # The demo page's own smoke check (docs/how-to/demo-run.md).
    resp = demo_client.get("/demo/requests/" + "0" * 32, headers=_demo_headers())
    assert resp.status_code == 404
    assert resp.json()["error"]["label"] == "unknown-request"


def test_upstream_body_is_exactly_the_pinned_shape(demo_client, fake_opener):
    queue_json(fake_opener, REGISTER_REPLY)
    demo_client.post("/demo/requests", json={"domain": "example.nl"}, headers=_demo_headers())
    assert len(fake_opener.calls) == 1
    _method, _url, body, _headers, _timeout = fake_opener.calls[0]
    assert json.loads(body) == {"type": "web", "domains": ["example.nl"], "name": "netnl-demo"}


def test_audit_row_shape_matches_a_tenant_submit(demo_client, fake_opener, demo_app):
    queue_json(fake_opener, REGISTER_REPLY)
    demo_client.post("/demo/requests", json={"domain": "example.nl"}, headers=_demo_headers())

    conn = store.connect(demo_app.state.settings.db)
    try:
        row = conn.execute("SELECT * FROM audit WHERE event = 'submit'").fetchone()
    finally:
        conn.close()
    assert row is not None
    assert row["credential"] == DEMO_TENANT
    assert row["domain_count"] == 1


# --- body shape (D1) -----------------------------------------------------------


def test_extra_field_rejected(demo_client, fake_opener):
    resp = demo_client.post(
        "/demo/requests",
        json={"domain": "example.nl", "type": "web"},
        headers=_demo_headers(),
    )
    assert resp.status_code == 400
    assert len(fake_opener.calls) == 0


def test_type_only_body_rejected(demo_client, fake_opener):
    resp = demo_client.post(
        "/demo/requests", json={"type": "web", "domains": ["example.nl"]}, headers=_demo_headers()
    )
    assert resp.status_code == 400
    assert len(fake_opener.calls) == 0


def test_list_domain_rejected(demo_client, fake_opener):
    resp = demo_client.post(
        "/demo/requests", json={"domain": ["example.nl"]}, headers=_demo_headers()
    )
    assert resp.status_code == 400
    assert len(fake_opener.calls) == 0


# --- normalisation and validation (D14) -----------------------------------------


def test_domain_is_trimmed_and_lowercased(demo_client, fake_opener):
    queue_json(fake_opener, REGISTER_REPLY)
    demo_client.post(
        "/demo/requests", json={"domain": "  Example.NL  "}, headers=_demo_headers()
    )
    _method, _url, body, _headers, _timeout = fake_opener.calls[0]
    assert json.loads(body)["domains"] == ["example.nl"]


@pytest.mark.parametrize(
    "bad_domain",
    ["https://example.nl/path", "127.0.0.1", "localhost", "metadata.google.internal", ""],
)
def test_bad_domain_shape_gets_the_one_literal_message(demo_client, fake_opener, bad_domain):
    resp = demo_client.post(
        "/demo/requests", json={"domain": bad_domain}, headers=_demo_headers()
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["msg"] == "enter a bare domain like example.nl, not a URL"
    assert len(fake_opener.calls) == 0


# --- kill switch (D3) ------------------------------------------------------------


def test_missing_demo_credential_is_unavailable(demo_settings, fake_opener):
    # No add_test_credential call at all — unlike the demo_app fixture.
    from starlette.testclient import TestClient
    from netnl.api import create_app

    app = create_app(demo_settings, opener=fake_opener)
    client = TestClient(app, raise_server_exceptions=False)

    resp = client.post("/demo/requests", json={"domain": "example.nl"}, headers=_demo_headers())
    assert resp.status_code == 503
    assert resp.json()["error"]["label"] == "demo-unavailable"
    assert len(fake_opener.calls) == 0


def test_revoked_demo_credential_is_unavailable(demo_app, demo_client, fake_opener):
    conn = store.connect(demo_app.state.settings.db)
    try:
        store.revoke_credential(conn, DEMO_TENANT, store.utcnow_iso(demo_app.state.now))
    finally:
        conn.close()

    resp = demo_client.post(
        "/demo/requests", json={"domain": "example.nl"}, headers=_demo_headers()
    )
    assert resp.status_code == 503
    assert resp.json()["error"]["label"] == "demo-unavailable"
    assert len(fake_opener.calls) == 0


# --- tenant-style rate/concurrency cap (D3) --------------------------------------


def test_demo_tenant_hourly_cap_is_enforced(settings_env, fake_opener, clock):
    from starlette.testclient import TestClient
    from conftest import add_test_credential
    from netnl.api import create_app
    from netnl.settings import load

    env = dict(settings_env)
    env["NETNL_DEMO_ENABLED"] = "1"
    env["NETNL_DEMO_ALLOWED_ORIGIN"] = DEMO_ORIGIN
    env["NETNL_DEMO_TENANT"] = DEMO_TENANT
    env["NETNL_DEMO_MAX_PER_HOUR"] = "1"
    env["NETNL_DEMO_PER_IP_PER_HOUR"] = "100"
    settings = load(env)
    app = create_app(settings, opener=fake_opener, now=clock)
    add_test_credential(app, DEMO_TENANT, "thrown-away")
    client = TestClient(app, raise_server_exceptions=False)

    queue_json(fake_opener, REGISTER_REPLY)
    first = client.post(
        "/demo/requests", json={"domain": "example.nl"}, headers=_demo_headers(ip="203.0.113.1")
    )
    assert first.status_code == 200

    # Builder-review fix (S2=B1): the second submit's own
    # `refresh_stale_non_terminal` call (run before every reservation
    # attempt, mirroring the tenant path) refreshes the first row's status
    # against upstream before the rate-limit check below ever rejects it —
    # one more upstream call, still non-terminal (`running`), so the
    # rejection is unaffected: the hourly cap counts *submits*, which the
    # refresh cannot undo.
    queue_json(fake_opener, STATUS_RUNNING)
    second = client.post(
        "/demo/requests", json={"domain": "second.nl"}, headers=_demo_headers(ip="203.0.113.2")
    )
    assert second.status_code == 429
    # 1 submit (accepted) + 1 status refresh (rejected before its own
    # submit ever reaches upstream).
    assert len(fake_opener.calls) == 2


# --- per-IP cap (D4) ---------------------------------------------------------------


def test_per_ip_cap_blocks_a_repeat_address(settings_env, fake_opener, clock):
    from starlette.testclient import TestClient
    from conftest import add_test_credential
    from netnl.api import create_app
    from netnl.settings import load

    env = dict(settings_env)
    env["NETNL_DEMO_ENABLED"] = "1"
    env["NETNL_DEMO_ALLOWED_ORIGIN"] = DEMO_ORIGIN
    env["NETNL_DEMO_TENANT"] = DEMO_TENANT
    env["NETNL_DEMO_MAX_PER_HOUR"] = "100"
    env["NETNL_DEMO_PER_IP_PER_HOUR"] = "1"
    settings = load(env)
    app = create_app(settings, opener=fake_opener, now=clock)
    add_test_credential(app, DEMO_TENANT, "thrown-away")
    client = TestClient(app, raise_server_exceptions=False)

    queue_json(fake_opener, REGISTER_REPLY)
    first = client.post(
        "/demo/requests", json={"domain": "one.nl"}, headers=_demo_headers(ip="203.0.113.5")
    )
    assert first.status_code == 200

    second = client.post(
        "/demo/requests", json={"domain": "two.nl"}, headers=_demo_headers(ip="203.0.113.5")
    )
    assert second.status_code == 429
    assert len(fake_opener.calls) == 1


def test_per_ip_cap_does_not_affect_a_different_address(settings_env, fake_opener, clock):
    from starlette.testclient import TestClient
    from conftest import add_test_credential
    from netnl.api import create_app
    from netnl.settings import load

    env = dict(settings_env)
    env["NETNL_DEMO_ENABLED"] = "1"
    env["NETNL_DEMO_ALLOWED_ORIGIN"] = DEMO_ORIGIN
    env["NETNL_DEMO_TENANT"] = DEMO_TENANT
    env["NETNL_DEMO_MAX_PER_HOUR"] = "100"
    env["NETNL_DEMO_PER_IP_PER_HOUR"] = "1"
    settings = load(env)
    app = create_app(settings, opener=fake_opener, now=clock)
    add_test_credential(app, DEMO_TENANT, "thrown-away")
    client = TestClient(app, raise_server_exceptions=False)

    queue_json(fake_opener, REGISTER_REPLY)
    first = client.post(
        "/demo/requests", json={"domain": "one.nl"}, headers=_demo_headers(ip="203.0.113.5")
    )
    # Builder-review fix (S2=B1): the second submit's own
    # `refresh_stale_non_terminal` call refreshes the first (still
    # non-terminal) row against upstream before its own reservation is
    # attempted — one extra queued status reply, ahead of the second
    # submit's own register reply.
    queue_json(fake_opener, STATUS_RUNNING)
    queue_json(fake_opener, REGISTER_REPLY)
    second = client.post(
        "/demo/requests", json={"domain": "two.nl"}, headers=_demo_headers(ip="198.51.100.9")
    )
    assert first.status_code == 200
    assert second.status_code == 200


def test_ipv6_addresses_in_the_same_slash64_share_a_bucket(settings_env, fake_opener, clock):
    from starlette.testclient import TestClient
    from conftest import add_test_credential
    from netnl.api import create_app
    from netnl.settings import load

    env = dict(settings_env)
    env["NETNL_DEMO_ENABLED"] = "1"
    env["NETNL_DEMO_ALLOWED_ORIGIN"] = DEMO_ORIGIN
    env["NETNL_DEMO_TENANT"] = DEMO_TENANT
    env["NETNL_DEMO_MAX_PER_HOUR"] = "100"
    env["NETNL_DEMO_PER_IP_PER_HOUR"] = "1"
    settings = load(env)
    app = create_app(settings, opener=fake_opener, now=clock)
    add_test_credential(app, DEMO_TENANT, "thrown-away")
    client = TestClient(app, raise_server_exceptions=False)

    queue_json(fake_opener, REGISTER_REPLY)
    first = client.post(
        "/demo/requests", json={"domain": "one.nl"}, headers=_demo_headers(ip="2001:db8::1")
    )
    second = client.post(
        # Same /64 as above, different host part.
        "/demo/requests", json={"domain": "two.nl"}, headers=_demo_headers(ip="2001:db8::dead:beef")
    )
    assert first.status_code == 200
    assert second.status_code == 429


def test_missing_ip_header_falls_into_shared_unattributed_bucket(settings_env, fake_opener, clock):
    from starlette.testclient import TestClient
    from conftest import add_test_credential
    from netnl.api import create_app
    from netnl.settings import load

    env = dict(settings_env)
    env["NETNL_DEMO_ENABLED"] = "1"
    env["NETNL_DEMO_ALLOWED_ORIGIN"] = DEMO_ORIGIN
    env["NETNL_DEMO_TENANT"] = DEMO_TENANT
    env["NETNL_DEMO_MAX_PER_HOUR"] = "100"
    env["NETNL_DEMO_PER_IP_PER_HOUR"] = "1"
    settings = load(env)
    app = create_app(settings, opener=fake_opener, now=clock)
    add_test_credential(app, DEMO_TENANT, "thrown-away")
    client = TestClient(app, raise_server_exceptions=False)

    queue_json(fake_opener, REGISTER_REPLY)
    first = client.post("/demo/requests", json={"domain": "one.nl"}, headers=_demo_headers())
    second = client.post("/demo/requests", json={"domain": "two.nl"}, headers=_demo_headers())
    assert first.status_code == 200
    assert second.status_code == 429  # shares the "unattributed" bucket


def test_unparseable_ip_header_falls_into_shared_unattributed_bucket(
    settings_env, fake_opener, clock
):
    from starlette.testclient import TestClient
    from conftest import add_test_credential
    from netnl.api import create_app
    from netnl.settings import load

    env = dict(settings_env)
    env["NETNL_DEMO_ENABLED"] = "1"
    env["NETNL_DEMO_ALLOWED_ORIGIN"] = DEMO_ORIGIN
    env["NETNL_DEMO_TENANT"] = DEMO_TENANT
    env["NETNL_DEMO_MAX_PER_HOUR"] = "100"
    env["NETNL_DEMO_PER_IP_PER_HOUR"] = "1"
    settings = load(env)
    app = create_app(settings, opener=fake_opener, now=clock)
    add_test_credential(app, DEMO_TENANT, "thrown-away")
    client = TestClient(app, raise_server_exceptions=False)

    queue_json(fake_opener, REGISTER_REPLY)
    first = client.post(
        "/demo/requests", json={"domain": "one.nl"}, headers=_demo_headers(ip="not-an-ip")
    )
    second = client.post(
        "/demo/requests", json={"domain": "two.nl"}, headers=_demo_headers(ip="also-garbage")
    )
    assert first.status_code == 200
    assert second.status_code == 429


def test_first_comma_token_of_the_ip_header_is_used(settings_env, fake_opener, clock):
    from starlette.testclient import TestClient
    from conftest import add_test_credential
    from netnl.api import create_app
    from netnl.settings import load

    env = dict(settings_env)
    env["NETNL_DEMO_ENABLED"] = "1"
    env["NETNL_DEMO_ALLOWED_ORIGIN"] = DEMO_ORIGIN
    env["NETNL_DEMO_TENANT"] = DEMO_TENANT
    env["NETNL_DEMO_MAX_PER_HOUR"] = "100"
    env["NETNL_DEMO_PER_IP_PER_HOUR"] = "1"
    settings = load(env)
    app = create_app(settings, opener=fake_opener, now=clock)
    add_test_credential(app, DEMO_TENANT, "thrown-away")
    client = TestClient(app, raise_server_exceptions=False)

    queue_json(fake_opener, REGISTER_REPLY)
    first = client.post(
        "/demo/requests",
        json={"domain": "one.nl"},
        headers=_demo_headers(ip="203.0.113.9, 10.0.0.1"),
    )
    second = client.post(
        "/demo/requests",
        json={"domain": "two.nl"},
        headers=_demo_headers(ip="203.0.113.9, 10.0.0.2"),
    )
    assert first.status_code == 200
    assert second.status_code == 429  # same first token -> same bucket


# --- per-domain cooldown (D5) -----------------------------------------------------


def test_domain_cooldown_blocks_a_repeat_and_never_returns_an_existing_id(
    demo_client, fake_opener
):
    queue_json(fake_opener, REGISTER_REPLY)
    first = demo_client.post(
        "/demo/requests", json={"domain": "example.nl"}, headers=_demo_headers(ip="203.0.113.1")
    )
    assert first.status_code == 200

    second = demo_client.post(
        "/demo/requests", json={"domain": "example.nl"}, headers=_demo_headers(ip="203.0.113.2")
    )
    assert second.status_code == 429
    assert "request" not in second.json()
    assert len(fake_opener.calls) == 1


def test_domain_cooldown_expires(demo_app, demo_client, fake_opener, clock):
    queue_json(fake_opener, REGISTER_REPLY)
    first = demo_client.post(
        "/demo/requests", json={"domain": "example.nl"}, headers=_demo_headers(ip="203.0.113.1")
    )
    assert first.status_code == 200

    clock.advance(demo_app.state.settings.demo.domain_cooldown_seconds + 1)

    # Builder-review fix (S2=B1): the second submit's own
    # `refresh_stale_non_terminal` call refreshes the first (still
    # non-terminal) row against upstream before its own reservation is
    # attempted.
    queue_json(fake_opener, STATUS_RUNNING)
    queue_json(fake_opener, REGISTER_REPLY)
    second = demo_client.post(
        "/demo/requests", json={"domain": "example.nl"}, headers=_demo_headers(ip="203.0.113.2")
    )
    assert second.status_code == 200
    assert second.json()["request"]["request_id"] != first.json()["request"]["request_id"]


# --- auth is never touched (D9) ---------------------------------------------------


def test_authorization_header_is_fully_ignored(demo_client, fake_opener):
    queue_json(fake_opener, REGISTER_REPLY)
    resp = demo_client.post(
        "/demo/requests",
        json={"domain": "example.nl"},
        headers={**_demo_headers(), **basic_auth_header("nobody", "wrong-password")},
    )
    assert resp.status_code == 200


def test_demo_works_even_if_password_hashing_would_raise(demo_client, fake_opener, monkeypatch):
    def _boom(*args, **kwargs):
        raise AssertionError("scrypt must never be invoked on the demo path")

    monkeypatch.setattr(auth, "hash_password", _boom)
    queue_json(fake_opener, REGISTER_REPLY)
    resp = demo_client.post(
        "/demo/requests", json={"domain": "example.nl"}, headers=_demo_headers()
    )
    assert resp.status_code == 200


# --- T4: ownership isolation between the demo credential and a tenant ------------


def test_demo_issued_id_is_retrievable_via_demo_status(demo_client, fake_opener):
    queue_json(fake_opener, REGISTER_REPLY)
    submitted = demo_client.post(
        "/demo/requests", json={"domain": "example.nl"}, headers=_demo_headers()
    )
    request_id = submitted.json()["request"]["request_id"]

    queue_json(fake_opener, STATUS_DONE)
    status = demo_client.get(f"/demo/requests/{request_id}", headers=_demo_headers())
    assert status.status_code == 200
    assert status.json()["request"]["request_id"] == request_id


def test_demo_issued_id_results_are_passthrough(demo_client, fake_opener):
    queue_json(fake_opener, REGISTER_REPLY)
    submitted = demo_client.post(
        "/demo/requests", json={"domain": "example.nl"}, headers=_demo_headers()
    )
    request_id = submitted.json()["request"]["request_id"]

    queue_json(fake_opener, RESULTS_REPLY)
    results = demo_client.get(f"/demo/requests/{request_id}/results", headers=_demo_headers())
    assert results.status_code == 200
    assert results.json()["domains"] == RESULTS_REPLY["domains"]


def test_a_tenants_own_id_is_404_via_the_demo_routes(demo_app, demo_client, fake_opener):
    from conftest import add_test_credential, basic_auth_header as _auth

    add_test_credential(demo_app, "tenant", "tenant-secret")
    from starlette.testclient import TestClient

    tenant_client = TestClient(demo_app, raise_server_exceptions=False)
    queue_json(fake_opener, REGISTER_REPLY)
    submitted = tenant_client.post(
        "/requests",
        json={"type": "web", "domains": ["example.nl"]},
        headers=_auth("tenant", "tenant-secret"),
    )
    tenant_request_id = submitted.json()["request"]["request_id"]

    lookup = demo_client.get(f"/demo/requests/{tenant_request_id}", headers=_demo_headers())
    assert lookup.status_code == 404
    assert lookup.json()["error"]["label"] == "unknown-request"


def test_a_demo_id_is_404_via_the_tenant_routes(demo_app, demo_client, fake_opener):
    from conftest import add_test_credential, basic_auth_header as _auth

    add_test_credential(demo_app, "tenant", "tenant-secret")
    from starlette.testclient import TestClient

    tenant_client = TestClient(demo_app, raise_server_exceptions=False)

    queue_json(fake_opener, REGISTER_REPLY)
    submitted = demo_client.post(
        "/demo/requests", json={"domain": "example.nl"}, headers=_demo_headers()
    )
    demo_request_id = submitted.json()["request"]["request_id"]

    lookup = tenant_client.get(
        f"/requests/{demo_request_id}", headers=_auth("tenant", "tenant-secret")
    )
    assert lookup.status_code == 404
    assert lookup.json()["error"]["label"] == "unknown-request"


# --- builder-review fixes: slot leak (S2=B1), visitor wording (S4=B2) -----------


def test_stale_non_terminal_row_is_refreshed_before_reserving(settings_env, fake_opener, clock):
    """A demo run whose upstream status went terminal without ever being
    polled must not keep occupying a concurrency slot until
    `NETNL_DEMO_RETENTION_HOURS` prunes it (up to 24h by default) — before
    this fix, `POST /demo/requests` never called `limits.
    refresh_stale_non_terminal`, unlike the tenant path.
    """
    from starlette.testclient import TestClient
    from conftest import add_test_credential
    from netnl.api import create_app
    from netnl.settings import load

    env = dict(settings_env)
    env["NETNL_DEMO_ENABLED"] = "1"
    env["NETNL_DEMO_ALLOWED_ORIGIN"] = DEMO_ORIGIN
    env["NETNL_DEMO_TENANT"] = DEMO_TENANT
    env["NETNL_DEMO_MAX_CONCURRENT"] = "1"
    env["NETNL_DEMO_MAX_PER_HOUR"] = "100"
    env["NETNL_DEMO_PER_IP_PER_HOUR"] = "100"
    settings = load(env)
    app = create_app(settings, opener=fake_opener, now=clock)
    add_test_credential(app, DEMO_TENANT, "thrown-away")
    client = TestClient(app, raise_server_exceptions=False)

    queue_json(fake_opener, REGISTER_REPLY)
    first = client.post(
        "/demo/requests", json={"domain": "first.nl"}, headers=_demo_headers(ip="203.0.113.1")
    )
    assert first.status_code == 200

    # Never polled. The second submit's own refresh call still finds it
    # `running` upstream -> the single concurrency slot is still taken.
    queue_json(fake_opener, STATUS_RUNNING)
    second = client.post(
        "/demo/requests", json={"domain": "second.nl"}, headers=_demo_headers(ip="203.0.113.2")
    )
    assert second.status_code == 429

    # Upstream now reports the first run as `done`. The third submit's own
    # refresh call learns this before ever reserving -> the slot is free.
    queue_json(fake_opener, STATUS_DONE)
    queue_json(fake_opener, REGISTER_REPLY)
    third = client.post(
        "/demo/requests", json={"domain": "third.nl"}, headers=_demo_headers(ip="203.0.113.3")
    )
    assert third.status_code == 200


def test_tenant_cap_rejection_uses_a_visitor_literal_not_the_tenant_wording(
    settings_env, fake_opener, clock
):
    """`limits.reserve_submission`'s own 429 names the operator-configured
    numbers ("rate limit of N submissions per hour reached") — that must
    never reach a demo reply verbatim (D13)."""
    from starlette.testclient import TestClient
    from conftest import add_test_credential
    from netnl.api import create_app
    from netnl.settings import load

    env = dict(settings_env)
    env["NETNL_DEMO_ENABLED"] = "1"
    env["NETNL_DEMO_ALLOWED_ORIGIN"] = DEMO_ORIGIN
    env["NETNL_DEMO_TENANT"] = DEMO_TENANT
    env["NETNL_DEMO_MAX_PER_HOUR"] = "1"
    env["NETNL_DEMO_PER_IP_PER_HOUR"] = "100"
    settings = load(env)
    app = create_app(settings, opener=fake_opener, now=clock)
    add_test_credential(app, DEMO_TENANT, "thrown-away")
    client = TestClient(app, raise_server_exceptions=False)

    queue_json(fake_opener, REGISTER_REPLY)
    first = client.post(
        "/demo/requests", json={"domain": "one.nl"}, headers=_demo_headers(ip="203.0.113.1")
    )
    assert first.status_code == 200

    # The second submit's own `refresh_stale_non_terminal` call refreshes
    # the first (still non-terminal) row before its own reservation is
    # attempted and rejected by the hourly cap.
    queue_json(fake_opener, STATUS_RUNNING)
    second = client.post(
        "/demo/requests", json={"domain": "two.nl"}, headers=_demo_headers(ip="203.0.113.2")
    )
    assert second.status_code == 429
    msg = second.json()["error"]["msg"]
    assert msg == "the demo is busy right now; please try again shortly"
    assert "rate limit of" not in msg
    assert "runs already in progress" not in msg
    assert "1" not in msg  # the configured number itself never leaks either


def test_upstream_failure_after_reservation_releases_ip_and_domain_claims(
    settings_env, clock
):
    """A demo submission whose reservation succeeded but whose upstream
    call then failed produced no result for the visitor — it must not
    still cost them their (capped) per-IP slot or their domain's cooldown
    window (S1=M1 reviewer-minor)."""
    from starlette.testclient import TestClient
    from conftest import add_test_credential
    from netnl.api import create_app
    from netnl.settings import load
    from internetnl_cli.client import HttpResponse

    env = dict(settings_env)
    env["NETNL_DEMO_ENABLED"] = "1"
    env["NETNL_DEMO_ALLOWED_ORIGIN"] = DEMO_ORIGIN
    env["NETNL_DEMO_TENANT"] = DEMO_TENANT
    env["NETNL_DEMO_PER_IP_PER_HOUR"] = "1"
    settings = load(env)

    calls = {"n": 0}

    def flaky_opener(method, url, body, headers, timeout):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("connection refused")
        return HttpResponse(status=200, body=json.dumps(REGISTER_REPLY).encode())

    app = create_app(settings, opener=flaky_opener, now=clock)
    add_test_credential(app, DEMO_TENANT, "thrown-away")
    client = TestClient(app, raise_server_exceptions=False)

    first = client.post(
        "/demo/requests", json={"domain": "flaky.nl"}, headers=_demo_headers(ip="203.0.113.7")
    )
    assert first.status_code == 503  # host-free upstream-unreachable outcome (M3)

    # The failed attempt above must not have burned the per-IP slot (cap
    # is 1) or the domain's cooldown: the exact same address and domain can
    # try again immediately and succeed once upstream actually cooperates.
    second = client.post(
        "/demo/requests", json={"domain": "flaky.nl"}, headers=_demo_headers(ip="203.0.113.7")
    )
    assert second.status_code == 200


# --- builder-review fix: host-free upstream errors (M3) -------------------------


def test_upstream_500_on_a_status_poll_has_no_hostname_in_the_body(demo_client, fake_opener):
    queue_json(fake_opener, REGISTER_REPLY)
    submitted = demo_client.post(
        "/demo/requests", json={"domain": "example.nl"}, headers=_demo_headers()
    )
    request_id = submitted.json()["request"]["request_id"]

    queue_json(
        fake_opener,
        {"api_version": "2.6.0", "error": {"label": "server-error", "msg": "boom"}},
        status=500,
    )
    resp = demo_client.get(f"/demo/requests/{request_id}", headers=_demo_headers())
    assert resp.status_code == 502
    body = resp.json()
    assert body["error"]["label"] == "upstream-error"
    assert body["error"]["msg"] == "the measurement instance is unreachable right now"
    raw = json.dumps(body)
    assert "batch.internal" not in raw  # the configured upstream host, from settings_env


def test_upstream_transport_failure_maps_to_demo_unavailable_host_free(settings_env):
    """A genuine network-level failure reusing the exact same, already-
    documented 503 `demo-unavailable` outcome the kill switch itself uses
    — see `netnl.demo._visitor_upstream_error`."""
    from starlette.testclient import TestClient
    from conftest import add_test_credential
    from netnl.api import create_app
    from netnl.settings import load

    env = dict(settings_env)
    env["NETNL_DEMO_ENABLED"] = "1"
    env["NETNL_DEMO_ALLOWED_ORIGIN"] = DEMO_ORIGIN
    env["NETNL_DEMO_TENANT"] = DEMO_TENANT
    settings = load(env)

    def crashing_opener(method, url, body, headers, timeout):
        raise OSError("connection refused while contacting batch.internal")

    app = create_app(settings, opener=crashing_opener)
    add_test_credential(app, DEMO_TENANT, "thrown-away")
    client = TestClient(app, raise_server_exceptions=False)

    resp = client.post("/demo/requests", json={"domain": "example.nl"}, headers=_demo_headers())
    assert resp.status_code == 503
    body = resp.json()
    assert body["error"]["label"] == "demo-unavailable"
    assert "batch.internal" not in json.dumps(body)


# --- builder-review fix: poll budget and terminal-row-from-store (M2) -----------


def test_polling_a_terminal_row_makes_no_further_upstream_calls(demo_client, fake_opener):
    queue_json(fake_opener, REGISTER_REPLY)
    submitted = demo_client.post(
        "/demo/requests", json={"domain": "example.nl"}, headers=_demo_headers()
    )
    request_id = submitted.json()["request"]["request_id"]

    queue_json(fake_opener, STATUS_DONE)
    first_poll = demo_client.get(f"/demo/requests/{request_id}", headers=_demo_headers())
    assert first_poll.status_code == 200
    assert first_poll.json()["request"]["status"] == "done"
    calls_after_first_terminal_poll = len(fake_opener.calls)

    # 50 further polls of the same, now-terminal row: the stored status
    # cannot change any further, so none of these touch upstream at all.
    for _ in range(50):
        again = demo_client.get(f"/demo/requests/{request_id}", headers=_demo_headers())
        assert again.status_code == 200
        assert again.json()["request"]["status"] == "done"
    assert len(fake_opener.calls) == calls_after_first_terminal_poll


def test_poll_budget_returns_429_once_exceeded(settings_env, fake_opener, clock):
    from starlette.testclient import TestClient
    from conftest import add_test_credential
    from netnl.api import create_app
    from netnl.settings import load

    env = dict(settings_env)
    env["NETNL_DEMO_ENABLED"] = "1"
    env["NETNL_DEMO_ALLOWED_ORIGIN"] = DEMO_ORIGIN
    env["NETNL_DEMO_TENANT"] = DEMO_TENANT
    env["NETNL_DEMO_POLLS_PER_IP_PER_HOUR"] = "3"
    settings = load(env)
    app = create_app(settings, opener=fake_opener, now=clock)
    add_test_credential(app, DEMO_TENANT, "thrown-away")
    client = TestClient(app, raise_server_exceptions=False)

    queue_json(fake_opener, REGISTER_REPLY)
    submitted = client.post(
        "/demo/requests", json={"domain": "example.nl"}, headers=_demo_headers(ip="203.0.113.4")
    )
    request_id = submitted.json()["request"]["request_id"]

    for _ in range(3):
        queue_json(fake_opener, STATUS_RUNNING)
        resp = client.get(
            f"/demo/requests/{request_id}", headers=_demo_headers(ip="203.0.113.4")
        )
        assert resp.status_code == 200

    over_budget = client.get(
        f"/demo/requests/{request_id}", headers=_demo_headers(ip="203.0.113.4")
    )
    assert over_budget.status_code == 429
    assert over_budget.json()["error"]["msg"] == (
        "too many status checks from this network recently; please try again later"
    )


# --- builder-review fix: pydantic validation errors flattened (S9) --------------


@pytest.mark.parametrize(
    "bad_body",
    [
        {"domain": "example.nl", "extra": "x"},
        {"domain": ["example.nl"]},
        {"type": "web", "domains": ["example.nl"]},
    ],
)
def test_pydantic_validation_failures_flatten_to_the_bad_domain_literal(
    demo_client, fake_opener, bad_body
):
    resp = demo_client.post("/demo/requests", json=bad_body, headers=_demo_headers())
    assert resp.status_code == 400
    assert resp.json()["error"]["msg"] == "enter a bare domain like example.nl, not a URL"
    # Never a raw pydantic field/loc path reflected back to the visitor.
    assert "loc" not in resp.text
    assert "extra" not in resp.json()["error"]["msg"]
