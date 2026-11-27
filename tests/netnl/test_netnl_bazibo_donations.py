"""Bazibo codes from plain BMC donations (openspec/changes/add-bazibo-unlock,
design.md D12; Mark, 27 Nov 2026): BMC sends this account a webhook only for
"support created", so with `NETNL_BAZIBO_FROM_DONATIONS=1` a live
`donation.created` of at least `NETNL_BAZIBO_DONATION_MIN` mints and mails a
Bazibo code, next to (never instead of) the netnl credential at
`NETNL_SUPPORTER_MIN_AMOUNT`.
"""

from __future__ import annotations

import logging

import pytest
from starlette.testclient import TestClient

from conftest import post_webhook
from netnl import bazibo, mail, store
from netnl.api import create_app
from netnl.errors import SettingsError
from netnl.settings import load

from test_netnl_bazibo import (  # noqa: F401  (fixtures reused by name)
    BUYER,
    DEVICE_SALT,
    EXTRA_ID,
    TEST_PUB,
    TEST_SEED_B64,
    _code_from_mail,
    _counts,
    _reset_rate_limit,
    bazibo_env,
    extra_payload,
)

DONOR = "donor@example.org"


def donation(*, txn="pi_don_1", amount=1.0, live=True, email_addr=DONOR, currency="EUR", **extra):
    data = {"transaction_id": txn, "amount": amount, "currency": currency,
            "support_type": "Supporter", "supporter_name": "Sam", "support_note": None}
    if email_addr is not None:
        data["supporter_email"] = email_addr
    data.update(extra)
    return {"event_id": 7, "type": "donation.created", "live_mode": live, "created": 1,
            "attempt": 1, "data": data}


@pytest.fixture
def don_env(bazibo_env) -> dict:
    env = dict(bazibo_env)
    env["NETNL_BAZIBO_FROM_DONATIONS"] = "1"
    return env


@pytest.fixture
def don_app(don_env, fake_opener, clock, recording_sender):
    return create_app(load(don_env), opener=fake_opener, now=clock, sender=recording_sender)


@pytest.fixture
def don_client(don_app):
    return TestClient(don_app, raise_server_exceptions=False)


@pytest.fixture
def don_conn(don_app):
    c = store.connect(don_app.state.settings.db)
    yield c
    c.close()


def _bazibo_mails(sender):
    return [m for m in sender.sent if bazibo.PREFIX in m.body]


def _netnl_mails(sender):
    return [m for m in sender.sent if bazibo.PREFIX not in m.body]


# --- settings ---------------------------------------------------------------


def test_off_by_default_and_min_defaults_to_one(bazibo_env):
    bz = load(bazibo_env).bazibo
    assert bz.from_donations is False
    assert str(bz.donation_min) == "1"


def test_switch_on_and_minimum_configurable(don_env):
    bz = load(dict(don_env, NETNL_BAZIBO_DONATION_MIN="3")).bazibo
    assert bz.from_donations is True and bz.enabled is True
    assert str(bz.donation_min) == "3"


def test_donations_alone_enable_the_subsystem_without_an_extra(don_env):
    env = dict(don_env)
    env.pop("NETNL_BAZIBO_EXTRA_ID")
    bz = load(env).bazibo
    assert bz.enabled is True and bz.extra_id is None


@pytest.mark.parametrize(
    "drop, add, needle",
    [
        ("NETNL_BAZIBO_SIGNING_KEY", {"NETNL_BAZIBO_EXTRA_ID": ""}, "NETNL_BAZIBO_SIGNING_KEY"),
        ("NETNL_BAZIBO_DEVICE_SALT", {}, "NETNL_BAZIBO_DEVICE_SALT"),
        (None, {"NETNL_BAZIBO_FROM_DONATIONS": "yes"}, "NETNL_BAZIBO_FROM_DONATIONS"),
        (None, {"NETNL_BAZIBO_DONATION_MIN": "-1"}, "NETNL_BAZIBO_DONATION_MIN"),
        (None, {"NETNL_BAZIBO_DONATION_MIN": "abc"}, "NETNL_BAZIBO_DONATION_MIN"),
    ],
)
def test_half_config_refused(don_env, drop, add, needle):
    env = dict(don_env)
    if drop:
        env.pop(drop)
    env.update(add)
    with pytest.raises(SettingsError) as exc:
        load(env)
    assert needle in str(exc.value)


def test_without_the_bridge_refused(don_env):
    env = dict(don_env)
    env.pop("NETNL_BAZIBO_EXTRA_ID")
    env.pop("NETNL_BMC_WEBHOOK_SECRET")
    with pytest.raises(SettingsError) as exc:
        load(env)
    assert "NETNL_BAZIBO_FROM_DONATIONS" in str(exc.value)


# --- the routing --------------------------------------------------------------


