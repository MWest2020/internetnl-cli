"""Bazibo unlock codes (openspec/changes/add-bazibo-unlock): settings and
half-config refusal, webhook routing, issuance (signature, idempotency,
mail failure and retry), the mail itself, `POST /bazibo/activate`, and the
privacy invariants (nothing sensitive in logs or the database).
"""

from __future__ import annotations

import base64
import email
import json
import logging
import os
import pathlib
import struct
from datetime import timedelta

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from starlette.testclient import TestClient

from conftest import post_webhook
from netnl import bazibo, bazibo_activation, mail, server, store
from netnl.api import create_app
from netnl.errors import SettingsError
from netnl.settings import load

# A throwaway test seed. Never the production key, which lives only in the
# cluster secret (and on the operator's machine, 0600).
TEST_SEED = bytes(range(32))
TEST_SEED_B64 = base64.b64encode(TEST_SEED).decode()
TEST_PUB = bazibo.public_key_from_seed(TEST_SEED)
EXTRA_ID = "424242"
DEVICE_SALT = "test-device-salt-at-least-32-characters!"
BUYER = "buyer@example.org"
FIXTURES = pathlib.Path(__file__).resolve().parent.parent / "fixtures"


@pytest.fixture(autouse=True)
def _reset_rate_limit():
    bazibo_activation.reset_state()
    yield
    bazibo_activation.reset_state()


@pytest.fixture
def bazibo_env(supporter_env) -> dict:
    env = dict(supporter_env)
    env["NETNL_BAZIBO_SIGNING_KEY"] = TEST_SEED_B64
    env["NETNL_BAZIBO_EXTRA_ID"] = EXTRA_ID
    env["NETNL_BAZIBO_DEVICE_SALT"] = DEVICE_SALT
    return env


def _app(env, fake_opener, clock, sender):
    return create_app(load(env), opener=fake_opener, now=clock, sender=sender)


@pytest.fixture
def bz_app(bazibo_env, fake_opener, clock, recording_sender):
    return _app(bazibo_env, fake_opener, clock, recording_sender)


@pytest.fixture
def bz_client(bz_app):
    return TestClient(bz_app, raise_server_exceptions=False)


@pytest.fixture
def bz_conn(bz_app):
    c = store.connect(bz_app.state.settings.db)
    yield c
    c.close()


def extra_payload(*, txn="pi_extra_1", extra_id=int(EXTRA_ID), title="Bazibo volledige versie (web)",
                  amount=1.0, email_addr=BUYER, live=True, note=None, event="extra_purchase.created"):
    """Shape per BMC's published webhook OpenAPI (ExtraEvent / ExtraData)."""
    data = {
        "id": 98765,
        "object": "payment",
        "transaction_id": txn,
        "status": "succeeded",
        "refunded": "false",
        "amount": amount,
        "currency": "EUR",
        "support_type": "Extra",
        "supporter_name": "Alex",
        "supporter_id": 42,
        "support_note": note,
        "message": "Alex bought an extra",
        "created_at": 1719825600,
        "extras": [
            {"id": extra_id, "object": "extra", "title": title, "description": "",
             "amount": amount, "quantity": 1, "currency": "EUR",
             "extra_question": None, "question_answers": []}
        ],
    }
    if email_addr is not None:
        data["supporter_email"] = email_addr
    return {"event_id": 1234, "type": event, "live_mode": live, "created": 1719825600,
            "attempt": 1, "data": data}


def donation_payload(*, txn="pi_donation_1", amount=5.0, note=None):
    return {"event_id": 1, "type": "donation.created", "live_mode": True, "created": 1,
            "attempt": 1, "data": {"transaction_id": txn, "amount": amount, "currency": "EUR",
                                   "supporter_email": "donor@example.org",
                                   "support_note": note, "message": note or ""}}


def _counts(conn):
    q = lambda t: conn.execute(f"SELECT COUNT(*) AS n FROM {t}").fetchone()["n"]  # noqa: E731
    return {t: q(t) for t in ("credentials", "supporter_issuance", "bazibo_issuance", "bazibo_device")}


def _code_from_mail(m: mail.Mail) -> str:
    for line in m.body.splitlines():
        line = line.strip()
        if line.startswith(bazibo.PREFIX):
            return line
    raise AssertionError("no code in mail")


def _db_dump(path) -> str:
    c = store.connect(path)
    try:
        return "\n".join(c.iterdump())
    finally:
        c.close()


# --- settings and half-config ------------------------------------------------


def test_off_when_no_key(supporter_env, fake_opener, clock, recording_sender):
    settings = load(supporter_env)
    assert settings.bazibo is None
    client = TestClient(_app(supporter_env, fake_opener, clock, recording_sender),
                        raise_server_exceptions=False)
    assert client.post("/bazibo/activate", content=b"{}").status_code == 501


