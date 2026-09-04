"""`POST /webhooks/bmc` — route ordering, idempotency, persist-then-mail
with revoke-on-failure, and the privacy/audit invariants from openspec/
changes/add-supporter-issuance.
"""

from __future__ import annotations

import asyncio
import base64
import dataclasses
import json
import logging
from decimal import Decimal

import pytest
from starlette.requests import ClientDisconnect
from starlette.requests import Request as StarletteRequest
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
from netnl import bmc, store, supporter


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

    # Pull the generated credential out of the mail body (there is no
    # other channel to get it from — this mirrors how a real donor would).
    credential_line = next(
        line.strip()
        for line in mail_sent.body.splitlines()
        if line.strip().startswith("INTERNETNL_CREDENTIAL=")
    )
    credential = credential_line.split("=", 1)[1]

    token = base64.b64encode(credential.encode()).decode()
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


# --- security review round: B1 (idempotency under concurrency) -------------
#
# `_persist_and_mint`/`_record_delivery_outcome` called directly (not
# through real threads — that is `test_netnl_real_server.py`'s job, see M2
# there) to deterministically reproduce and prove closed the exact
# interleaving that, without the B1(a)/(b) fixes, minted 5 credentials for
# 5 concurrent identical deliveries and left an active orphan no
# `supporter_issuance` row referenced.


def _active_credential_orphans(conn, prefix: str = "supporter-") -> set[str]:
    """Every active (non-revoked) credential under the supporter prefix
    that is *not* the current `username` of a `pending`/`delivered`
    `supporter_issuance` row — the B1 invariant this must always be empty.
    """
    active = {
        row["username"]
        for row in conn.execute(
            "SELECT username FROM credentials WHERE username LIKE ? AND revoked_at IS NULL",
            (f"{prefix}%",),
        ).fetchall()
    }
    referenced = {
        row["username"]
        for row in conn.execute(
            "SELECT username FROM supporter_issuance WHERE state IN ('pending', 'delivered')"
        ).fetchall()
    }
    return active - referenced


def _qualify_cfg(cfg):
    return bmc.QualifyConfig(
        accept_test_mode=cfg.accept_test_mode, min_amount=cfg.min_amount, currency=cfg.currency
    )


def test_pending_row_within_lease_refuses_takeover(supporter_settings, clock, conn):
    cfg = supporter_settings.supporter
    delivery = bmc.parse_delivery(bmc_payload(data={"transaction_id": "txn-lease-1"}))
    decision = bmc.qualifies(delivery, _qualify_cfg(cfg))

    username1, _password1, _sanitized1, _attempts1 = supporter._persist_and_mint(
        conn, cfg, delivery, decision, clock()
    )

    # A second call for the exact same transaction, a moment later — still
    # well within the lease (B1(a)): the first call's own mail-send has not
    # been given a chance to conclude.
    clock.advance(1)
    with pytest.raises(Exception) as exc_info:
        supporter._persist_and_mint(conn, cfg, delivery, decision, clock())
    from netnl.errors import NetnlHTTPError

    assert isinstance(exc_info.value, NetnlHTTPError)
    assert exc_info.value.status == 503

    # No takeover happened — the first mint's credential is untouched.
    cred = store.find_credential(conn, username1)
    assert cred["revoked_at"] is None
    assert _active_credential_orphans(conn) == set()


def test_pending_row_past_lease_allows_takeover(supporter_settings, clock, conn):
    cfg = supporter_settings.supporter
    delivery = bmc.parse_delivery(bmc_payload(data={"transaction_id": "txn-lease-2"}))
    decision = bmc.qualifies(delivery, _qualify_cfg(cfg))

    username1, _password1, _sanitized1, attempts1 = supporter._persist_and_mint(
        conn, cfg, delivery, decision, clock()
    )

    clock.advance(supporter._pending_lease_seconds(cfg) + 1)
    username2, _password2, _sanitized2, attempts2 = supporter._persist_and_mint(
        conn, cfg, delivery, decision, clock()
    )

    assert username2 != username1
    assert attempts2 == attempts1 + 1
    cred1 = store.find_credential(conn, username1)
    assert cred1["revoked_at"] is not None
    cred2 = store.find_credential(conn, username2)
    assert cred2["revoked_at"] is None
    assert _active_credential_orphans(conn) == set()