def test_one_euro_gives_a_bazibo_code_only(don_client, don_conn, recording_sender):
    resp = post_webhook(don_client, donation(amount=1.0))
    assert resp.status_code == 200 and resp.json() == {"status": "ok"}
    counts = _counts(don_conn)
    assert counts["bazibo_issuance"] == 1
    assert counts["credentials"] == 0 and counts["supporter_issuance"] == 0
    assert len(recording_sender.sent) == 1
    sent = recording_sender.sent[0]
    assert sent.to == DONOR
    verified = bazibo.verify(TEST_PUB, _code_from_mail(sent))
    assert verified is not None
    row = store.find_bazibo_issuance(don_conn, "pi_don_1")
    assert row["state"] == "delivered" and row["code_id"] == verified.code_id_hex


def test_two_euro_gives_both_mails(don_client, don_conn, recording_sender):
    resp = post_webhook(don_client, donation(amount=2.0))
    assert resp.json() == {"status": "ok"}
    counts = _counts(don_conn)
    assert counts["bazibo_issuance"] == 1 and counts["supporter_issuance"] == 1
    assert counts["credentials"] == 1
    assert len(_bazibo_mails(recording_sender)) == 1
    assert len(_netnl_mails(recording_sender)) == 1
    assert {m.to for m in recording_sender.sent} == {DONOR}


def test_replay_sends_nothing_new(don_client, don_conn, recording_sender):
    post_webhook(don_client, donation(amount=5.0))
    before = _counts(don_conn)
    for _ in range(2):
        resp = post_webhook(don_client, donation(amount=5.0))
        assert resp.status_code == 200 and resp.json() == {"status": "ok"}
    assert _counts(don_conn) == before
    assert len(recording_sender.sent) == 2  # one Bazibo, one netnl, once each


def test_bazibo_mail_failure_retries_only_the_bazibo_mail(
    don_client, don_conn, recording_sender, clock
):
    recording_sender.fail_on_calls = {1}  # the netnl mail goes first, the Bazibo mail second
    resp = post_webhook(don_client, donation(amount=2.0))
    assert resp.status_code == 503
    assert store.find_bazibo_issuance(don_conn, "pi_don_1")["state"] == "failed"
    # The netnl credential was still issued and mailed in the same delivery.
    assert len(_netnl_mails(recording_sender)) == 1
    first_code = _code_from_mail(recording_sender.sent[1])

    clock.advance(600)
    resp = post_webhook(don_client, donation(amount=2.0))  # BMC's retry
    assert resp.status_code == 200
    assert store.find_bazibo_issuance(don_conn, "pi_don_1")["state"] == "delivered"
    assert len(_netnl_mails(recording_sender)) == 1  # not re-sent
    bz = _bazibo_mails(recording_sender)
    assert len(bz) == 2 and _code_from_mail(bz[1]) == first_code
    assert _counts(don_conn)["credentials"] == 1


def test_netnl_mail_failure_retries_only_the_netnl_mail(
    don_client, don_conn, recording_sender, clock
):
    recording_sender.fail_on_calls = {0}  # the netnl mail goes first
    resp = post_webhook(don_client, donation(amount=2.0))
    assert resp.status_code == 503
    # The Bazibo code still went out in the same delivery.
    assert store.find_bazibo_issuance(don_conn, "pi_don_1")["state"] == "delivered"

    clock.advance(600)
    resp = post_webhook(don_client, donation(amount=2.0))
    assert resp.status_code == 200
    assert len(_bazibo_mails(recording_sender)) == 1  # not re-sent
    assert len(_netnl_mails(recording_sender)) == 2  # the failed one, then the retry
    assert store.find_issuance(don_conn, "pi_don_1")["state"] == "delivered"


def test_one_euro_mail_failure_then_retry(don_client, don_conn, recording_sender, clock):
    recording_sender.fail_on_calls = {0}
    assert post_webhook(don_client, donation(amount=1.0)).status_code == 503
    clock.advance(600)
    assert post_webhook(don_client, donation(amount=1.0)).json() == {"status": "ok"}
    assert _counts(don_conn)["bazibo_issuance"] == 1
    assert _code_from_mail(recording_sender.sent[0]) == _code_from_mail(recording_sender.sent[1])


def test_feature_off_keeps_todays_behaviour(bazibo_env, fake_opener, clock, recording_sender):
    app = create_app(load(bazibo_env), opener=fake_opener, now=clock, sender=recording_sender)
    client = TestClient(app, raise_server_exceptions=False)
    conn = store.connect(app.state.settings.db)
    try:
        assert post_webhook(client, donation(txn="a", amount=1.0)).json() == {"status": "ignored"}
        assert post_webhook(client, donation(txn="b", amount=2.0)).json() == {"status": "ok"}
        counts = _counts(conn)
        assert counts["bazibo_issuance"] == 0 and counts["credentials"] == 1
        assert _bazibo_mails(recording_sender) == []
    finally:
        conn.close()