def test_key_without_extra_is_dormant(supporter_env, fake_opener, clock, recording_sender):
    env = dict(supporter_env, NETNL_BAZIBO_SIGNING_KEY=TEST_SEED_B64)
    settings = load(env)
    assert settings.bazibo is not None and settings.bazibo.enabled is False
    app = _app(env, fake_opener, clock, recording_sender)
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.post("/bazibo/activate", json={"code": "x", "device": "0" * 32},
                       headers={"Origin": "https://bazibo.nl"})
    assert resp.status_code == 503
    assert resp.json() == {"error": "off"}
    assert resp.headers["Access-Control-Allow-Origin"] == "https://bazibo.nl"
    # An Extra purchase is just an ignored event while dormant.
    assert post_webhook(client, extra_payload()).json() == {"status": "ignored"}
    assert recording_sender.sent == []


@pytest.mark.parametrize(
    "drop, add, needle",
    [
        ("NETNL_BAZIBO_SIGNING_KEY", {}, "NETNL_BAZIBO_SIGNING_KEY"),
        ("NETNL_BAZIBO_DEVICE_SALT", {}, "NETNL_BAZIBO_DEVICE_SALT"),
        ("NETNL_BMC_WEBHOOK_SECRET", {}, "NETNL_BMC_WEBHOOK_SECRET"),
        (None, {"NETNL_BAZIBO_SIGNING_KEY": "bm90LTMyLWJ5dGVz"}, "32-byte"),
        (None, {"NETNL_BAZIBO_DEVICE_SALT": "short"}, "NETNL_BAZIBO_DEVICE_SALT"),
        (None, {"NETNL_BAZIBO_EXTRA_ID": "abc"}, "NETNL_BAZIBO_EXTRA_ID"),
        (None, {"NETNL_BAZIBO_PUBLIC_KEY": bazibo.b64url_encode(b"\x01" * 32)}, "does not match"),
        (None, {"NETNL_BAZIBO_AUTO_REVOKE_AT": "10"}, "NETNL_BAZIBO_AUTO_REVOKE_AT"),
        (None, {"NETNL_BAZIBO_ALLOWED_ORIGINS": "http://bazibo.nl"}, "NETNL_BAZIBO_ALLOWED_ORIGINS"),
    ],
)
def test_half_config_refused(bazibo_env, drop, add, needle):
    env = dict(bazibo_env)
    if drop:
        env.pop(drop)
    env.update(add)
    with pytest.raises(SettingsError) as exc:
        load(env)
    assert needle in str(exc.value)
    assert TEST_SEED_B64 not in str(exc.value)


def test_half_config_refused_at_startup(bazibo_env, capsys):
    env = dict(bazibo_env)
    env.pop("NETNL_BAZIBO_SIGNING_KEY")
    assert server.main(env=env, run=lambda app: None) == 1
    assert "NETNL_BAZIBO_SIGNING_KEY" in capsys.readouterr().err


def test_public_key_cross_check_accepts_the_right_key(bazibo_env):
    env = dict(bazibo_env, NETNL_BAZIBO_PUBLIC_KEY=bazibo.b64url_encode(TEST_PUB))
    assert load(env).bazibo.public_key == TEST_PUB


def test_settings_repr_never_carries_the_seed_or_salt(bazibo_env):
    text = repr(load(bazibo_env))
    assert repr(TEST_SEED) not in text
    assert DEVICE_SALT not in text


# --- the code format -----------------------------------------------------------


def test_minted_code_matches_the_format_and_verifies_with_the_domain_prefix():
    code_id = bytes(range(10))
    code = bazibo.mint(TEST_SEED, day=20784, code_id=code_id)
    assert code.startswith("BZB1.") and "=" not in code and len(code) == bazibo.CODE_LEN
    _, p64, s64 = code.split(".")
    dec = lambda v: base64.urlsafe_b64decode(v + "=" * (-len(v) % 4))  # noqa: E731
    payload, sig = dec(p64), dec(s64)
    assert payload[0] == 1 and payload[1] == 1
    assert struct.unpack(">I", payload[2:6])[0] == 20784
    assert payload[6:] == code_id
    # Independently of `bazibo.verify`: Ed25519 over the domain prefix || payload.
    Ed25519PublicKey.from_public_bytes(TEST_PUB).verify(sig, b"bazibo-unlock-v1" + payload)
    assert bazibo.verify(TEST_PUB, code).code_id == code_id


def test_verify_rejects_a_signature_without_the_domain_prefix():
    payload = struct.pack(">BBI", 1, 1, 20784) + bytes(10)
    sig = Ed25519PrivateKey.from_private_bytes(TEST_SEED).sign(payload)
    code = "BZB1." + bazibo.b64url_encode(payload) + "." + bazibo.b64url_encode(sig)
    assert bazibo.verify(TEST_PUB, code) is None


