"""`POST /webhooks/bmc` — route ordering, idempotency, persist-then-mail
with revoke-on-failure, and the privacy/audit invariants from openspec/
changes/add-supporter-issuance.
"""

from __future__ import annotations

import base64
import dataclasses
import json
from decimal import Decimal

import pytest
from starlette.testclient import TestClient

from netnl.api import create_app
from netnl.mail import DeliveryError
from netnl.settings import load

from conftest import (
    SUPPORTER_SECRET,
    add_test_credential,
    basic_auth_header,
    bmc_payload,
    post_webhook,
    queue_json,
    sign_body,
)
from fakes import REGISTER_REPLY
from netnl import store


def _counts(conn) -> tuple[int, int, int]:
    credentials = conn.execute("SELECT COUNT(*) AS n FROM credentials").fetchone()["n"]
    issuance = conn.execute("SELECT COUNT(*) AS n FROM supporter_issuance").fetchone()["n"]
    audit = conn.execute("SELECT COUNT(*) AS n FROM audit").fetchone()["n"]
    return credentials, issuance, audit


# --- opt-in / method registration ------------------------------------------


def test_route_absent_when_not_configured(client):
    # `client` (conftest.py's plain fixture) has no supporter config.
    resp = client.post("/webhooks/bmc", content=b"{}")
    assert resp.status_code == 501


def test_only_post_is_registered(supporter_client):
    resp = supporter_client.get("/webhooks/bmc")
    assert resp.status_code == 501  # not 405 — acts like the path does not exist


# --- signature verification ---------------------------------------------


