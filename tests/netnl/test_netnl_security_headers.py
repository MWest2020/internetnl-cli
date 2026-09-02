"""Security headers on every reply (success and error), and the opt-in
`/.well-known/security.txt` route (RFC 9116).

Measured against the live facade (2026-08-31, Internet.nl webtest):
`web_appsecpriv_csp`, `web_appsecpriv_x_content_type_options` and
`web_appsecpriv_securitytxt` all came back "bad" for lack of these.
"""

from __future__ import annotations

from conftest import basic_auth_header


_EXPECTED_CSP = "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"


def _assert_security_headers(resp):
    assert resp.headers["Content-Security-Policy"] == _EXPECTED_CSP
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    assert resp.headers["Referrer-Policy"] == "no-referrer"
    assert resp.headers["X-Frame-Options"] == "DENY"
    # TLS is terminated in front of this facade, not by this process — HSTS
    # is that hop's responsibility, not this one's.
    assert "Strict-Transport-Security" not in resp.headers


def test_security_headers_on_a_normal_response(client, fake_opener):
    resp = client.get("/health")
    assert resp.status_code == 200
    _assert_security_headers(resp)


def test_security_headers_on_an_unauthorised_response(client, fake_opener):
    resp = client.get("/metadata/report")
    assert resp.status_code == 401
    _assert_security_headers(resp)


def test_security_headers_on_a_not_implemented_response(client, fake_opener):
    resp = client.get("/definitely-not-a-route")
    assert resp.status_code == 501
    _assert_security_headers(resp)


def test_security_headers_on_the_generic_500_handler(settings_env, tmp_path):
    """The catch-all `Exception` handler sits outside the middleware stack
    (see the `test_unexpected_exception_still_carries_provenance_headers`
    round-1 fix in `test_netnl_errors.py`) — security headers must be added
    there directly too, not only by the `security_headers` middleware.
    """
    from starlette.testclient import TestClient

    from conftest import add_test_credential
    from netnl.api import create_app
    from netnl.settings import load

    env = dict(settings_env)
    env["NETNL_DB"] = str(tmp_path / "crash.sqlite3")
    local_settings = load(env)

    def crashing_opener(method, url, body, headers, timeout):
        raise RuntimeError("boom")

    app = create_app(local_settings, opener=crashing_opener)
    add_test_credential(app, "tenant", "secret")
    client = TestClient(app, raise_server_exceptions=False)

    resp = client.get("/metadata/report", headers=basic_auth_header("tenant", "secret"))
    assert resp.status_code == 500
    _assert_security_headers(resp)


def test_security_txt_absent_without_env_var_acts_like_no_route(client, fake_opener):
    resp = client.get("/.well-known/security.txt")
    assert resp.status_code == 501
    assert resp.json()["error"]["label"] == "not-implemented"


def test_security_txt_served_when_configured(settings_env, tmp_path, clock):
    from netnl.api import create_app
    from netnl.settings import load
    from starlette.testclient import TestClient

    env = dict(settings_env)
    env["NETNL_DB"] = str(tmp_path / "securitytxt.sqlite3")
    env["NETNL_SECURITY_CONTACT"] = "mailto:security@example.org"
    local_settings = load(env)
    app = create_app(local_settings, now=clock)
    client = TestClient(app, raise_server_exceptions=False)

    resp = client.get("/.well-known/security.txt")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain")
    assert "Contact: mailto:security@example.org" in resp.text
    assert "Expires: 2027-01-01T00:00:00Z" in resp.text


def test_security_txt_needs_no_credential(settings_env, tmp_path):
    from netnl.api import create_app
    from netnl.settings import load
    from starlette.testclient import TestClient

    env = dict(settings_env)
    env["NETNL_DB"] = str(tmp_path / "securitytxt-anon.sqlite3")
    env["NETNL_SECURITY_CONTACT"] = "mailto:security@example.org"
    local_settings = load(env)
    app = create_app(local_settings)
    client = TestClient(app, raise_server_exceptions=False)

    resp = client.get("/.well-known/security.txt")
    assert resp.status_code == 200


def test_security_txt_carries_security_headers_too(settings_env, tmp_path):
    from netnl.api import create_app
    from netnl.settings import load
    from starlette.testclient import TestClient

    env = dict(settings_env)
    env["NETNL_DB"] = str(tmp_path / "securitytxt-headers.sqlite3")
    env["NETNL_SECURITY_CONTACT"] = "mailto:security@example.org"
    local_settings = load(env)
    app = create_app(local_settings)
    client = TestClient(app, raise_server_exceptions=False)

    resp = client.get("/.well-known/security.txt")
    _assert_security_headers(resp)