@pytest.mark.parametrize("mutate", [
    lambda c: c[:-1] + ("A" if c[-1] != "A" else "B"),
    lambda c: "BZB2" + c[4:],
    lambda c: c + "A",
    lambda c: c.replace(".", "=", 1),
    lambda c: None,
    lambda c: 12,
])
def test_verify_rejects_tampered_codes(mutate):
    code = bazibo.mint(TEST_SEED, day=1, code_id=bytes(10))
    assert bazibo.verify(TEST_PUB, mutate(code)) is None


def test_verify_rejects_another_product_or_version():
    for head in (struct.pack(">BBI", 2, 1, 1), struct.pack(">BBI", 1, 2, 1)):
        payload = head + bytes(10)
        sig = Ed25519PrivateKey.from_private_bytes(TEST_SEED).sign(b"bazibo-unlock-v1" + payload)
        code = "BZB1." + bazibo.b64url_encode(payload) + "." + bazibo.b64url_encode(sig)
        assert bazibo.verify(TEST_PUB, code) is None


def test_production_public_key_fixture_is_a_raw_32_byte_key():
    raw = (FIXTURES / "bazibo-ed25519.pub").read_text().strip()
    assert len(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))) == 32


@pytest.mark.skipif(
    not os.environ.get("BAZIBO_TEST_SIGNING_KEY_FILE"),
    reason="set BAZIBO_TEST_SIGNING_KEY_FILE to the production seed file to run",
)
def test_code_minted_with_the_production_key_verifies_against_the_published_public_key():
    from netnl.settings import _decode_seed

    seed = _decode_seed(pathlib.Path(os.environ["BAZIBO_TEST_SIGNING_KEY_FILE"]).read_text())
    raw = (FIXTURES / "bazibo-ed25519.pub").read_text().strip()
    pub = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
    code = bazibo.mint(seed, day=20784, code_id=bazibo.new_code_id())
    assert bazibo.verify(pub, code) is not None


# --- routing ------------------------------------------------------------------


def test_bazibo_extra_mints_a_code_and_never_a_credential(bz_client, bz_conn, recording_sender):
    before = _counts(bz_conn)
    resp = post_webhook(bz_client, extra_payload())
    assert resp.status_code == 200 and resp.json() == {"status": "ok"}
    after = _counts(bz_conn)
    assert after["credentials"] == before["credentials"]
    assert after["supporter_issuance"] == before["supporter_issuance"]
    assert after["bazibo_issuance"] == before["bazibo_issuance"] + 1
    assert len(recording_sender.sent) == 1
    sent = recording_sender.sent[0]
    assert sent.to == BUYER
    code = _code_from_mail(sent)
    verified = bazibo.verify(TEST_PUB, code)
    assert verified is not None
    row = store.find_bazibo_issuance(bz_conn, "pi_extra_1")
    assert row["state"] == "delivered" and row["code_id"] == verified.code_id_hex


def test_donation_still_mints_a_credential_and_no_code(bz_client, bz_conn, recording_sender):
    resp = post_webhook(bz_client, donation_payload())
    assert resp.json() == {"status": "ok"}
    counts = _counts(bz_conn)
    assert counts["credentials"] == 1 and counts["bazibo_issuance"] == 0
    assert "BZB1." not in recording_sender.sent[0].body


def test_donation_that_mentions_bazibo_still_follows_the_donation_rules(
    bz_client, bz_conn, recording_sender
):
    # Above the netnl minimum: a netnl key, no Bazibo code.
    post_webhook(bz_client, donation_payload(txn="pi_d2", note="Voor Bazibo! volledige versie"))
    assert _counts(bz_conn)["bazibo_issuance"] == 0
    assert all("BZB1." not in m.body for m in recording_sender.sent)
    # Below the netnl minimum (default 2): nothing at all, Bazibo mention or not.
    resp = post_webhook(bz_client, donation_payload(txn="pi_d3", amount=1.0, note="bazibo"))
    assert resp.json() == {"status": "ignored"}
    assert _counts(bz_conn)["bazibo_issuance"] == 0


def test_other_extra_is_ignored(bz_client, bz_conn, recording_sender):
    before = _counts(bz_conn)
    resp = post_webhook(bz_client, extra_payload(extra_id=7, title="Sticker"))
    assert resp.json() == {"status": "ignored"}
    assert _counts(bz_conn) == before and recording_sender.sent == []


def test_title_match_when_no_id_is_configured(bazibo_env, fake_opener, clock, recording_sender):
    env = dict(bazibo_env)
    env.pop("NETNL_BAZIBO_EXTRA_ID")
    env["NETNL_BAZIBO_EXTRA_TITLE"] = "bazibo  Volledige versie (web)"
    client = TestClient(_app(env, fake_opener, clock, recording_sender), raise_server_exceptions=False)
    assert post_webhook(client, extra_payload(extra_id=1)).json() == {"status": "ok"}
    assert len(recording_sender.sent) == 1


