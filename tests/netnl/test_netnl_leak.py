from __future__ import annotations

import base64
import logging

from fakes import raising_opener

from conftest import queue_json
from internetnl_cli.errors import TransportError

_UPSTREAM_USERNAME = "upstream-user"
_UPSTREAM_PASSWORD = "upstream-secret"
_UPSTREAM_BASIC = base64.b64encode(f"{_UPSTREAM_USERNAME}:{_UPSTREAM_PASSWORD}".encode()).decode()


def _assert_no_leak(resp, caplog):
    haystacks = [resp.text, str(dict(resp.headers)), caplog.text]
    for haystack in haystacks:
        assert _UPSTREAM_USERNAME not in haystack
        assert _UPSTREAM_PASSWORD not in haystack
        assert _UPSTREAM_BASIC not in haystack


def test_upstream_401_never_leaks_credential(client, fake_opener, tenant, caplog):
    caplog.set_level(logging.DEBUG, logger="netnl")
    caplog.set_level(logging.DEBUG, logger="uvicorn")

    queue_json(fake_opener, {"api_version": "2.6.0", "error": {"label": "unauthorised", "msg": "x"}}, status=401)
    resp = client.post(
        "/requests", json={"type": "web", "domains": ["example.nl"]}, headers=tenant["headers"]
    )

    assert resp.status_code == 502
    assert resp.json()["error"]["label"] == "upstream-error"
    _assert_no_leak(resp, caplog)
    # The upstream hostname is allowed to appear (it names the failure);
    # the credential must not.
    assert "batch.internal" in resp.json()["error"]["msg"]


def test_upstream_transport_failure_never_leaks_credential(settings_env, tmp_path, caplog):
    caplog.set_level(logging.DEBUG, logger="netnl")
    caplog.set_level(logging.DEBUG, logger="uvicorn")

    from starlette.testclient import TestClient

    from conftest import add_test_credential, basic_auth_header
    from netnl.api import create_app
    from netnl.settings import load

    # A DB path of its own — distinct from other tests' `settings` fixture
    # instance, so this credential insert cannot collide with theirs.
    env = dict(settings_env)
    env["NETNL_DB"] = str(tmp_path / "leak-transport.sqlite3")
    settings = load(env)

    failing_opener = raising_opener(
        TransportError("connection refused while contacting batch.internal")
    )
    failing_app = create_app(settings, opener=failing_opener)
    add_test_credential(failing_app, "tenant", "tenant-secret")
    failing_client = TestClient(failing_app, raise_server_exceptions=False)

    resp = failing_client.post(
        "/requests",
        json={"type": "web", "domains": ["example.nl"]},
        headers=basic_auth_header("tenant", "tenant-secret"),
    )

    assert resp.status_code == 502
    assert resp.json()["error"]["label"] == "upstream-unreachable"
    _assert_no_leak(resp, caplog)
    assert "batch.internal" in resp.json()["error"]["msg"]
