from __future__ import annotations

import json

from conftest import queue_json


def test_unimplemented_list_requests_is_501(client):
    resp = client.get("/requests")
    assert resp.status_code == 501
    body = resp.json()
    assert body["api_version"]
    assert body["error"]["label"] == "not-implemented"


def test_unimplemented_patch_is_501(client):
    resp = client.patch("/requests/" + "a" * 32)
    assert resp.status_code == 501
    assert resp.json()["error"]["label"] == "not-implemented"


def test_openapi_json_is_501_not_leaked(client):
    resp = client.get("/openapi.json")
    assert resp.status_code == 501
    assert resp.json()["error"]["label"] == "not-implemented"


def test_docs_is_501_not_leaked(client):
    resp = client.get("/docs")
    assert resp.status_code == 501
    assert resp.json()["error"]["label"] == "not-implemented"


def test_every_reply_carries_provenance_headers(client, settings):
    for resp in (client.get("/requests"), client.get("/docs")):
        assert resp.headers["X-Netnl-Instance"] == settings.instance
        assert resp.headers["X-Netnl-Notice"] == (
            "independent instance; not internet.nl and not Platform Internetstandaarden"
        )


def test_error_body_is_always_v2_shaped(client):
    for resp in (client.get("/requests"), client.get("/docs"), client.patch("/requests/x")):
        body = resp.json()
        assert set(body.keys()) == {"api_version", "error"}
        assert set(body["error"].keys()) == {"label", "msg"}
        assert isinstance(body["error"]["label"], str)
        assert isinstance(body["error"]["msg"], str)


def test_unexpected_exception_still_carries_provenance_headers(settings_env, tmp_path):
    """Round-1 fix (M4): Starlette's `ServerErrorMiddleware` sits outside
    the `@app.middleware("http")` stack, so the catch-all 500 must add the
    provenance headers itself — proven here with an opener that raises a
    plain exception `call_upstream` does not translate (not `ApiError`/
    `TransportError`), so it reaches the generic handler.
    """
    from starlette.testclient import TestClient

    from conftest import add_test_credential, basic_auth_header
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
    assert resp.json()["error"]["label"] == "server-error"
    assert resp.headers["X-Netnl-Instance"] == local_settings.instance
    assert "X-Netnl-Notice" in resp.headers


def test_upstream_503_passes_through_with_its_own_status(client, fake_opener, tenant):
    """Round-1 fix (M5): only 401/403 are forced to 502; every other
    non-2xx upstream status reaches the tenant unchanged.
    """
    queue_json(
        fake_opener,
        {"api_version": "2.6.0", "error": {"label": "server-error", "msg": "maintenance"}},
        status=503,
    )
    resp = client.get("/metadata/report", headers=tenant["headers"])
    assert resp.status_code == 503
    assert resp.json()["error"]["label"] == "upstream-error"
    assert json.dumps(resp.json())  # v2-shaped, JSON-serialisable