def test_below_the_bazibo_minimum_nothing(don_env, fake_opener, clock, recording_sender):
    env = dict(don_env, NETNL_BAZIBO_DONATION_MIN="1")
    app = create_app(load(env), opener=fake_opener, now=clock, sender=recording_sender)
    client = TestClient(app, raise_server_exceptions=False)
    assert post_webhook(client, donation(amount=0.5)).json() == {"status": "ignored"}
    assert recording_sender.sent == []


def test_custom_minimum_above_the_netnl_one(don_env, fake_opener, clock, recording_sender):
    env = dict(don_env, NETNL_BAZIBO_DONATION_MIN="3")
    app = create_app(load(env), opener=fake_opener, now=clock, sender=recording_sender)
    client = TestClient(app, raise_server_exceptions=False)
    assert post_webhook(client, donation(amount=2.0)).json() == {"status": "ok"}
    assert len(_netnl_mails(recording_sender)) == 1 and _bazibo_mails(recording_sender) == []


def test_test_mode_donation_mints_nothing(don_client, don_conn, recording_sender):
    assert post_webhook(don_client, donation(amount=5.0, live=False)).json() == {"status": "ignored"}
    assert recording_sender.sent == [] and _counts(don_conn)["bazibo_issuance"] == 0


def test_unsigned_donation_is_401_and_writes_nothing(don_client, don_conn, recording_sender):
    before = _counts(don_conn)
    resp = post_webhook(don_client, donation(), secret="wrong-secret-" + "x" * 40)
    assert resp.status_code == 401
    assert _counts(don_conn) == before and recording_sender.sent == []


def test_donation_without_email_is_undeliverable_for_both(don_client, don_conn, recording_sender):
    resp = post_webhook(don_client, donation(amount=2.0, email_addr=None))
    assert resp.json() == {"status": "ignored"}
    assert recording_sender.sent == []
    assert store.find_bazibo_issuance(don_conn, "pi_don_1")["state"] == "undeliverable"
    assert store.find_issuance(don_conn, "pi_don_1")["state"] == "undeliverable"


def test_an_extra_purchase_never_gets_two_codes(don_client, don_conn, recording_sender):
    # The same BMC transaction reported as an Extra purchase and as a plain
    # donation (should BMC ever send both) is one bazibo_issuance row.
    txn = "pi_both"
    assert post_webhook(don_client, extra_payload(txn=txn)).json() == {"status": "ok"}
    assert post_webhook(don_client, donation(txn=txn, amount=1.0)).json() == {"status": "ok"}
    assert _counts(don_conn)["bazibo_issuance"] == 1
    assert len(_bazibo_mails(recording_sender)) == 1


def test_extra_marked_donation_still_goes_the_extra_way(don_client, don_conn, recording_sender):
    # A "support created" for some other shop item: neither a code nor a key.
    payload = extra_payload(txn="pi_sticker", extra_id=7, title="Sticker", event="donation.created",
                            amount=5.0)
    assert post_webhook(don_client, payload).json() == {"status": "ignored"}
    assert recording_sender.sent == []
    counts = _counts(don_conn)
    assert counts["bazibo_issuance"] == 0 and counts["credentials"] == 0


def test_the_donation_mail_thanks_for_the_donation(don_client, recording_sender):
    post_webhook(don_client, donation(amount=1.0))
    m = recording_sender.sent[0]
    line = "Bedankt voor je donatie! Hier is je code voor de volledige versie van Bazibo in de browser."
    assert line in m.body and line in m.html
    assert "Thank you for your donation!" in m.body and "Thank you for your donation!" in m.html
    assert "voor het kopen" not in m.body and "voor het kopen" not in m.html
    assert "Thank you for buying" not in m.body and "You bought" not in m.html


def test_the_extra_mail_keeps_its_purchase_wording():
    m = mail.build_bazibo_mail(to=BUYER, code="BZB1.x", link="https://l/#unlock=x",
                               link_pages="https://p/#unlock=x", device_limit=10)
    assert "Dank je wel voor het kopen" in m.body and "donatie" not in m.body
    assert "Dank je wel voor het kopen" in m.html and "donatie" not in m.html
    assert "Thank you for buying" in m.body and "Thank you for buying" in m.html


def test_logs_carry_no_email_or_code(don_client, recording_sender, caplog):
    caplog.set_level(logging.DEBUG)
    post_webhook(don_client, donation(amount=2.0))
    code = _code_from_mail(_bazibo_mails(recording_sender)[0])
    assert DONOR not in caplog.text and code not in caplog.text
    assert "event=bazibo-issued source=donation" in caplog.text


