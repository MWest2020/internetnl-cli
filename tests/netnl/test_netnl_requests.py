from __future__ import annotations

import json
import re

from fakes import REQUEST_ID, RESULTS_REPLY, STATUS_DONE

from conftest import queue_json

_FACADE_ID_RE = re.compile(r"^[a-f0-9]{32}$")


def test_submit_returns_v2_shaped_reply_with_facade_id(client, app, fake_opener, tenant, conn):
    from fakes import REGISTER_REPLY

    queue_json(fake_opener, REGISTER_REPLY)
    resp = client.post(
        "/requests",
        json={"type": "web", "domains": ["example.nl"]},
        headers=tenant["headers"],
    )
    assert resp.status_code == 200
    body = resp.json()
    facade_id = body["request"]["request_id"]
    assert _FACADE_ID_RE.fullmatch(facade_id)
    assert facade_id != REQUEST_ID

    # Everything else from the upstream reply is untouched.
    assert body["request"]["name"] == REGISTER_REPLY["request"]["name"]
    assert body["request"]["status"] == REGISTER_REPLY["request"]["status"]

    row = conn.execute(
        "SELECT upstream_id FROM requests WHERE facade_id = ?", (facade_id,)
    ).fetchone()
    assert row["upstream_id"] == REQUEST_ID


def test_status_and_results_return_the_same_facade_id(client, fake_opener, tenant):
    from fakes import REGISTER_REPLY

    queue_json(fake_opener, REGISTER_REPLY)
    submit = client.post(
        "/requests", json={"type": "web", "domains": ["example.nl"]}, headers=tenant["headers"]
    )
    facade_id = submit.json()["request"]["request_id"]

    queue_json(fake_opener, STATUS_DONE)
    status = client.get(f"/requests/{facade_id}", headers=tenant["headers"])
    assert status.status_code == 200
    assert status.json()["request"]["request_id"] == facade_id

    queue_json(fake_opener, RESULTS_REPLY)
    results = client.get(f"/requests/{facade_id}/results", headers=tenant["headers"])
    assert results.status_code == 200
    assert results.json()["request"]["request_id"] == facade_id


def test_results_domains_are_unmodified_passthrough(client, fake_opener, tenant):
    from fakes import REGISTER_REPLY

    queue_json(fake_opener, REGISTER_REPLY)
    submit = client.post(
        "/requests", json={"type": "web", "domains": ["example.nl"]}, headers=tenant["headers"]
    )
    facade_id = submit.json()["request"]["request_id"]

    queue_json(fake_opener, RESULTS_REPLY)
    results = client.get(f"/requests/{facade_id}/results", headers=tenant["headers"])
    body = results.json()

    assert body["domains"] == RESULTS_REPLY["domains"]
    assert json.dumps(body["domains"], sort_keys=True) == json.dumps(
        RESULTS_REPLY["domains"], sort_keys=True
    )


def test_upstream_request_id_never_leaves_the_facade(client, fake_opener, tenant):
    from fakes import REGISTER_REPLY

    queue_json(fake_opener, REGISTER_REPLY)
    submit = client.post(
        "/requests", json={"type": "web", "domains": ["example.nl"]}, headers=tenant["headers"]
    )
    facade_id = submit.json()["request"]["request_id"]

    queue_json(fake_opener, RESULTS_REPLY)
    results = client.get(f"/requests/{facade_id}/results", headers=tenant["headers"])

    assert REQUEST_ID not in json.dumps(submit.json())
    assert REQUEST_ID not in json.dumps(results.json())
    for value in list(submit.headers.values()) + list(results.headers.values()):
        assert REQUEST_ID not in value


def test_submit_without_auth_is_401_with_www_authenticate(client):
    resp = client.post("/requests", json={"type": "web", "domains": ["example.nl"]})
    assert resp.status_code == 401
    assert resp.json()["error"]["label"] == "unauthorised"
    assert resp.headers["WWW-Authenticate"] == 'Basic realm="netnl"'


def test_invalid_submit_body_is_400(client, tenant):
    resp = client.post("/requests", json={"type": "web", "domains": []}, headers=tenant["headers"])
    assert resp.status_code == 400
    assert resp.json()["error"]["label"] == "bad-request"


def test_upstream_calls_carry_the_facade_user_agent(client, fake_opener, tenant):
    """openspec/changes/facade-followups: every call the facade makes
    upstream must be distinguishable from a directly-run CLI in the
    upstream instance's own logs, without changing anything else the
    unmodified `BatchClient` would have sent."""
    from fakes import REGISTER_REPLY

    queue_json(fake_opener, REGISTER_REPLY)
    resp = client.post(
        "/requests", json={"type": "web", "domains": ["example.nl"]}, headers=tenant["headers"]
    )
    assert resp.status_code == 200

    assert len(fake_opener.calls) == 1
    _method, _url, _body, headers, _timeout = fake_opener.calls[0]
    user_agent = headers["User-Agent"]
    assert user_agent.startswith("netnl/")
    assert "internetnl-cli/" in user_agent
    assert headers["Content-Type"] == "application/json"
    assert headers["Accept"] == "application/json"
    assert "Authorization" in headers