def test_missing_signature_rejected_with_no_side_effects(supporter_client, conn):
    before = _counts(conn)
    resp = supporter_client.post(
        "/webhooks/bmc", content=json.dumps(bmc_payload()).encode(),
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 401
    assert _counts(conn) == before


def test_invalid_signature_rejected_with_no_side_effects(supporter_client, conn):
    before = _counts(conn)
    body = json.dumps(bmc_payload()).encode()
    resp = supporter_client.post(
        "/webhooks/bmc", content=body,
        headers={"Content-Type": "application/json", "X-Signature-Sha256": "0" * 64},
    )
    assert resp.status_code == 401
    assert _counts(conn) == before


def test_oversized_body_rejected_before_signature_is_ever_checked(supporter_settings, fake_opener, clock, recording_sender):
    from starlette.testclient import TestClient
    from netnl.api import create_app
    import dataclasses

    small_cap_settings = dataclasses.replace(
        supporter_settings,
        supporter=dataclasses.replace(supporter_settings.supporter, max_body_bytes=16),
    )
    app = create_app(small_cap_settings, opener=fake_opener, now=clock, sender=recording_sender)
    client = TestClient(app, raise_server_exceptions=False)

    payload = bmc_payload()
    body = json.dumps(payload).encode()
    assert len(body) > 16
    # A deliberately *correct* signature for the oversized body — proves
    # rejection happens on size before signature verification is even
    # attempted (an attacker without the secret could not produce this).
    resp = client.post(
        "/webhooks/bmc", content=body,
        headers={"Content-Type": "application/json", "X-Signature-Sha256": sign_body(body)},
    )
    assert resp.status_code == 400


# --- parsing --------------------------------------------------------------


def test_malformed_json_body_is_400(supporter_client):
    body = b"not json at all"
    resp = supporter_client.post(
        "/webhooks/bmc", content=body,
        headers={"Content-Type": "application/json", "X-Signature-Sha256": sign_body(body)},
    )
    assert resp.status_code == 400


def test_malformed_field_names_only_the_field(supporter_client):
    payload = bmc_payload()
    del payload["data"]["transaction_id"]
    resp = post_webhook(supporter_client, payload)
    assert resp.status_code == 400
    assert "transaction_id" in resp.json()["error"]["msg"]


# --- qualification / filters -----------------------------------------------


@pytest.mark.parametrize(
    "mutate",
    [
        lambda p: p.__setitem__("type", "member.created"),
        lambda p: p.__setitem__("live_mode", False),
    ],
)
def test_filtered_deliveries_write_nothing_and_answer_ignored(
    supporter_client, conn, recording_sender, mutate
):
    payload = bmc_payload()
    mutate(payload)
    before = _counts(conn)
    resp = post_webhook(supporter_client, payload)
    assert resp.status_code == 200
    assert resp.json() == {"status": "ignored"}
    assert _counts(conn) == before
    assert recording_sender.sent == []


def test_amount_below_minimum_is_ignored(supporter_settings, fake_opener, clock, recording_sender):
    raised_min = dataclasses.replace(
        supporter_settings,
        supporter=dataclasses.replace(supporter_settings.supporter, min_amount=Decimal("10")),
    )
    app = create_app(raised_min, opener=fake_opener, now=clock, sender=recording_sender)
    client = TestClient(app, raise_server_exceptions=False)
    conn = store.connect(raised_min.db)
    try:
        before = _counts(conn)
        resp = post_webhook(client, bmc_payload(data={"amount": "1.00"}))
        assert resp.status_code == 200
        assert resp.json() == {"status": "ignored"}
        assert _counts(conn) == before
        assert recording_sender.sent == []
    finally:
        conn.close()


def test_currency_mismatch_is_ignored(supporter_settings, fake_opener, clock, recording_sender):
    eur_only = dataclasses.replace(
        supporter_settings,
        supporter=dataclasses.replace(supporter_settings.supporter, currency="EUR"),
    )
    app = create_app(eur_only, opener=fake_opener, now=clock, sender=recording_sender)
    client = TestClient(app, raise_server_exceptions=False)
    conn = store.connect(eur_only.db)
    try:
        before = _counts(conn)
        resp = post_webhook(client, bmc_payload(data={"currency": "USD"}))
        assert resp.status_code == 200
        assert resp.json() == {"status": "ignored"}
        assert _counts(conn) == before
        assert recording_sender.sent == []
    finally:
        conn.close()


def test_test_mode_accepted_when_configured(supporter_env, fake_opener, clock, recording_sender):
    env = dict(supporter_env)
    env["NETNL_BMC_ACCEPT_TEST_MODE"] = "1"
    settings = load(env)
    app = create_app(settings, opener=fake_opener, now=clock, sender=recording_sender)
    client = TestClient(app, raise_server_exceptions=False)

    resp = post_webhook(client, bmc_payload(live_mode=False))
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
    assert len(recording_sender.sent) == 1


# --- no usable recipient: undeliverable, not issued -------------------------


def test_missing_email_is_undeliverable_no_mail_no_credential(supporter_client, conn, recording_sender):
    payload = bmc_payload()
    del payload["data"]["email"]
    resp = post_webhook(supporter_client, payload)

    assert resp.status_code == 200
    assert resp.json() == {"status": "ignored"}
    assert recording_sender.sent == []

    row = store.find_issuance(conn, payload["data"]["transaction_id"])
    assert row is not None
    assert row["state"] == "undeliverable"
    # No credential minted for this transaction.
    assert conn.execute("SELECT COUNT(*) AS n FROM credentials").fetchone()["n"] == 0


def test_header_injection_shaped_email_never_reaches_mail(supporter_client, conn, recording_sender):
    payload = bmc_payload(data={"email": "a@example.org\r\nBcc: x@evil.example"})
    resp = post_webhook(supporter_client, payload)

    assert resp.status_code == 200
    assert resp.json() == {"status": "ignored"}
    assert recording_sender.sent == []

    row = store.find_issuance(conn, payload["data"]["transaction_id"])
    assert row["state"] == "undeliverable"


# --- issuance, end to end ---------------------------------------------------


def test_issued_credential_authenticates_on_the_real_authenticated_surface(
    supporter_client, recording_sender, fake_opener
):
    resp = post_webhook(supporter_client, bmc_payload())
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}

    assert len(recording_sender.sent) == 1
    mail_sent = recording_sender.sent[0]
    assert mail_sent.to == "supporter@example.org"

    # Pull the generated username/password out of the mail body (there is
    # no other channel to get them from — this mirrors how a real donor
    # would).
    lines = {line.split(":", 1)[0].strip(): line.split(":", 1)[1].strip() for line in mail_sent.body.splitlines() if ":" in line}
    username = lines["Username"]
    password = lines["Password"]

    token = base64.b64encode(f"{username}:{password}".encode()).decode()
    queue_json(fake_opener, REGISTER_REPLY)
    submit_resp = supporter_client.post(
        "/requests",
        json={"type": "web", "domains": ["example.nl"]},
        headers={"Authorization": f"Basic {token}"},
    )
    assert submit_resp.status_code == 200