def test_activation_is_on_with_donations_only(don_env, fake_opener, clock, recording_sender):
    env = dict(don_env)
    env.pop("NETNL_BAZIBO_EXTRA_ID")
    client = TestClient(create_app(load(env), opener=fake_opener, now=clock, sender=recording_sender),
                        raise_server_exceptions=False)
    resp = client.post("/bazibo/activate", json={"code": "x", "device": "0" * 32},
                       headers={"Origin": "https://bazibo.nl"})
    assert resp.status_code == 400 and resp.json() == {"error": "invalid"}


# Silence "imported but unused" for the reused constants.
_ = (DEVICE_SALT, EXTRA_ID, TEST_SEED_B64)


def test_an_unexpected_bazibo_error_never_blocks_the_netnl_key(
    don_client, don_conn, recording_sender, monkeypatch, caplog, clock
):
    real_mint = bazibo.mint
    monkeypatch.setattr(bazibo, "mint", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    caplog.set_level(logging.INFO)
    resp = post_webhook(don_client, donation(amount=2.0))
    assert resp.status_code == 503
    assert len(_netnl_mails(recording_sender)) == 1 and _bazibo_mails(recording_sender) == []
    assert store.find_issuance(don_conn, "pi_don_1")["state"] == "delivered"
    assert "event=bazibo-error source=donation" in caplog.text and "boom" not in caplog.text

    monkeypatch.setattr(bazibo, "mint", real_mint)
    clock.advance(3600)  # past the pending lease the crashed attempt left behind
    resp = post_webhook(don_client, donation(amount=2.0))
    assert resp.status_code == 200
    assert len(_netnl_mails(recording_sender)) == 1 and len(_bazibo_mails(recording_sender)) == 1


def test_bazibo_hourly_cap_with_the_netnl_key_delivered(don_env, fake_opener, clock, recording_sender):
    env = dict(don_env, NETNL_SUPPORTER_MAX_PER_HOUR="1")
    app = create_app(load(env), opener=fake_opener, now=clock, sender=recording_sender)
    client = TestClient(app, raise_server_exceptions=False)
    assert post_webhook(client, donation(txn="t1", amount=1.0)).status_code == 200
    resp = post_webhook(client, donation(txn="t2", amount=2.0))
    assert resp.status_code == 503  # the code waits for the next hour
    assert len(_netnl_mails(recording_sender)) == 1 and len(_bazibo_mails(recording_sender)) == 1
    clock.advance(3700)
    assert post_webhook(client, donation(txn="t2", amount=2.0)).status_code == 200
    assert len(_netnl_mails(recording_sender)) == 1 and len(_bazibo_mails(recording_sender)) == 2


def test_both_mails_failing_then_both_retried(don_client, don_conn, recording_sender, clock):
    recording_sender.fail_on_calls = {0, 1}
    assert post_webhook(don_client, donation(amount=2.0)).status_code == 503
    clock.advance(600)
    assert post_webhook(don_client, donation(amount=2.0)).status_code == 200
    assert store.find_issuance(don_conn, "pi_don_1")["state"] == "delivered"
    assert store.find_bazibo_issuance(don_conn, "pi_don_1")["state"] == "delivered"


def test_a_parked_bazibo_row_with_the_netnl_key_is_ok(don_client, don_conn, recording_sender, clock):
    store.insert_bazibo_issuance(
        don_conn, txn_id="pi_don_1", code_id="00" * 10, state=store.SUPPORTER_FAILED,
        attempts=99, created_at="2026-01-01T00:00:00+00:00", updated_at="2026-01-01T00:00:00+00:00",
    )
    don_conn.commit()
    resp = post_webhook(don_client, donation(amount=2.0))
    assert resp.status_code == 200 and resp.json() == {"status": "ok"}
    assert len(_netnl_mails(recording_sender)) == 1 and _bazibo_mails(recording_sender) == []


def test_poll_and_donations_together_refused(don_env):
    env = dict(don_env, NETNL_BAZIBO_SOURCE="poll", NETNL_BMC_API_TOKEN="tok",
               NETNL_BMC_POLL_NOT_BEFORE="2026-12-01T00:00:00")
    with pytest.raises(SettingsError) as exc:
        load(env)
    assert "NETNL_BAZIBO_FROM_DONATIONS" in str(exc.value)


def test_notify_mail_names_the_donation(don_env, fake_opener, clock, recording_sender):
    env = dict(don_env, NETNL_SUPPORTER_NOTIFY="ops@example.org")
    client = TestClient(create_app(load(env), opener=fake_opener, now=clock, sender=recording_sender),
                        raise_server_exceptions=False)
    post_webhook(client, donation(amount=1.0))
    notes = [m for m in recording_sender.sent if m.to == "ops@example.org"]
    assert len(notes) == 1 and "(donation)" in notes[0].body and DONOR not in notes[0].body