@pytest.mark.parametrize("payload", [
    {"type": "membership.started", "live_mode": True, "data": {"supporter_email": BUYER}},
    {"type": "extra_purchase.refunded", "live_mode": True, "data": {"supporter_email": BUYER}},
    {"type": "something.new", "data": {"supporter_email": BUYER}},
    {"type": "commission_order.created"},
])
def test_unknown_or_unhandled_event_is_ignored_with_2xx(bz_client, bz_conn, recording_sender,
                                                        payload, caplog):
    caplog.set_level(logging.DEBUG, logger="netnl")
    before = _counts(bz_conn)
    resp = post_webhook(bz_client, payload)
    assert resp.status_code == 200 and resp.json() == {"status": "ignored"}
    assert _counts(bz_conn) == before and recording_sender.sent == []
    assert BUYER not in caplog.text
    assert "something.new" not in caplog.text  # unknown type strings are not echoed


def test_extra_below_bazibo_minimum_is_ignored(bazibo_env, fake_opener, clock, recording_sender):
    env = dict(bazibo_env, NETNL_BAZIBO_MIN_AMOUNT="1")
    client = TestClient(_app(env, fake_opener, clock, recording_sender), raise_server_exceptions=False)
    assert post_webhook(client, extra_payload(amount=0.5)).json() == {"status": "ignored"}
    assert post_webhook(client, extra_payload(txn="pi_ok", amount=1)).json() == {"status": "ok"}


def test_netnl_minimum_does_not_apply_to_the_extra(bz_client, recording_sender):
    # NETNL_SUPPORTER_MIN_AMOUNT defaults to 2; a 1-euro Extra still issues.
    assert post_webhook(bz_client, extra_payload(amount=1)).json() == {"status": "ok"}


def test_test_mode_extra_is_ignored(bz_client, recording_sender):
    assert post_webhook(bz_client, extra_payload(live=False)).json() == {"status": "ignored"}
    assert recording_sender.sent == []


def test_extra_without_email_is_undeliverable(bz_client, bz_conn, recording_sender):
    assert post_webhook(bz_client, extra_payload(email_addr=None)).json() == {"status": "ignored"}
    assert recording_sender.sent == []
    assert store.find_bazibo_issuance(bz_conn, "pi_extra_1")["state"] == "undeliverable"


def test_unsigned_extra_is_401_and_writes_nothing(bz_client, bz_conn):
    before = _counts(bz_conn)
    resp = bz_client.post("/webhooks/bmc", content=json.dumps(extra_payload()).encode())
    assert resp.status_code == 401
    assert _counts(bz_conn) == before


# --- idempotency, failure and retry -----------------------------------------------


def test_replay_sends_nothing_new(bz_client, bz_conn, recording_sender):
    post_webhook(bz_client, extra_payload())
    resp = post_webhook(bz_client, extra_payload())
    assert resp.json() == {"status": "ok"}
    assert len(recording_sender.sent) == 1
    assert _counts(bz_conn)["bazibo_issuance"] == 1


def test_mail_failure_marks_failed_and_a_retry_resends_the_same_code(
    bz_client, bz_conn, recording_sender, clock
):
    recording_sender.fail_on_calls = {0}
    resp = post_webhook(bz_client, extra_payload())
    assert resp.status_code == 503
    row = store.find_bazibo_issuance(bz_conn, "pi_extra_1")
    assert row["state"] == "failed" and row["attempts"] == 1
    first_code = _code_from_mail(recording_sender.sent[0])

    clock.advance(3600 * 30)  # BMC retries later; the code keeps its issued day
    resp = post_webhook(bz_client, extra_payload())
    assert resp.status_code == 200
    row = store.find_bazibo_issuance(bz_conn, "pi_extra_1")
    assert row["state"] == "delivered" and row["attempts"] == 2
    assert _code_from_mail(recording_sender.sent[1]) == first_code


def test_exhausted_attempts_park_with_2xx_and_no_mail(bz_client, bz_conn, recording_sender):
    recording_sender.fail_always = True
    for _ in range(3):
        assert post_webhook(bz_client, extra_payload()).status_code == 503
    sent = len(recording_sender.sent)
    resp = post_webhook(bz_client, extra_payload())
    assert resp.status_code == 200 and resp.json() == {"status": "ignored"}
    assert len(recording_sender.sent) == sent


def test_pending_row_inside_its_lease_refuses_a_concurrent_delivery(bz_app, bz_client, bz_conn, clock):
    now = store.utcnow_iso(clock)
    store.insert_bazibo_issuance(bz_conn, txn_id="pi_extra_1", code_id="00" * 10,
                                 state="pending", attempts=1, created_at=now, updated_at=now)
    assert post_webhook(bz_client, extra_payload()).status_code == 503
    clock.advance(bz_app.state.settings.supporter.pending_lease_seconds + 1)
    assert post_webhook(bz_client, extra_payload()).status_code == 200


