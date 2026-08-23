"""GET /health: anonymous liveness probe (design.md, "Facade image and
liveness"; spec.md, "Authenticated surface" — the /health exception).

No credential, no upstream call, no tenant/DB data, no version/host/
credential disclosure.
"""

from __future__ import annotations


def test_health_is_anonymous_and_ok(client, fake_opener):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_health_does_not_call_upstream(client, fake_opener):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert fake_opener.calls == []


def test_health_leaks_no_api_version_host_or_credential(client, fake_opener, settings):
    resp = client.get("/health")
    body = resp.text
    assert "api_version" not in body
    assert settings.upstream_username not in body
    assert settings.upstream_password not in body
    assert settings.upstream_endpoint not in body
    assert resp.json() == {"status": "ok"}


def test_health_provenance_headers_are_facade_own_name_only(client, fake_opener, settings):
    # The provenance middleware still stamps its own headers — that is the
    # facade's own instance name, not upstream information, and is
    # explicitly allowed by design.md.
    resp = client.get("/health")
    assert resp.headers["X-Netnl-Instance"] == settings.instance
    assert "X-Netnl-Notice" in resp.headers


def test_health_not_shadowed_by_not_implemented_catch_all(client, fake_opener):
    # A route that does not exist still gets the 501 v2-shaped
    # not-implemented body, proving /health is a real, distinct route
    # rather than something the catch-all happens to answer for.
    resp = client.get("/definitely-not-a-route")
    assert resp.status_code == 501
    assert resp.json()["error"]["label"] == "not-implemented"

    health = client.get("/health")
    assert health.status_code == 200
    assert health.json() == {"status": "ok"}


def test_health_needs_no_credential_unlike_metadata_report(client, fake_opener):
    # Companion to the "Anonymous metadata request" scenario: /metadata/report
    # stays 401 without credentials, /health does not.
    metadata = client.get("/metadata/report")
    assert metadata.status_code == 401

    health = client.get("/health")
    assert health.status_code == 200
