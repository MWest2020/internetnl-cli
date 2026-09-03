"""CORS, preflight and no-store for the `/demo/*` family (openspec/changes/
add-demo-run, T5 — D6/D7/D8).
"""

from __future__ import annotations

from fakes import REGISTER_REPLY

from conftest import DEMO_ORIGIN, DEMO_TENANT, add_test_credential, basic_auth_header, queue_json


def _origin_header(origin: str = DEMO_ORIGIN) -> dict:
    return {"Origin": origin}


def _assert_demo_cors(resp, *, origin_matches: bool):
    assert resp.headers.get("Cache-Control") == "no-store"
    assert resp.headers.get("Vary") == "Origin"
    if origin_matches:
        assert resp.headers.get("Access-Control-Allow-Origin") == DEMO_ORIGIN
        assert (
            resp.headers.get("Access-Control-Expose-Headers")
            == "X-Netnl-Instance, X-Netnl-Notice"
        )
    else:
        assert "Access-Control-Allow-Origin" not in resp.headers
        assert "Access-Control-Expose-Headers" not in resp.headers
    assert "Access-Control-Allow-Credentials" not in resp.headers


_EXPECTED_CSP = "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"


def _assert_security_headers(resp):
    assert resp.headers["Content-Security-Policy"] == _EXPECTED_CSP
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    assert resp.headers["Referrer-Policy"] == "no-referrer"
    assert resp.headers["X-Frame-Options"] == "DENY"


# --- ACAO present across every demo status code --------------------------------


def test_acao_on_a_200(demo_client, fake_opener):
    queue_json(fake_opener, REGISTER_REPLY)
    resp = demo_client.post(
        "/demo/requests", json={"domain": "example.nl"}, headers=_origin_header()
    )
    assert resp.status_code == 200
    _assert_demo_cors(resp, origin_matches=True)
    _assert_security_headers(resp)


def test_acao_on_a_400(demo_client, fake_opener):
    resp = demo_client.post(
        "/demo/requests", json={"domain": "not a domain"}, headers=_origin_header()
    )
    assert resp.status_code == 400
    _assert_demo_cors(resp, origin_matches=True)
    _assert_security_headers(resp)


def test_acao_on_a_429(demo_client, fake_opener):
    queue_json(fake_opener, REGISTER_REPLY)
    demo_client.post(
        "/demo/requests",
        json={"domain": "example.nl"},
        headers={**_origin_header(), "CF-Connecting-IP": "203.0.113.1"},
    )
    resp = demo_client.post(
        "/demo/requests",
        json={"domain": "example.nl"},
        headers={**_origin_header(), "CF-Connecting-IP": "203.0.113.2"},
    )
    assert resp.status_code == 429
    _assert_demo_cors(resp, origin_matches=True)


def test_acao_on_a_404(demo_client, fake_opener):
    resp = demo_client.get("/demo/requests/" + "0" * 32, headers=_origin_header())
    assert resp.status_code == 404
    _assert_demo_cors(resp, origin_matches=True)


def test_acao_on_the_body_size_400(settings_env, tmp_path, fake_opener):
    from starlette.testclient import TestClient
    from netnl.api import create_app
    from netnl.settings import load

    env = dict(settings_env)
    env["NETNL_DEMO_ENABLED"] = "1"
    env["NETNL_DEMO_ALLOWED_ORIGIN"] = DEMO_ORIGIN
    env["NETNL_DEMO_TENANT"] = DEMO_TENANT
    env["NETNL_MAX_BODY_BYTES"] = "16"
    env["NETNL_DB"] = str(tmp_path / "demo-body-size.sqlite3")
    settings = load(env)
    app = create_app(settings, opener=fake_opener)
    add_test_credential(app, DEMO_TENANT, "thrown-away")
    client = TestClient(app, raise_server_exceptions=False)

    resp = client.post(
        "/demo/requests", json={"domain": "example.nl"}, headers=_origin_header()
    )
    assert resp.status_code == 400
    _assert_demo_cors(resp, origin_matches=True)
    _assert_security_headers(resp)


def test_acao_on_the_catch_all_500(settings_env, tmp_path):
    from starlette.testclient import TestClient
    from netnl.api import create_app
    from netnl.settings import load

    env = dict(settings_env)
    env["NETNL_DEMO_ENABLED"] = "1"
    env["NETNL_DEMO_ALLOWED_ORIGIN"] = DEMO_ORIGIN
    env["NETNL_DEMO_TENANT"] = DEMO_TENANT
    env["NETNL_DB"] = str(tmp_path / "demo-crash.sqlite3")
    settings = load(env)

    def crashing_opener(method, url, body, headers, timeout):
        raise RuntimeError("boom")

    app = create_app(settings, opener=crashing_opener)
    add_test_credential(app, DEMO_TENANT, "thrown-away")
    client = TestClient(app, raise_server_exceptions=False)

    resp = client.post(
        "/demo/requests", json={"domain": "example.nl"}, headers=_origin_header()
    )
    assert resp.status_code == 500
    _assert_demo_cors(resp, origin_matches=True)
    _assert_security_headers(resp)


# --- never on non-demo routes ----------------------------------------------------


def test_acao_absent_on_health(demo_client):
    resp = demo_client.get("/health", headers=_origin_header())
    assert resp.status_code == 200
    assert "Access-Control-Allow-Origin" not in resp.headers
    assert "Cache-Control" not in resp.headers


def test_acao_absent_on_the_tenant_surface(demo_app, demo_client, fake_opener):
    add_test_credential(demo_app, "tenant", "tenant-secret")
    resp = demo_client.get(
        "/requests/" + "a" * 32,
        headers={**_origin_header(), **basic_auth_header("tenant", "tenant-secret")},
    )
    assert "Access-Control-Allow-Origin" not in resp.headers


# --- preflight (D8) ---------------------------------------------------------------


def test_preflight_matching_origin_gets_cors_headers(demo_client):
    resp = demo_client.options("/demo/requests", headers=_origin_header())
    assert resp.status_code == 204
    _assert_demo_cors(resp, origin_matches=True)


def test_preflight_mismatched_origin_is_204_without_cors_headers(demo_client):
    resp = demo_client.options("/demo/requests", headers=_origin_header("https://evil.example"))
    assert resp.status_code == 204
    _assert_demo_cors(resp, origin_matches=False)


def test_preflight_on_the_id_paths(demo_client):
    for path in ["/demo/requests/" + "a" * 32, "/demo/requests/" + "a" * 32 + "/results"]:
        resp = demo_client.options(path, headers=_origin_header())
        assert resp.status_code == 204
        _assert_demo_cors(resp, origin_matches=True)


# --- forbidden-origin on an actual request (D6) ------------------------------------


def test_mismatched_origin_on_an_actual_request_is_403(demo_client, fake_opener):
    resp = demo_client.post(
        "/demo/requests",
        json={"domain": "example.nl"},
        headers=_origin_header("https://evil.example"),
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["label"] == "forbidden-origin"
    assert len(fake_opener.calls) == 0


def test_absent_origin_on_an_actual_request_is_allowed(demo_client, fake_opener):
    queue_json(fake_opener, REGISTER_REPLY)
    resp = demo_client.post("/demo/requests", json={"domain": "example.nl"})
    assert resp.status_code == 200
    # No Origin header sent -> no ACAO expected either (nothing to answer).
    assert "Access-Control-Allow-Origin" in resp.headers  # still the fixed value
    assert resp.headers["Access-Control-Allow-Origin"] == DEMO_ORIGIN