def test_hourly_cap_applies(bazibo_env, fake_opener, clock, recording_sender):
    env = dict(bazibo_env, NETNL_SUPPORTER_MAX_PER_HOUR="1")
    client = TestClient(_app(env, fake_opener, clock, recording_sender), raise_server_exceptions=False)
    assert post_webhook(client, extra_payload(txn="a")).status_code == 200
    assert post_webhook(client, extra_payload(txn="b")).status_code == 503


def test_notify_mail_carries_code_id_only(bazibo_env, fake_opener, clock, recording_sender):
    env = dict(bazibo_env, NETNL_SUPPORTER_NOTIFY="op@example.org")
    client = TestClient(_app(env, fake_opener, clock, recording_sender), raise_server_exceptions=False)
    post_webhook(client, extra_payload())
    code = _code_from_mail(recording_sender.sent[0])
    notify = recording_sender.sent[1]
    assert notify.to == "op@example.org"
    assert code not in notify.body and BUYER not in notify.body
    assert bazibo.verify(TEST_PUB, code).code_id_hex in notify.body


# --- the mail -------------------------------------------------------------------


def test_mail_content_links_code_qr_and_no_remote_resources(bz_client, recording_sender):
    post_webhook(bz_client, extra_payload())
    m = recording_sender.sent[0]
    code = _code_from_mail(m)
    assert f"https://bazibo.nl/#unlock={code}" in m.body
    assert f"https://mwest2020.github.io/bazibo/#unlock={code}" in m.body
    assert "Dank je wel" in m.body and "Thank you" in m.body
    assert "tot 10 apparaten" in m.body and "up to 10 devices" in m.body
    assert m.html is not None and f'href="https://bazibo.nl/#unlock={code}"' in m.html
    assert len(m.inline_images) == 1
    image = m.inline_images[0]
    assert image.data.startswith(b"\x89PNG\r\n\x1a\n")
    assert image.data == mail.qr_png(f"https://bazibo.nl/#unlock={code}")
    # Every image is inline; nothing is fetched when the mail is opened.
    import re
    srcs = re.findall(r'<img[^>]+src="([^"]+)"', m.html)
    assert srcs == [f"cid:{image.cid}"]
    assert "<link" not in m.html and "url(" not in m.html and "<script" not in m.html


def test_smtp_message_is_related_multipart_with_the_inline_png(monkeypatch):
    captured = {}

    class FakeSMTP:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def starttls(self, **k):
            pass

        def sendmail(self, from_addr, to, message):
            captured["message"] = message

    monkeypatch.setattr(mail.smtplib, "SMTP", FakeSMTP)
    code = bazibo.mint(TEST_SEED, day=1, code_id=bytes(10))
    link, pages = bazibo.links(code)
    m = mail.build_bazibo_mail(to=BUYER, code=code, link=link, link_pages=pages, device_limit=10)
    mail.smtp_sender(host="h", port=587, username=None, password=None, from_addr="f@example.org",
                     mode="starttls", timeout=5)(m)
    msg = email.message_from_string(captured["message"])
    types = [part.get_content_type() for part in msg.walk()]
    assert types[0] == "multipart/alternative"
    assert "text/plain" in types and "multipart/related" in types
    assert "text/html" in types and "image/png" in types
    png = next(p for p in msg.walk() if p.get_content_type() == "image/png")
    assert png["Content-ID"] == "<bazibo-qr@bazibo.nl>"
    assert png.get_payload(decode=True).startswith(b"\x89PNG")


# --- privacy: logs and database -----------------------------------------------------


def test_nothing_sensitive_in_logs_or_db_across_the_issuance_flow(
    bz_app, bz_client, recording_sender, caplog
):
    caplog.set_level(logging.DEBUG)
    recording_sender.fail_on_calls = {0}
    post_webhook(bz_client, extra_payload())
    post_webhook(bz_client, extra_payload())
    post_webhook(bz_client, extra_payload())
    code = _code_from_mail(recording_sender.sent[-1])
    payload_part, sig_part = code.split(".")[1:]
    dump = _db_dump(bz_app.state.settings.db)
    for secret in (BUYER, code, payload_part, sig_part, "Alex"):
        assert secret not in caplog.text
        assert secret not in dump


# --- POST /bazibo/activate -----------------------------------------------------------


def _code(code_id=b"\x01" * 10):
    return bazibo.mint(TEST_SEED, day=20784, code_id=code_id)


def _device(n: int) -> str:
    return f"{n:032x}"


def _activate(client, code, device, *, origin="https://bazibo.nl", ip="198.51.100.7"):
    headers = {"CF-Connecting-IP": ip}
    if origin:
        headers["Origin"] = origin
    return client.post("/bazibo/activate", json={"code": code, "device": device}, headers=headers)


@pytest.fixture
def act_client(bazibo_env, fake_opener, clock, recording_sender):
    env = dict(bazibo_env, NETNL_BAZIBO_ACTIVATE_PER_MINUTE="1000")
    return TestClient(_app(env, fake_opener, clock, recording_sender), raise_server_exceptions=False)


def test_valid_code_activates(act_client):
    resp = _activate(act_client, _code(), _device(1))
    assert resp.status_code == 200 and resp.json() == {"status": "ok"}


