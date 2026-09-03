"""Pure Buy Me a Coffee (BMC) webhook payload handling: signature
verification and delivery parsing/qualification.

Deliberately imports nothing from `netnl.api`, `netnl.store`, or
`netnl.mail` — every function here is a pure function of its arguments,
unit-testable with nothing but a payload dict and a secret. All I/O (the
database, mail, the HTTP reply) lives in `netnl.supporter`, which calls
into this module.

The exact wire shape BMC's webhook sends could not be confirmed against a
live delivery at build time. The fixture payloads used in this module's own
tests are literals commented "derived from documented shape; replace with
an owner-supplied real delivery" — see
`docs/how-to/supporter-webhook.md`'s troubleshooting section for what to
check against a real delivery once one is available, and
`NETNL_BMC_SIGNATURE_HEADER` for the one thing most likely to need
adjusting.
"""

from __future__ import annotations

import base64
import binascii
import enum
import hmac
import hashlib
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

# A conservative cap on any string field pulled out of an untrusted
# payload — long before it could plausibly be a real value, and short
# enough that even a maximally abusive payload cannot make later
# processing (audit writes, mail headers) expensive.
_MAX_FIELD_LEN = 512

# Digest length for HMAC-SHA256, in raw bytes.
_DIGEST_LEN = 32


class MalformedDelivery(Exception):
    """Raised by `parse_delivery` for a delivery missing a required field,
    or one that fails a basic shape/type check. Carries only the field
    name — never any part of the payload itself, which is untrusted input
    that must never reach an HTTP reply or a log line verbatim.
    """

    def __init__(self, field: str) -> None:
        super().__init__(field)
        self.field = field


@dataclass(frozen=True)
class Delivery:
    """The handful of fields this bridge actually needs, pulled out of a
    much larger real BMC payload. Every string is already length-capped by
    `parse_delivery`. `email` is `None` when the payload carries none —
    `qualifies` (not this dataclass) decides what that means.
    """

    event: str
    live_mode: bool
    transaction_id: str
    amount: Decimal
    currency: str
    email: str | None


class Decision(enum.Enum):
    ISSUE = "issue"
    IGNORE_EVENT = "ignore_event"
    IGNORE_TEST_MODE = "ignore_test_mode"
    IGNORE_AMOUNT = "ignore_amount"
    IGNORE_CURRENCY = "ignore_currency"
    UNDELIVERABLE_NO_EMAIL = "undeliverable_no_email"


# Decisions that mean "acknowledge and do nothing" — no database row, no
# mail, no audit entry (see netnl.supporter). `UNDELIVERABLE_NO_EMAIL` is
# deliberately *not* in this set: it still writes an `undeliverable` row,
# just never mints a credential.
IGNORE_DECISIONS = frozenset(
    {
        Decision.IGNORE_EVENT,
        Decision.IGNORE_TEST_MODE,
        Decision.IGNORE_AMOUNT,
        Decision.IGNORE_CURRENCY,
    }
)

_DONATION_CREATED = "donation.created"


# --- signature verification --------------------------------------------


def _decode_digest(value: str) -> bytes | None:
    """Try hex, then base64. Returns `None` (never raises) if neither
    decodes to exactly `_DIGEST_LEN` bytes.
    """
    try:
        raw = bytes.fromhex(value)
        if len(raw) == _DIGEST_LEN:
            return raw
    except ValueError:
        pass
    try:
        raw = base64.b64decode(value, validate=True)
        if len(raw) == _DIGEST_LEN:
            return raw
    except (binascii.Error, ValueError):
        pass
    return None


def verify_signature(secret: str, header_value: str | None, raw_body: bytes) -> bool:
    """HMAC-SHA256 over `raw_body` (the exact bytes received, never a
    reparsed/reserialised form of them) under `secret`, compared to the
    decoded `header_value` in constant time.

    Accepts the digest hex- or base64-encoded, with an optional
    case-insensitive `sha256=` prefix (a shape several webhook providers
    use). This is not a weakening: both encodings represent exactly the
    same computed digest for the same secret and body — an attacker who
    cannot forge a valid digest in one encoding cannot forge it in the
    other either, since both require knowing `secret`. The tolerance exists
    because the exact header/encoding this BMC account's dashboard sends
    was not confirmed against a live delivery at build time.

    Never raises: any malformed input (missing header, bad hex/base64,
    wrong length) is simply treated as "does not match".
    """
    if not header_value:
        return False
    value = header_value.strip()
    if value.lower().startswith("sha256="):
        value = value[len("sha256="):]
    if not value:
        return False

    provided = _decode_digest(value)
    if provided is None:
        return False

    expected = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).digest()
    return hmac.compare_digest(expected, provided)


# --- delivery parsing -----------------------------------------------------


def _lookup(payload: dict, data: dict, *names: str) -> Any:
    """Tolerant of a field appearing top-level or nested under `data` —
    BMC's documented webhook shape nests most fields there, but this
    bridge does not assume that is the only shape it will ever see.
    Returns the *first* present value across `names` and both locations,
    or a sentinel `_MISSING` if none is present anywhere.
    """
    for name in names:
        if name in payload:
            return payload[name]
    for name in names:
        if name in data:
            return data[name]
    return _MISSING