def test_takeover_into_undeliverable_revokes_the_previous_credential(
    supporter_settings, clock, conn
):
    """Security review fix (N4): attempt 1 mints and persists `pending`
    (a real credential minted for it), then is abandoned (its lease
    expires without ever being confirmed delivered — e.g. the process
    crashed before mailing). A later delivery for the exact same
    transaction — BMC's own retry — this time carries no usable email at
    all (`UNDELIVERABLE_NO_EMAIL`). Without this fix, the row moves to
    `undeliverable` (terminal, never revisited) while attempt 1's
    credential is simply abandoned: active, and referenced by no row ever
    again.
    """
    cfg = supporter_settings.supporter
    delivery1 = bmc.parse_delivery(bmc_payload(data={"transaction_id": "txn-undeliverable-takeover"}))
    username1, _password1, _sanitized1, _attempts1 = supporter._persist_and_mint(
        conn, cfg, delivery1, bmc.Decision.ISSUE, clock()
    )
    cred1_before = store.find_credential(conn, username1)
    assert cred1_before["revoked_at"] is None  # sanity: really minted, really active

    clock.advance(supporter._pending_lease_seconds(cfg) + 1)
    payload2 = bmc_payload(data={"transaction_id": "txn-undeliverable-takeover"})
    del payload2["data"]["email"]
    delivery2 = bmc.parse_delivery(payload2)

    outcome = supporter._persist_and_mint(
        conn, cfg, delivery2, bmc.Decision.UNDELIVERABLE_NO_EMAIL, clock()
    )
    assert outcome == "ignored"

    cred1_after = store.find_credential(conn, username1)
    assert cred1_after["revoked_at"] is not None  # N4: no longer abandoned-but-active

    row = store.find_issuance(conn, "txn-undeliverable-takeover")
    assert row["state"] == "undeliverable"
    assert row["username"] == ""
    assert _active_credential_orphans(conn) == set()


def test_b1_invariant_holds_when_a_stale_outcome_write_arrives_after_takeover(
    supporter_settings, clock, conn
):
    """The direct reproduction of the measured bug: call #1 mints and
    persists (`pending`); its mail-send is slow enough that call #2 (the
    same transaction, once the lease has expired) takes the row over,
    revoking call #1's credential and minting its own. Call #1's mail-send
    then "finally" succeeds and tries to record that outcome — using its
    own, now-stale username. Without B1(b)'s conditional write, this would
    blindly overwrite call #2's row with call #1's already-revoked
    username, marking it `delivered` and leaving call #2's actually-active
    credential referenced by nothing.
    """
    cfg = supporter_settings.supporter
    delivery = bmc.parse_delivery(bmc_payload(data={"transaction_id": "txn-race-1"}))
    decision = bmc.Decision.ISSUE

    username1, _password1, sanitized1, attempts1 = supporter._persist_and_mint(
        conn, cfg, delivery, decision, clock()
    )

    clock.advance(supporter._pending_lease_seconds(cfg) + 1)
    username2, _password2, _sanitized2, _attempts2 = supporter._persist_and_mint(
        conn, cfg, delivery, decision, clock()
    )
    assert username2 != username1

    # Call #1's stale outcome write arrives after the takeover.
    supporter._record_delivery_outcome(
        conn, txn_id=delivery.transaction_id, username=username1,
        sanitized_txn=sanitized1, attempts=attempts1, now=clock(), delivered=True,
    )

    row = store.find_issuance(conn, "txn-race-1")
    assert row["username"] == username2  # untouched by call #1's stale write
    cred1 = store.find_credential(conn, username1)
    assert cred1["revoked_at"] is not None  # never left active
    cred2 = store.find_credential(conn, username2)
    assert cred2["revoked_at"] is None  # the sole active credential
    assert _active_credential_orphans(conn) == set()


# --- security review round: undeliverable deliveries count toward the ------
# --- hourly cap too (conservative fix) --------------------------------------


def test_hourly_cap_also_bounds_undeliverable_deliveries(
    supporter_env, fake_opener, clock, recording_sender
):
    env = dict(supporter_env)
    env["NETNL_SUPPORTER_MAX_PER_HOUR"] = "1"
    settings = load(env)
    app = create_app(settings, opener=fake_opener, now=clock, sender=recording_sender)
    client = TestClient(app, raise_server_exceptions=False)
    conn = store.connect(settings.db)
    try:
        payload1 = bmc_payload(data={"transaction_id": "txn-undeliverable-1"})
        del payload1["data"]["email"]
        resp1 = post_webhook(client, payload1)
        assert resp1.status_code == 200
        assert resp1.json() == {"status": "ignored"}

        payload2 = bmc_payload(data={"transaction_id": "txn-undeliverable-2"})
        del payload2["data"]["email"]
        resp2 = post_webhook(client, payload2)
        assert resp2.status_code == 503
        assert resp2.json()["error"]["label"] == "delivery-failed"
    finally:
        conn.close()


# --- security review round: direct SHALL-evidence ---------------------------


def test_bad_signature_never_opens_a_database_connection(supporter_client, monkeypatch):
    from netnl import store as store_module

    def _boom(*args, **kwargs):
        raise AssertionError(
            "store.connect must not be called for a request with an invalid signature"
        )

    monkeypatch.setattr(store_module, "connect", _boom)
    resp = supporter_client.post(
        "/webhooks/bmc",
        content=json.dumps(bmc_payload()).encode(),
        headers={"Content-Type": "application/json", "X-Signature-Sha256": "0" * 64},
    )
    assert resp.status_code == 401