def test_dashed_uuid_device_is_accepted(act_client):
    assert _activate(act_client, _code(), "0f8fad5b-d9cb-469f-a165-70867728950e").status_code == 200


def test_signature_is_checked_before_the_database(act_client, bz_conn, monkeypatch):
    forged = _code()[:-2] + "AA"
    opened = []
    real_connect = store.connect
    monkeypatch.setattr(store, "connect", lambda *a, **k: opened.append(1) or real_connect(*a, **k))
    resp = _activate(act_client, forged, _device(1))
    assert resp.status_code == 400 and resp.json() == {"error": "invalid"}
    assert opened == []
    other_key = Ed25519PrivateKey.generate().private_bytes_raw()
    resp = _activate(act_client, bazibo.mint(other_key, day=1, code_id=bytes(10)), _device(1))
    assert resp.status_code == 400 and opened == []


def test_revoked_code_is_refused(act_client, bz_conn, clock):
    code = _code()
    assert _activate(act_client, code, _device(1)).status_code == 200
    store.revoke_bazibo_code(bz_conn, (b"\x01" * 10).hex(), reason="manual", at=store.utcnow_iso(clock))
    resp = _activate(act_client, code, _device(1))
    assert resp.status_code == 403 and resp.json() == {"error": "revoked"}


def test_device_limit_and_idempotent_reactivation(act_client, caplog):
    caplog.set_level(logging.DEBUG)
    code = _code()
    for n in range(1, 11):
        assert _activate(act_client, code, _device(n)).status_code == 200
    resp = _activate(act_client, code, _device(11))
    assert resp.status_code == 409 and resp.json() == {"error": "limit"}
    # Known devices keep working; the refused one stays refused.
    for n in (1, 5, 10, 1):
        assert _activate(act_client, code, _device(n)).status_code == 200
    assert _activate(act_client, code, _device(11)).status_code == 409
    assert (b"\x01" * 10).hex() in caplog.text  # the code id is logged once at the limit
    # Another code is counted on its own.
    assert _activate(act_client, _code(b"\x02" * 10), _device(11)).status_code == 200


def test_auto_revoke_at_100_distinct_devices_with_the_defaults(bazibo_env, tmp_path, clock):
    settings = load(bazibo_env)
    bz = settings.bazibo
    assert (bz.device_limit, bz.auto_revoke_at) == (10, 100)
    conn = store.connect(settings.db)
    store.migrate(conn)
    code_id = "ab" * 10
    now = clock()
    outcomes = [bazibo_activation.record_activation(conn, bz, code_id, bytes.fromhex(_device(n)), now)
                for n in range(1, 101)]
    assert outcomes[:10] == ["ok"] * 10
    assert outcomes[10:99] == ["limit"] * 89
    assert outcomes[99] == "revoked"
    # Now even a known, previously accepted device is refused.
    assert bazibo_activation.record_activation(conn, bz, code_id, bytes.fromhex(_device(1)), now) == "revoked"
    assert store.bazibo_revocation(conn, code_id)["reason"] == "auto"
    conn.close()


def test_limits_are_configurable(bazibo_env, fake_opener, clock, recording_sender):
    env = dict(bazibo_env, NETNL_BAZIBO_DEVICE_LIMIT="2", NETNL_BAZIBO_AUTO_REVOKE_AT="4")
    client = TestClient(_app(env, fake_opener, clock, recording_sender), raise_server_exceptions=False)
    code = _code()
    assert [_activate(client, code, _device(n)).status_code for n in range(1, 5)] == [200, 200, 409, 403]
    assert _activate(client, code, _device(1)).status_code == 403


def test_last_seen_is_a_date_and_updates(act_client, bz_conn, clock):
    code = _code()
    _activate(act_client, code, _device(1))
    clock.advance(86400 * 3)
    _activate(act_client, code, _device(1))
    row = bz_conn.execute("SELECT first_seen, last_seen FROM bazibo_device").fetchone()
    assert row["first_seen"] == "2026-01-01" and row["last_seen"] == "2026-01-04"


@pytest.mark.parametrize("body", [
    b'{"code": "x", "device": "' + b"0" * 32 + b'", "code": "y"}',
    b'{"code": "x", "device": "' + b"0" * 32 + b'", "extra": 1}',
    b'{"code": "x"}',
    b'["x"]',
    b'{"code": NaN, "device": "' + b"0" * 32 + b'"}',
    b'{"code": 1, "device": "' + b"0" * 32 + b'"}',
    b'{"code": "x", "device": "not-hex"}',
    b'{"code": "x", "device": "' + b"0" * 31 + b'"}',
    b"\xff\xfe",
    b"",
    b'{"code": "' + b"A" * 2000 + b'", "device": "' + b"0" * 32 + b'"}',
])
def test_strict_body_parsing(act_client, body, bz_conn):
    resp = act_client.post("/bazibo/activate", content=body,
                           headers={"Content-Type": "application/json", "Origin": "https://bazibo.nl"})
    assert resp.status_code == 400 and resp.json() == {"error": "invalid"}
    assert _counts(bz_conn)["bazibo_device"] == 0


