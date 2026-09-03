"""`netnl.bmc` — pure signature verification and delivery parsing/
qualification. No fixtures beyond a payload dict and a secret; no app, no
database, no network.

The fixture payloads below are literals, derived from documented shape;
replace with an owner-supplied real delivery once one is available (see
`docs/how-to/supporter-webhook.md`'s troubleshooting note).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from decimal import Decimal

import pytest

from netnl import bmc

SECRET = "test-webhook-secret"


def sign(body: bytes, secret: str = SECRET) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _payload(**overrides) -> dict:
    base = {
        "type": "donation.created",
        "live_mode": True,
        "attempt": 1,
        "data": {
            "id": 12345,
            "amount": "5.00",
            "currency": "EUR",
            "transaction_id": "txn-abc123",
            "email": "supporter@example.org",
            "supporter_name": "Jane Doe",
            "support_note": "keep it up!",
        },
    }
    base["data"].update(overrides.pop("data", {}))
    base.update(overrides)
    return base


def _body(payload: dict) -> bytes:
    return json.dumps(payload).encode()


# --- verify_signature --------------------------------------------------


def test_verify_signature_accepts_matching_hex_digest():
    body = _body(_payload())
    assert bmc.verify_signature(SECRET, sign(body), body) is True


def test_verify_signature_accepts_matching_base64_digest():
    body = _body(_payload())
    digest = hmac.new(SECRET.encode(), body, hashlib.sha256).digest()
    header = base64.b64encode(digest).decode()
    assert bmc.verify_signature(SECRET, header, body) is True


def test_verify_signature_accepts_sha256_prefix_case_insensitively():
    body = _body(_payload())
    assert bmc.verify_signature(SECRET, f"sha256={sign(body)}", body) is True
    assert bmc.verify_signature(SECRET, f"SHA256={sign(body)}", body) is True


def test_verify_signature_rejects_wrong_secret():
    body = _body(_payload())
    assert bmc.verify_signature("wrong-secret", sign(body), body) is False


def test_verify_signature_rejects_missing_header():
    body = _body(_payload())
    assert bmc.verify_signature(SECRET, None, body) is False
    assert bmc.verify_signature(SECRET, "", body) is False


@pytest.mark.parametrize("garbage", ["not-hex-or-base64!!", "deadbeef", "", "   "])
def test_verify_signature_never_raises_on_malformed_header(garbage):
    body = _body(_payload())
    assert bmc.verify_signature(SECRET, garbage, body) is False


def test_verify_signature_rejects_signature_over_a_reserialised_body():
    """Proves verification is over the exact raw bytes received, not a
    reparsed/reserialised form of them — a re-serialised body (different
    whitespace/key order) must fail even though it round-trips to the same
    JSON structure.
    """
    payload = _payload()
    original_body = _body(payload)
    signature = sign(original_body)

    reserialised_body = json.dumps(payload, indent=2).encode()
    assert reserialised_body != original_body
    assert bmc.verify_signature(SECRET, signature, reserialised_body) is False
    # The original body with its own signature still verifies.
    assert bmc.verify_signature(SECRET, signature, original_body) is True


def test_verify_signature_rejects_tampered_body():
    payload = _payload()
    body = _body(payload)
    signature = sign(body)
    tampered = _body(_payload(data={"amount": "500.00"}))
    assert bmc.verify_signature(SECRET, signature, tampered) is False


# --- parse_delivery ------------------------------------------------------


def test_parse_delivery_reads_nested_fields():
    delivery = bmc.parse_delivery(_payload())
    assert delivery.event == "donation.created"
    assert delivery.live_mode is True
    assert delivery.transaction_id == "txn-abc123"
    assert delivery.amount == Decimal("5.00")
    assert delivery.currency == "EUR"
    assert delivery.email == "supporter@example.org"


def test_parse_delivery_reads_top_level_fields_too():
    payload = _payload()
    data = payload.pop("data")
    payload.update(data)
    delivery = bmc.parse_delivery(payload)
    assert delivery.transaction_id == "txn-abc123"
    assert delivery.amount == Decimal("5.00")


def test_parse_delivery_amount_uses_decimal_not_float():
    payload = _payload(data={"amount": "0.10"})
    delivery = bmc.parse_delivery(payload)
    assert delivery.amount == Decimal("0.10")
    assert str(delivery.amount) == "0.10"


@pytest.mark.parametrize(
    "field,mutate",
    [
        ("type", lambda p: p.pop("type")),
        ("live_mode", lambda p: p.pop("live_mode")),
        ("live_mode", lambda p: p.__setitem__("live_mode", "yes")),
        ("transaction_id", lambda p: p["data"].pop("transaction_id")),
        ("amount", lambda p: p["data"].pop("amount")),
        ("amount", lambda p: p["data"].__setitem__("amount", "not-a-number")),
        ("amount", lambda p: p["data"].__setitem__("amount", -5)),
        ("currency", lambda p: p["data"].pop("currency")),
    ],
)
def test_parse_delivery_raises_malformed_delivery_naming_only_the_field(field, mutate):
    payload = _payload()
    mutate(payload)
    with pytest.raises(bmc.MalformedDelivery) as exc_info:
        bmc.parse_delivery(payload)
    assert exc_info.value.field == field
    # Nothing about the payload itself leaks into the exception's own text.
    assert "supporter@example.org" not in str(exc_info.value)


def test_parse_delivery_missing_email_is_none_not_an_error():
    payload = _payload()
    del payload["data"]["email"]
    delivery = bmc.parse_delivery(payload)
    assert delivery.email is None


def test_parse_delivery_falls_back_to_supporter_email_field():
    payload = _payload()
    del payload["data"]["email"]
    payload["data"]["supporter_email"] = "other@example.org"
    delivery = bmc.parse_delivery(payload)
    assert delivery.email == "other@example.org"


def test_parse_delivery_oversized_field_is_malformed():
    payload = _payload(data={"transaction_id": "x" * 1000})
    with pytest.raises(bmc.MalformedDelivery, match="transaction_id"):
        bmc.parse_delivery(payload)


def test_parse_delivery_rejects_non_dict_payload():
    with pytest.raises(bmc.MalformedDelivery):
        bmc.parse_delivery([])  # type: ignore[arg-type]


# --- valid_recipient ------------------------------------------------------


@pytest.mark.parametrize(
    "email",
    [
        "supporter@example.org",
        "a.b+tag@sub.example.co.uk",
    ],
)
def test_valid_recipient_accepts_plain_addresses(email):
    assert bmc.valid_recipient(email) is True


@pytest.mark.parametrize(
    "email",
    [
        None,
        "",
        "not-an-email",
        "a@b@c.example",
        "a b@example.org",
        "a@example.org\r\nBcc: x@evil.example",
        "a@example.org, b@evil.example",
        "<a@example.org>",
        "a" * 260 + "@example.org",
        "a@localhost",  # no dot at all in the domain
    ],
)
def test_valid_recipient_rejects_unsafe_or_malformed(email):
    assert bmc.valid_recipient(email) is False


# --- qualifies -------------------------------------------------------------


def _cfg(**overrides) -> bmc.QualifyConfig:
    base = dict(accept_test_mode=False, min_amount=Decimal("0"), currency=None)
    base.update(overrides)
    return bmc.QualifyConfig(**base)


def test_qualifies_issues_for_a_plain_live_donation():
    delivery = bmc.parse_delivery(_payload())
    assert bmc.qualifies(delivery, _cfg()) == bmc.Decision.ISSUE


def test_qualifies_ignores_non_donation_event():
    delivery = bmc.parse_delivery(_payload(type="member.created"))
    assert bmc.qualifies(delivery, _cfg()) == bmc.Decision.IGNORE_EVENT


def test_qualifies_ignores_test_mode_by_default():
    delivery = bmc.parse_delivery(_payload(live_mode=False))
    assert bmc.qualifies(delivery, _cfg()) == bmc.Decision.IGNORE_TEST_MODE


def test_qualifies_accepts_test_mode_when_explicitly_configured():
    delivery = bmc.parse_delivery(_payload(live_mode=False))
    assert bmc.qualifies(delivery, _cfg(accept_test_mode=True)) == bmc.Decision.ISSUE


def test_qualifies_ignores_amount_below_minimum():
    delivery = bmc.parse_delivery(_payload(data={"amount": "1.00"}))
    assert bmc.qualifies(delivery, _cfg(min_amount=Decimal("5.00"))) == bmc.Decision.IGNORE_AMOUNT


def test_qualifies_default_minimum_zero_accepts_any_amount():
    delivery = bmc.parse_delivery(_payload(data={"amount": "0.00"}))
    assert bmc.qualifies(delivery, _cfg()) == bmc.Decision.ISSUE


def test_qualifies_ignores_currency_mismatch():
    delivery = bmc.parse_delivery(_payload(data={"currency": "USD"}))
    assert bmc.qualifies(delivery, _cfg(currency="EUR")) == bmc.Decision.IGNORE_CURRENCY


def test_qualifies_currency_match_is_case_insensitive():
    delivery = bmc.parse_delivery(_payload(data={"currency": "eur"}))
    assert bmc.qualifies(delivery, _cfg(currency="EUR")) == bmc.Decision.ISSUE


def test_qualifies_no_currency_configured_accepts_any():
    delivery = bmc.parse_delivery(_payload(data={"currency": "JPY"}))
    assert bmc.qualifies(delivery, _cfg(currency=None)) == bmc.Decision.ISSUE


def test_qualifies_undeliverable_when_no_email():
    payload = _payload()
    del payload["data"]["email"]
    delivery = bmc.parse_delivery(payload)
    assert bmc.qualifies(delivery, _cfg()) == bmc.Decision.UNDELIVERABLE_NO_EMAIL


def test_qualifies_undeliverable_when_email_is_header_injection_shaped():
    delivery = bmc.parse_delivery(
        _payload(data={"email": "a@example.org\r\nBcc: x@evil.example"})
    )
    assert bmc.qualifies(delivery, _cfg()) == bmc.Decision.UNDELIVERABLE_NO_EMAIL


def test_ignore_decisions_set_excludes_undeliverable_no_email():
    # UNDELIVERABLE_NO_EMAIL still writes a row (as `undeliverable`) — it
    # must not be treated as a bare "ignore, write nothing" outcome.
    assert bmc.Decision.UNDELIVERABLE_NO_EMAIL not in bmc.IGNORE_DECISIONS
    assert bmc.Decision.IGNORE_EVENT in bmc.IGNORE_DECISIONS
    assert bmc.Decision.IGNORE_TEST_MODE in bmc.IGNORE_DECISIONS
    assert bmc.Decision.IGNORE_AMOUNT in bmc.IGNORE_DECISIONS
    assert bmc.Decision.IGNORE_CURRENCY in bmc.IGNORE_DECISIONS
    assert bmc.Decision.ISSUE not in bmc.IGNORE_DECISIONS