def test_duplicate_transaction_after_delivered_is_a_safe_no_op(
    supporter_client, recording_sender, conn
):
    payload = bmc_payload()
    resp1 = post_webhook(supporter_client, payload)
    assert resp1.status_code == 200
    assert len(recording_sender.sent) == 1

    credentials_after_first = conn.execute("SELECT COUNT(*) AS n FROM credentials").fetchone()["n"]

    resp2 = post_webhook(supporter_client, payload)
    assert resp2.status_code == 200
    assert resp2.json() == {"status": "ok"}
    # No second mail, no second credential.
    assert len(recording_sender.sent) == 1
    assert conn.execute("SELECT COUNT(*) AS n FROM credentials").fetchone()["n"] == credentials_after_first


# --- delivery failure, retry, and the attempt ceiling -----------------------


def test_mail_failure_revokes_the_credential_and_answers_503(
    supporter_client, recording_sender, conn
):
    recording_sender.fail_always = True
    payload = bmc_payload()
    resp = post_webhook(supporter_client, payload)
    assert resp.status_code == 503
    assert resp.json()["error"]["label"] == "delivery-failed"

    row = store.find_issuance(conn, payload["data"]["transaction_id"])
    assert row["state"] == "failed"
    assert row["attempts"] == 1
    cred = store.find_credential(conn, row["username"])
    assert cred["revoked_at"] is not None


def test_retry_after_failure_mints_a_fresh_key_and_revokes_the_old_one(
    supporter_client, recording_sender, conn
):
    recording_sender.fail_always = True
    payload = bmc_payload()
    post_webhook(supporter_client, payload)
    row1 = store.find_issuance(conn, payload["data"]["transaction_id"])
    old_username = row1["username"]

    recording_sender.fail_always = False
    resp2 = post_webhook(supporter_client, payload)
    assert resp2.status_code == 200
    assert resp2.json() == {"status": "ok"}

    row2 = store.find_issuance(conn, payload["data"]["transaction_id"])
    assert row2["username"] != old_username
    assert row2["state"] == "delivered"

    old_cred = store.find_credential(conn, old_username)
    assert old_cred["revoked_at"] is not None
    new_cred = store.find_credential(conn, row2["username"])
    assert new_cred["revoked_at"] is None


def test_exhausting_max_attempts_parks_the_transaction(
    supporter_env, fake_opener, clock, recording_sender
):
    env = dict(supporter_env)
    env["NETNL_SUPPORTER_MAX_ATTEMPTS"] = "2"
    settings = load(env)
    recording_sender.fail_always = True
    app = create_app(settings, opener=fake_opener, now=clock, sender=recording_sender)
    client = TestClient(app, raise_server_exceptions=False)
    conn = store.connect(settings.db)
    try:
        payload = bmc_payload()
        r1 = post_webhook(client, payload)
        assert r1.status_code == 503
        r2 = post_webhook(client, payload)
        assert r2.status_code == 503
        row = store.find_issuance(conn, payload["data"]["transaction_id"])
        assert row["attempts"] == 2

        mails_before = len(recording_sender.sent)
        r3 = post_webhook(client, payload)
        assert r3.status_code == 503
        assert r3.json()["error"]["label"] == "delivery-failed"
        # Parked: no further mail attempt, no third mint.
        assert len(recording_sender.sent) == mails_before
    finally:
        conn.close()


# --- hourly cap --------------------------------------------------------------


