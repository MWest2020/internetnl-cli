from __future__ import annotations


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