def test_oversized_chunked_body_without_content_length_is_rejected(
    supporter_settings, fake_opener, clock, recording_sender
):
    small_cap_settings = dataclasses.replace(
        supporter_settings,
        supporter=dataclasses.replace(supporter_settings.supporter, max_body_bytes=16),
    )
    app = create_app(small_cap_settings, opener=fake_opener, now=clock, sender=recording_sender)
    client = TestClient(app, raise_server_exceptions=False)

    payload = bmc_payload()
    body = json.dumps(payload).encode()
    assert len(body) > 16

    def chunks():
        for i in range(0, len(body), 4):
            yield body[i : i + 4]

    resp = client.post(
        "/webhooks/bmc",
        content=chunks(),
        headers={"Content-Type": "application/json", "X-Signature-Sha256": sign_body(body)},
    )
    # `content=<generator>` sends no Content-Length at all (verified below)
    # — the shape a real chunked-encoded request takes, which api.py's own
    # `enforce_body_size` middleware (keyed on that header) cannot see.
    assert "content-length" not in {k.lower() for k in resp.request.headers.keys()}
    assert resp.status_code == 400


# --- security review round: ClientDisconnect handled cleanly ---------------


def test_read_bounded_body_lets_client_disconnect_propagate_unwrapped():
    class _FakeStreamRequest:
        async def stream(self):
            yield b'{"type": "dono'
            raise ClientDisconnect()

    with pytest.raises(ClientDisconnect):
        asyncio.run(supporter._read_bounded_body(_FakeStreamRequest(), max_bytes=10_000))


def test_client_disconnect_mid_body_read_answers_400_not_500(supporter_client, monkeypatch, caplog):
    async def _disconnecting_stream(self):
        yield b'{"type": "dona'
        raise ClientDisconnect()

    monkeypatch.setattr(StarletteRequest, "stream", _disconnecting_stream)
    caplog.set_level(logging.WARNING, logger="netnl")

    resp = supporter_client.post(
        "/webhooks/bmc",
        content=b'{"type": "donation.created"}',
        headers={"Content-Type": "application/json", "X-Signature-Sha256": "0" * 64},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["label"] == "bad-request"
    # Never the generic 500 path.
    assert "an unexpected error occurred" not in resp.text


def test_client_disconnect_handler_uses_route_path_not_the_raw_url(
    supporter_client, monkeypatch, caplog
):
    """Security review fix (N6): the handler must log `auth._route_path
    (request)`, never `request.url.path` directly — uvicorn percent-
    decodes the raw path before Starlette ever sees it, so a crafted
    `%0A` in an attacker-chosen path segment would land as a literal
    newline in a log line built from the raw path (log injection). Proven
    by monkeypatching `auth._route_path` itself to a sentinel and
    confirming the handler's log line reflects *that*, not the request's
    own URL — i.e. the handler is actually wired through it, not through
    `request.url.path`.
    """
    from netnl import auth

    monkeypatch.setattr(auth, "_route_path", lambda request: "<sentinel-route-path>")

    async def _disconnecting_stream(self):
        yield b'{"type": "dona'
        raise ClientDisconnect()

    monkeypatch.setattr(StarletteRequest, "stream", _disconnecting_stream)
    caplog.set_level(logging.DEBUG, logger="netnl")

    resp = supporter_client.post(
        "/webhooks/bmc",
        content=b'{"type": "donation.created"}',
        headers={"Content-Type": "application/json", "X-Signature-Sha256": "0" * 64},
    )
    assert resp.status_code == 400
    assert "<sentinel-route-path>" in caplog.text
    # The real path never appears verbatim alongside it — the handler
    # went through `auth._route_path`, not `request.url.path`.
    disconnect_records = [r for r in caplog.records if "client disconnected" in r.getMessage()]
    assert disconnect_records
    assert disconnect_records[0].getMessage() == (
        "client disconnected mid-request: <sentinel-route-path>"
    )


def test_route_path_sanitizes_an_unmatched_raw_path_with_control_characters():
    """Direct unit proof of the mechanism `handle_client_disconnect` (N6)
    relies on: `auth._route_path` falls back to a printable-only,
    length-capped sanitizer of the raw path only when no route matched —
    a raw path carrying a decoded `%0A` (a literal newline, exactly what
    uvicorn would hand Starlette for a URL containing that escape) never
    reaches a log line unfiltered.
    """
    from netnl.auth import _route_path

    class _FakeURL:
        def __init__(self, path: str) -> None:
            self.path = path

    class _FakeRequest:
        def __init__(self, path: str) -> None:
            self.scope: dict = {}  # no matched route
            self.url = _FakeURL(path)

    malicious = _FakeRequest("/webhooks/bmc\ninjected: evil-header-line")
    result = _route_path(malicious)
    assert "\n" not in result


def test_route_path_uses_the_fixed_template_when_a_route_matched():
    from netnl.auth import _route_path

    class _FakeRoute:
        def __init__(self, path: str) -> None:
            self.path = path

    class _FakeURL:
        def __init__(self, path: str) -> None:
            self.path = path

    class _FakeRequest:
        def __init__(self, route_path: str, raw_path: str) -> None:
            self.scope = {"route": _FakeRoute(route_path)}
            self.url = _FakeURL(raw_path)

    # Even a raw URL carrying attacker-chosen content is irrelevant once a
    # route actually matched — only the fixed template is ever returned.
    request = _FakeRequest("/webhooks/bmc", "/webhooks/bmc?whatever\ninjected")
    assert _route_path(request) == "/webhooks/bmc"