def test_hourly_cap_rejects_a_new_transaction_without_minting(
    supporter_env, fake_opener, clock, recording_sender
):
    env = dict(supporter_env)
    env["NETNL_SUPPORTER_MAX_PER_HOUR"] = "1"
    settings = load(env)
    app = create_app(settings, opener=fake_opener, now=clock, sender=recording_sender)
    client = TestClient(app, raise_server_exceptions=False)
    conn = store.connect(settings.db)
    try:
        resp1 = post_webhook(client, bmc_payload(data={"transaction_id": "txn-first"}))
        assert resp1.status_code == 200

        credentials_after_first = conn.execute("SELECT COUNT(*) AS n FROM credentials").fetchone()["n"]

        resp2 = post_webhook(client, bmc_payload(data={"transaction_id": "txn-second"}))
        assert resp2.status_code == 503
        assert resp2.json()["error"]["label"] == "delivery-failed"
        assert conn.execute("SELECT COUNT(*) AS n FROM credentials").fetchone()["n"] == credentials_after_first
    finally:
        conn.close()


def test_hourly_cap_resets_after_the_window(supporter_env, fake_opener, clock, recording_sender):
    env = dict(supporter_env)
    env["NETNL_SUPPORTER_MAX_PER_HOUR"] = "1"
    settings = load(env)
    app = create_app(settings, opener=fake_opener, now=clock, sender=recording_sender)
    client = TestClient(app, raise_server_exceptions=False)

    resp1 = post_webhook(client, bmc_payload(data={"transaction_id": "txn-a"}))
    assert resp1.status_code == 200

    clock.advance(3601)

    resp2 = post_webhook(client, bmc_payload(data={"transaction_id": "txn-b"}))
    assert resp2.status_code == 200


# --- notify (operator notification) -----------------------------------------


def test_notify_mail_sent_after_success_and_has_no_password(
    supporter_env, fake_opener, clock, recording_sender
):
    env = dict(supporter_env)
    env["NETNL_SUPPORTER_NOTIFY"] = "operator@example.org"
    settings = load(env)
    app = create_app(settings, opener=fake_opener, now=clock, sender=recording_sender)
    client = TestClient(app, raise_server_exceptions=False)

    resp = post_webhook(client, bmc_payload())
    assert resp.status_code == 200
    assert len(recording_sender.sent) == 2
    notify_mail = recording_sender.sent[1]
    assert notify_mail.to == "operator@example.org"
    assert "password" not in notify_mail.body.lower()
    assert "supporter@example.org" not in notify_mail.body  # no supporter PII either


def test_notify_failure_is_non_fatal(supporter_env, fake_opener, clock):
    env = dict(supporter_env)
    env["NETNL_SUPPORTER_NOTIFY"] = "operator@example.org"
    settings = load(env)

    calls = []

    def flaky_sender(mail_obj):
        calls.append(mail_obj)
        if mail_obj.to == "operator@example.org":
            raise DeliveryError("nope")

    app = create_app(settings, opener=fake_opener, now=clock, sender=flaky_sender)
    client = TestClient(app, raise_server_exceptions=False)

    resp = post_webhook(client, bmc_payload())
    # The donor's own delivery still succeeds even though the notify mail
    # failed.
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
    assert len(calls) == 2

    conn = store.connect(settings.db)
    try:
        row = store.find_issuance(conn, bmc_payload()["data"]["transaction_id"])
        assert row["state"] == "delivered"
    finally:
        conn.close()


# --- security headers --------------------------------------------------------


@pytest.mark.parametrize(
    "make_request",
    [
        lambda client: post_webhook(client, bmc_payload(data={"transaction_id": "txn-headers-ok"})),
        lambda client: client.post("/webhooks/bmc", content=b"{}"),  # 401
        lambda client: client.get("/webhooks/bmc"),  # 501
    ],
)
def test_security_headers_present_on_every_webhook_reply(supporter_client, make_request):
    resp = make_request(supporter_client)
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    assert resp.headers["X-Frame-Options"] == "DENY"
    assert "Content-Security-Policy" in resp.headers
    assert resp.headers["X-Netnl-Instance"] == "netnl"