_MISSING = object()


def _require_str(value: Any, field: str, *, max_len: int = _MAX_FIELD_LEN) -> str:
    if value is _MISSING or not isinstance(value, str) or not value or len(value) > max_len:
        raise MalformedDelivery(field)
    return value


def parse_delivery(payload: dict) -> Delivery:
    """Parses the fields this bridge needs out of a BMC webhook payload.

    Tolerant of the field appearing top-level or nested under a `data`
    object (BMC's documented shape nests most fields there — see the
    module docstring for the caveat on how firmly this is pinned down).
    Raises `MalformedDelivery(field)` — naming only the field — for
    anything missing, wrongly typed, or oversized.
    """
    if not isinstance(payload, dict):
        raise MalformedDelivery("payload")

    data = payload.get("data")
    if not isinstance(data, dict):
        data = {}

    event = _require_str(_lookup(payload, data, "type", "event"), "type")

    live_mode_raw = _lookup(payload, data, "live_mode")
    if not isinstance(live_mode_raw, bool):
        raise MalformedDelivery("live_mode")
    live_mode = live_mode_raw

    transaction_id = _require_str(
        _lookup(payload, data, "transaction_id"), "transaction_id", max_len=128
    )

    amount_raw = _lookup(payload, data, "amount")
    if amount_raw is _MISSING or isinstance(amount_raw, bool):
        raise MalformedDelivery("amount")
    if not isinstance(amount_raw, (str, int, float)):
        raise MalformedDelivery("amount")
    try:
        # Always via `Decimal(str(...))`, never `Decimal(float)` directly —
        # the latter would carry the float's own binary-representation
        # error into the comparison against `NETNL_SUPPORTER_MIN_AMOUNT`.
        amount = Decimal(str(amount_raw))
    except InvalidOperation as exc:
        raise MalformedDelivery("amount") from exc
    if amount < 0:
        raise MalformedDelivery("amount")

    currency = _require_str(_lookup(payload, data, "currency"), "currency", max_len=8)

    email_raw = _lookup(payload, data, "email", "supporter_email")
    email: str | None
    if email_raw is _MISSING or email_raw is None:
        email = None
    elif isinstance(email_raw, str) and email_raw and len(email_raw) <= 254:
        email = email_raw
    else:
        # Present but unusable (wrong type, empty, oversized) — treated the
        # same as absent by `qualifies` (UNDELIVERABLE_NO_EMAIL), not a
        # parse failure: a malformed recipient must never fail the whole
        # request with a 400 an attacker can use to probe field shapes.
        email = None

    return Delivery(
        event=event,
        live_mode=live_mode,
        transaction_id=transaction_id,
        amount=amount,
        currency=currency,
        email=email,
    )


# --- recipient validation ---------------------------------------------------

# Deliberately conservative: local-part and domain of only "plain" address
# characters, exactly one `@`, no whitespace/control characters, no comma
# (a second address), no angle bracket (a display-name wrapper, which would
# let a crafted "name" carry a second, hidden address or header content).
# This is a guard, not an RFC 5321 validator — it exists to keep an
# untrusted string out of a mail header and an SMTP envelope recipient
# safely, not to accept every technically-valid address.
_RECIPIENT_RE = re.compile(
    r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)+$"
)


def valid_recipient(email: str | None) -> bool:
    """Guards *both* a mail header (`To:`) and an SMTP envelope recipient
    (`RCPT TO`) use of `email` — both are injection surfaces for an
    untrusted string, so both must be checked before either use, not just
    one of them.
    """
    if not email or len(email) > 254:
        return False
    if any(ch.isspace() or ord(ch) < 0x20 for ch in email):
        return False
    if "," in email or "<" in email or ">" in email:
        return False
    if email.count("@") != 1:
        return False
    return bool(_RECIPIENT_RE.fullmatch(email))


# --- qualification -----------------------------------------------------


@dataclass(frozen=True)
class QualifyConfig:
    """The subset of `netnl.settings.SupporterSettings` `qualifies` needs —
    kept as its own tiny shape so `bmc.py` never imports `netnl.settings`
    (avoiding any temptation to reach for more of it than these four
    fields).
    """

    accept_test_mode: bool
    min_amount: Decimal
    currency: str | None  # already upper-cased by settings.py, or None


def qualifies(delivery: Delivery, cfg: QualifyConfig) -> Decision:
    if delivery.event != _DONATION_CREATED:
        return Decision.IGNORE_EVENT
    if not delivery.live_mode and not cfg.accept_test_mode:
        return Decision.IGNORE_TEST_MODE
    if delivery.amount < cfg.min_amount:
        return Decision.IGNORE_AMOUNT
    if cfg.currency is not None and delivery.currency.upper() != cfg.currency:
        return Decision.IGNORE_CURRENCY
    if not valid_recipient(delivery.email):
        return Decision.UNDELIVERABLE_NO_EMAIL
    return Decision.ISSUE