def test_cors_preflight_and_origins(act_client):
    for origin in ("https://bazibo.nl", "https://mwest2020.github.io"):
        pre = act_client.options("/bazibo/activate", headers={
            "Origin": origin, "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type"})
        assert pre.status_code == 204
        assert pre.headers["Access-Control-Allow-Origin"] == origin
        assert pre.headers["Access-Control-Allow-Methods"] == "POST, OPTIONS"
        assert "Access-Control-Allow-Credentials" not in pre.headers
        resp = _activate(act_client, _code(), _device(1), origin=origin)
        assert resp.status_code == 200
        assert resp.headers["Access-Control-Allow-Origin"] == origin
        assert resp.headers["Vary"] == "Origin"
        assert "Access-Control-Allow-Credentials" not in resp.headers

    evil = "https://evil.example"
    pre = act_client.options("/bazibo/activate", headers={"Origin": evil})
    assert pre.status_code == 204
    assert "Access-Control-Allow-Origin" not in pre.headers
    assert "Access-Control-Allow-Methods" not in pre.headers
    resp = _activate(act_client, _code(), _device(1), origin=evil)
    assert resp.status_code == 403 and resp.json() == {"error": "origin"}
    assert "Access-Control-Allow-Origin" not in resp.headers
    # Only POST and OPTIONS exist on this path.
    assert act_client.get("/bazibo/activate").status_code == 501
    assert act_client.put("/bazibo/activate").status_code == 501


def test_per_ip_rate_limit(bz_client):
    code = _code()
    statuses = [_activate(bz_client, code, _device(1)).status_code for _ in range(11)]
    assert statuses[:10] == [200] * 10
    assert statuses[10] == 429
    assert _activate(bz_client, code, _device(1), ip="203.0.113.9").status_code == 200


def test_rate_limit_window_slides(monkeypatch, bz_client):
    t = [1000.0]
    monkeypatch.setattr(bazibo_activation.time, "monotonic", lambda: t[0])
    code = _code()
    for _ in range(10):
        _activate(bz_client, code, _device(1))
    assert _activate(bz_client, code, _device(1)).status_code == 429
    t[0] += 61
    assert _activate(bz_client, code, _device(1)).status_code == 200


def test_nothing_sensitive_in_logs_or_db_for_activation(bazibo_env, fake_opener, clock,
                                                        recording_sender, caplog):
    env = dict(bazibo_env, NETNL_BAZIBO_DEVICE_LIMIT="1", NETNL_BAZIBO_AUTO_REVOKE_AT="3")
    app = _app(env, fake_opener, clock, recording_sender)
    client = TestClient(app, raise_server_exceptions=False)
    caplog.set_level(logging.DEBUG)
    code = _code()
    ip = "198.51.100.77"
    devices = [_device(0xDEADBEEF00 + n) for n in range(4)]
    for d in devices:
        _activate(client, code, d, ip=ip)
    _activate(client, code[:-2] + "AA", devices[0], ip=ip)
    dump = _db_dump(app.state.settings.db)
    for secret in [code, *code.split(".")[1:], ip, *devices]:
        assert secret not in caplog.text
        assert secret not in dump
    cols = [r["name"] for r in store.connect(app.state.settings.db).execute("PRAGMA table_info(bazibo_device)")]
    assert cols == ["id", "code_id", "device_hash", "first_seen", "last_seen"]


def test_access_log_filter_drops_activation_lines():
    f = server.PrivatePathFilter()
    rec = lambda path: logging.LogRecord("uvicorn.access", logging.INFO, "", 0,  # noqa: E731
                                         '%s - "%s %s HTTP/%s" %d',
                                         ("198.51.100.7:1", "POST", path, "1.1", 200), None)
    assert f.filter(rec("/bazibo/activate")) is False
    assert f.filter(rec("/health")) is True
    cfg = server.log_config()
    assert cfg["handlers"]["access"]["filters"] == ["netnl_private_paths"]


# --- operator commands ------------------------------------------------------------------


def test_admin_mint_revoke_show(bazibo_env, act_client, capsys):
    import io

    from netnl import admin

    out = io.StringIO()
    assert admin.main(["bazibo", "mint"], stdout=out, env=bazibo_env) == 0
    lines = out.getvalue().splitlines()
    code_id = lines[0].split()[1]
    code = lines[1]
    assert bazibo.verify(TEST_PUB, code).code_id_hex == code_id
    assert lines[2] == f"https://bazibo.nl/#unlock={code}"
    assert lines[3] == f"https://mwest2020.github.io/bazibo/#unlock={code}"
    assert _activate(act_client, code, _device(1)).status_code == 200

    assert admin.main(["bazibo", "revoke", code_id], env=bazibo_env) == 0
    assert _activate(act_client, code, _device(1)).status_code == 403
    out = io.StringIO()
    assert admin.main(["bazibo", "show", code_id], stdout=out, env=bazibo_env) == 0
    assert "devices 1" in out.getvalue() and "revoked (manual" in out.getvalue()
    assert admin.main(["bazibo", "unrevoke", code_id], env=bazibo_env) == 0
    assert _activate(act_client, code, _device(1)).status_code == 200
    assert admin.main(["bazibo", "revoke", "nothex"], env=bazibo_env) == 1


