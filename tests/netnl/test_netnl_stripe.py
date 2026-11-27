"""`POST /webhooks/stripe` (openspec/changes/add-bazibo-unlock D12): Bazibo
codes from a Stripe Payment Link, with signed fixture payloads."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from datetime import timedelta

import pytest
from starlette.testclient import TestClient

from netnl import bazibo, store, stripe_webhook
from netnl.api import create_app
from netnl.errors import SettingsError
from netnl.settings import load

from test_netnl_bazibo import TEST_PUB, _code_from_mail, _reset_rate_limit, bazibo_env  # noqa: F401

WHSEC = "whsec_test_0123456789abcdefghijklmnopqrstuv"
PLINK = "plink_1TestLinkAbc"
BUYER = "ouder@example.org"


def session_event(*, sid="cs_test_a1", link=PLINK, paid=True, live=True, email=BUYER,
                  type_="checkout.session.completed", mode="payment"):
    obj = {
        "id": sid, "object": "checkout.session", "mode": mode, "status": "complete",
        "payment_status": "paid" if paid else "unpaid", "payment_link": link, "livemode": live,
        "amount_total": 100, "currency": "eur",
        "customer_details": {"email": email, "name": "Sam", "address": {"country": "NL"}},
    }
    return {"id": "evt_1", "object": "event", "type": type_, "livemode": live,
            "created": 1764200000, "data": {"object": obj}}


def sign(body: bytes, ts: int, secret: str = WHSEC) -> str:
    sig = hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    return f"t={ts},v1={sig}"


def post(client, clock, event, *, secret=WHSEC, ts_offset=0, header=None):
    body = json.dumps(event).encode()
    ts = int(clock().timestamp()) + ts_offset
    return client.post("/webhooks/stripe", content=body, headers={
        "Content-Type": "application/json",
        "Stripe-Signature": header if header is not None else sign(body, ts, secret),
    })


@pytest.fixture
def s_env(bazibo_env) -> dict:
    env = dict(bazibo_env, NETNL_STRIPE_WEBHOOK_SECRET=WHSEC, NETNL_STRIPE_PAYMENT_LINK_ID=PLINK)
    env.pop("NETNL_BAZIBO_EXTRA_ID")
    return env


@pytest.fixture
def s_app(s_env, fake_opener, clock, recording_sender):
    return create_app(load(s_env), opener=fake_opener, now=clock, sender=recording_sender)


@pytest.fixture
def s_client(s_app):
    return TestClient(s_app, raise_server_exceptions=False)


@pytest.fixture
def s_conn(s_app):
    c = store.connect(s_app.state.settings.db)
    yield c
    c.close()


def codes(sender):
    return [m for m in sender.sent if bazibo.PREFIX in m.body]


# --- the signature ------------------------------------------------------------


def test_good_signature_mints_and_mails_one_code(s_client, s_conn, clock, recording_sender):
    resp = post(s_client, clock, session_event())
    assert resp.status_code == 200 and resp.json() == {"status": "ok"}
    [m] = recording_sender.sent
    assert m.to == BUYER
    verified = bazibo.verify(TEST_PUB, _code_from_mail(m))
    row = store.find_bazibo_issuance(s_conn, "cs_test_a1")
    assert row["state"] == "delivered" and row["code_id"] == verified.code_id_hex
    assert s_conn.execute("SELECT COUNT(*) AS n FROM credentials").fetchone()["n"] == 0


@pytest.mark.parametrize("header", [
    " ", "garbage", "t=abc,v1=00", "v1=" + "0" * 64, "t=1",
])
def test_malformed_signature_header_is_401(s_client, s_conn, clock, recording_sender, header):
    resp = post(s_client, clock, session_event(), header=header)
    assert resp.status_code == 401
    assert recording_sender.sent == [] and store.find_bazibo_issuance(s_conn, "cs_test_a1") is None


def test_bad_signature_is_401_and_writes_nothing(s_client, s_conn, clock, recording_sender):
    resp = post(s_client, clock, session_event(), secret="whsec_wrong")
    assert resp.status_code == 401
    assert recording_sender.sent == []
    assert s_conn.execute("SELECT COUNT(*) AS n FROM bazibo_watch").fetchone()["n"] == 0


def test_a_changed_body_breaks_the_signature(s_client, clock, recording_sender):
    body = json.dumps(session_event()).encode()
    header = sign(body, int(clock().timestamp()))
    tampered = body.replace(BUYER.encode(), b"evil@example.org")
    resp = s_client.post("/webhooks/stripe", content=tampered, headers={"Stripe-Signature": header})
    assert resp.status_code == 401 and recording_sender.sent == []


@pytest.mark.parametrize("offset", [-301, 301, -3600])
def test_stale_or_future_timestamp_is_401(s_client, clock, recording_sender, offset):
    assert post(s_client, clock, session_event(), ts_offset=offset).status_code == 401
    assert recording_sender.sent == []


def test_within_tolerance_and_a_second_v1_are_accepted(s_client, clock, recording_sender):
    body = json.dumps(session_event()).encode()
    ts = int(clock().timestamp()) - 299
    good = sign(body, ts).split(",v1=")[1]
    header = f"t={ts},v1={'0' * 64},v1={good},v0=ignored"
    resp = s_client.post("/webhooks/stripe", content=body, headers={"Stripe-Signature": header})
    assert resp.status_code == 200 and len(codes(recording_sender)) == 1


def test_verify_signature_unit():
    raw = b'{"a":1}'
    header = sign(raw, 1000)
    assert stripe_webhook.verify_signature(WHSEC, header, raw, 1000)
    assert not stripe_webhook.verify_signature(WHSEC, header, raw, 1301)
    assert not stripe_webhook.verify_signature("whsec_other", header, raw, 1000)
    assert not stripe_webhook.verify_signature(WHSEC, None, raw, 1000)
    assert not stripe_webhook.verify_signature(WHSEC, "t=1000,v1=" + "x" * 5000, raw, 1000)


# --- what qualifies -------------------------------------------------------------


def test_another_payment_link_is_ignored(s_client, s_conn, clock, recording_sender):
    resp = post(s_client, clock, session_event(link="plink_somethingElse"))
    assert resp.status_code == 200 and resp.json() == {"status": "ignored"}
    assert recording_sender.sent == [] and store.find_bazibo_issuance(s_conn, "cs_test_a1") is None


def test_unpaid_session_is_ignored(s_client, s_conn, clock, recording_sender):
    assert post(s_client, clock, session_event(paid=False)).json() == {"status": "ignored"}
    assert recording_sender.sent == []


def test_async_payment_succeeded_mints(s_client, clock, recording_sender):
    resp = post(s_client, clock, session_event(type_="checkout.session.async_payment_succeeded"))
    assert resp.json() == {"status": "ok"} and len(codes(recording_sender)) == 1


@pytest.mark.parametrize("type_", ["payment_intent.succeeded", "charge.refunded", "checkout.session.expired"])
def test_every_other_event_is_200_and_ignored(s_client, s_conn, clock, recording_sender, type_):
    resp = post(s_client, clock, session_event(type_=type_))
    assert resp.status_code == 200 and resp.json() == {"status": "ignored"}
    assert recording_sender.sent == []


def test_subscription_mode_is_ignored(s_client, clock, recording_sender):
    assert post(s_client, clock, session_event(mode="subscription")).json() == {"status": "ignored"}


def test_test_mode_is_ignored_unless_allowed(s_env, s_client, clock, recording_sender, fake_opener):
    assert post(s_client, clock, session_event(live=False)).json() == {"status": "ignored"}
    assert recording_sender.sent == []
    env = dict(s_env, NETNL_STRIPE_ALLOW_TEST_MODE="1")
    client = TestClient(create_app(load(env), opener=fake_opener, now=clock, sender=recording_sender),
                        raise_server_exceptions=False)
    assert post(client, clock, session_event(live=False, sid="cs_test_b2")).json() == {"status": "ok"}


def test_no_email_is_undeliverable(s_client, s_conn, clock, recording_sender):
    assert post(s_client, clock, session_event(email=None)).json() == {"status": "ignored"}
    assert recording_sender.sent == []
    assert store.find_bazibo_issuance(s_conn, "cs_test_a1")["state"] == "undeliverable"


@pytest.mark.parametrize("bad", [{"data": {}}, {"data": {"object": {"id": "pi_123"}}},
                                 {"data": {"object": {"id": "cs_x\nevil"}}}])
def test_malformed_session_is_400(s_client, clock, recording_sender, bad):
    event = {"type": "checkout.session.completed", **bad}
    assert post(s_client, clock, event).status_code == 400
    assert recording_sender.sent == []


# --- idempotency, retries -------------------------------------------------------


def test_replay_sends_nothing_new(s_client, s_conn, clock, recording_sender):
    post(s_client, clock, session_event())
    for _ in range(3):
        assert post(s_client, clock, session_event()).json() == {"status": "ok"}
    assert len(recording_sender.sent) == 1
    assert s_conn.execute("SELECT COUNT(*) AS n FROM bazibo_issuance").fetchone()["n"] == 1


def test_mail_failure_is_503_and_the_retry_sends_the_same_code(s_client, s_conn, clock, recording_sender):
    recording_sender.fail_on_calls = {0}
    assert post(s_client, clock, session_event()).status_code == 503
    assert store.find_bazibo_issuance(s_conn, "cs_test_a1")["state"] == "failed"
    clock.advance(600)  # Stripe retries; the signature is fresh, the code the same
    assert post(s_client, clock, session_event()).json() == {"status": "ok"}
    assert store.find_bazibo_issuance(s_conn, "cs_test_a1")["state"] == "delivered"
    first, second = recording_sender.sent
    assert _code_from_mail(first) == _code_from_mail(second)


def test_two_sessions_two_codes(s_client, clock, recording_sender):
    post(s_client, clock, session_event(sid="cs_live_1"))
    post(s_client, clock, session_event(sid="cs_live_2"))
    a, b = (_code_from_mail(m) for m in recording_sender.sent)
    assert a != b


def test_nothing_sensitive_in_logs_or_db(s_app, s_client, clock, recording_sender, caplog):
    caplog.set_level(logging.DEBUG)
    post(s_client, clock, session_event())
    code = _code_from_mail(recording_sender.sent[0])
    dump = "\n".join(store.connect(s_app.state.settings.db).iterdump())
    for secret in (BUYER, code, WHSEC):
        assert secret not in caplog.text and secret not in dump
    assert "event=bazibo-issued source=stripe txn_id=cs_test_a1" in caplog.text


def test_watch_alerts_for_a_stripe_session(s_app, s_client, s_conn, clock, recording_sender, s_env):
    from test_netnl_bazibo_watch import watch  # the CronJob's own entry point
    env = dict(s_env, NETNL_BAZIBO_ALERT_TO="mark@example.org")
    app = create_app(load(env), opener=None, now=clock, sender=recording_sender)
    client = TestClient(app, raise_server_exceptions=False)
    conn = store.connect(app.state.settings.db)
    try:
        recording_sender.fail_always = True
        post(client, clock, session_event(sid="cs_live_w"))
        recording_sender.fail_always = False
        watch(app, conn, clock() + timedelta(minutes=16), recording_sender)
        [alert] = [m for m in recording_sender.sent if m.to == "mark@example.org"]
        assert "cs_live_w" in alert.subject and BUYER not in alert.body
        assert "--ref cs_live_w" in alert.body
    finally:
        conn.close()


# --- settings -----------------------------------------------------------------


def test_off_without_settings(bazibo_env, fake_opener, clock, recording_sender):
    client = TestClient(create_app(load(bazibo_env), opener=fake_opener, now=clock, sender=recording_sender),
                        raise_server_exceptions=False)
    assert client.post("/webhooks/stripe", content=b"{}").status_code == 501


@pytest.mark.parametrize("change, needle", [
    ({"NETNL_STRIPE_PAYMENT_LINK_ID": None}, "NETNL_STRIPE_PAYMENT_LINK_ID"),
    ({"NETNL_STRIPE_WEBHOOK_SECRET": None}, "NETNL_STRIPE_WEBHOOK_SECRET"),
    ({"NETNL_STRIPE_WEBHOOK_SECRET": "sk_live_oops"}, "NETNL_STRIPE_WEBHOOK_SECRET"),
    ({"NETNL_STRIPE_PAYMENT_LINK_ID": "https://buy.stripe.com/x"}, "NETNL_STRIPE_PAYMENT_LINK_ID"),
    ({"NETNL_STRIPE_ALLOW_TEST_MODE": "yes"}, "NETNL_STRIPE_ALLOW_TEST_MODE"),
    ({"NETNL_BAZIBO_SIGNING_KEY": None}, "NETNL_BAZIBO_SIGNING_KEY"),
    ({"NETNL_BAZIBO_DEVICE_SALT": None}, "NETNL_BAZIBO_DEVICE_SALT"),
    ({"NETNL_BMC_WEBHOOK_SECRET": None}, "NETNL_BMC_WEBHOOK_SECRET"),
])
def test_half_config_refused(s_env, change, needle):
    env = dict(s_env)
    for k, v in change.items():
        if v is None:
            env.pop(k, None)
        else:
            env[k] = v
    with pytest.raises(SettingsError) as exc:
        load(env)
    assert needle in str(exc.value)
    assert WHSEC not in str(exc.value)


def test_stripe_alone_switches_activation_on(s_env, fake_opener, clock, recording_sender):
    settings = load(s_env)
    assert settings.bazibo.enabled and settings.bazibo.extra_id is None
    assert WHSEC not in repr(settings)
    client = TestClient(create_app(settings, opener=fake_opener, now=clock, sender=recording_sender),
                        raise_server_exceptions=False)
    resp = client.post("/bazibo/activate", json={"code": "x", "device": "0" * 32},
                       headers={"Origin": "https://bazibo.nl"})
    assert resp.status_code == 400
