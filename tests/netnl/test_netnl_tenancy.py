from __future__ import annotations

from datetime import datetime, timezone

from fakes import REGISTER_REPLY, RESULTS_REPLY, STATUS_DONE

from conftest import add_test_credential, basic_auth_header, queue_json
from netnl import store


def _submit_as(client, fake_opener, headers) -> str:
    queue_json(fake_opener, REGISTER_REPLY)
    resp = client.post(
        "/requests", json={"type": "web", "domains": ["example.nl"]}, headers=headers
    )
    assert resp.status_code == 200
    return resp.json()["request"]["request_id"]


def test_foreign_id_is_indistinguishable_from_unknown(client, app, fake_opener, tenant):
    other_username, other_password = "other-tenant", "other-secret"
    add_test_credential(app, other_username, other_password)
    other_headers = basic_auth_header(other_username, other_password)

    facade_id = _submit_as(client, fake_opener, tenant["headers"])

    foreign_status = client.get(f"/requests/{facade_id}", headers=other_headers)
    foreign_results = client.get(f"/requests/{facade_id}/results", headers=other_headers)

    random_id = "0" * 32
    unknown_status = client.get(f"/requests/{random_id}", headers=other_headers)
    unknown_results = client.get(f"/requests/{random_id}/results", headers=other_headers)

    assert foreign_status.status_code == 404 == unknown_status.status_code
    assert foreign_results.status_code == 404 == unknown_results.status_code
    assert foreign_status.json() == unknown_status.json()
    assert foreign_results.json() == unknown_results.json()
    assert foreign_status.json()["error"]["label"] == "unknown-request"

    # The owning tenant can still reach it.
    queue_json(fake_opener, STATUS_DONE)
    own = client.get(f"/requests/{facade_id}", headers=tenant["headers"])
    assert own.status_code == 200


def test_revoked_credential_is_rejected_immediately(client, app, fake_opener, tenant, conn):
    facade_id = _submit_as(client, fake_opener, tenant["headers"])

    revoked_at = store.utcnow_iso(lambda: datetime.now(timezone.utc))
    assert store.revoke_credential(conn, tenant["username"], revoked_at) is True

    resp = client.get(f"/requests/{facade_id}", headers=tenant["headers"])
    assert resp.status_code == 401
    assert resp.json()["error"]["label"] == "unauthorised"