def test_bazibo_tables_hold_no_email_column(bz_conn):
    for table in ("bazibo_issuance", "bazibo_device", "bazibo_revoked"):
        cols = {r["name"] for r in bz_conn.execute(f"PRAGMA table_info({table})")}
        assert not cols & {"email", "ip", "user_agent", "code"}
    assert {r["name"] for r in bz_conn.execute("PRAGMA table_info(bazibo_issuance)")} == {
        "id", "txn_id", "code_id", "state", "attempts", "created_at", "updated_at"}


def test_issued_day_comes_from_the_first_attempt(bz_client, bz_conn, recording_sender, clock):
    clock.advance(timedelta(days=10).total_seconds())
    post_webhook(bz_client, extra_payload())
    code = _code_from_mail(recording_sender.sent[0])
    assert bazibo.verify(TEST_PUB, code).day == bazibo.day_number(clock())


# --- security review round 1 -----------------------------------------------------------


@pytest.mark.parametrize("payload", [
    {"type": "extra_purchase.created", "live_mode": True, "data": {"extras": [{"id": 7}]}},
    {"type": "extra_purchase.created", "data": {"something": "else"}},
    {"type": "extra_purchase.created", "data": {"extras": "nope"}},
])
def test_odd_payload_for_another_extra_is_200_not_400(bz_client, bz_conn, payload):
    # A 400 would make BMC retry and count toward disabling the webhook.
    before = _counts(bz_conn)
    resp = post_webhook(bz_client, payload)
    assert resp.status_code == 200 and resp.json() == {"status": "ignored"}
    assert _counts(bz_conn) == before


def test_malformed_bazibo_purchase_is_still_400(bz_client, bz_conn):
    payload = extra_payload()
    del payload["data"]["transaction_id"]
    assert post_webhook(bz_client, payload).status_code == 400
    assert _counts(bz_conn)["bazibo_issuance"] == 0


def test_spoofed_ip_flood_cannot_lock_out_a_real_client(bz_client, monkeypatch):
    monkeypatch.setattr(bazibo_activation, "_MAX_BUCKETS", 50)
    code = _code()
    # A real client that has used part of its budget.
    for _ in range(3):
        assert _activate(bz_client, code, _device(1), ip="198.51.100.1").status_code == 200
    # A flood of fresh, spoofed addresses fills and churns the table.
    for n in range(200):
        bazibo_activation._allow(f"spoof-{n}", 10, bazibo_activation.time.monotonic())
    assert len(bazibo_activation._rate) <= 50
    # The real client and a brand-new real client still get through.
    assert _activate(bz_client, code, _device(1), ip="198.51.100.1").status_code == 200
    assert _activate(bz_client, code, _device(1), ip="198.51.100.2").status_code == 200


def test_manual_unrevoke_of_an_auto_revoked_code_holds(bazibo_env, fake_opener, clock, recording_sender):
    import io

    from netnl import admin

    env = dict(bazibo_env, NETNL_BAZIBO_DEVICE_LIMIT="1", NETNL_BAZIBO_AUTO_REVOKE_AT="3",
               NETNL_BAZIBO_ACTIVATE_PER_MINUTE="1000")
    client = TestClient(_app(env, fake_opener, clock, recording_sender), raise_server_exceptions=False)
    code = _code()
    assert [_activate(client, code, _device(n)).status_code for n in (1, 2, 3)] == [200, 409, 403]
    assert admin.main(["bazibo", "unrevoke", (b"\x01" * 10).hex()], stdout=io.StringIO(), env=env) == 0
    assert _activate(client, code, _device(1)).status_code == 200
    assert _activate(client, code, _device(4)).status_code == 409  # not re-revoked
    assert _activate(client, code, _device(1)).status_code == 200


def test_mail_links_keep_the_code_out_of_the_query_string(bz_client, recording_sender):
    # The fragment is never sent to the web server, so the code stays out of
    # bazibo.nl's and GitHub Pages' access logs (design.md O6).
    post_webhook(bz_client, extra_payload())
    m = recording_sender.sent[0]
    code = _code_from_mail(m)
    from urllib.parse import urlsplit

    for link in bazibo.links(code):
        parts = urlsplit(link)
        assert parts.query == "" and parts.fragment == f"unlock={code}"
        assert link in m.body
    assert "?unlock=" not in m.body and "?unlock=" not in m.html
