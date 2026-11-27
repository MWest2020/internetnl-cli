"""`POST /webhooks/stripe`: Bazibo unlock codes from a Stripe Payment Link
(openspec/changes/add-bazibo-unlock, design.md D12; Mark, 27 Nov 2026: a
Stripe Payment Link at EUR 1, because Buy Me a Coffee's coffee price can only
be 3, 5 or 10 and its Extras send this account no webhook).

Order: size cap on the bytes read -> `Stripe-Signature` check (nothing else
happens without it) -> JSON -> event type -> the Checkout Session's payment
link, payment status and mode -> the shared persist-then-mail step
(`bazibo_issuance._process`), keyed on the Checkout Session id. Every other
event, and every session of another payment link, is answered 200 and
ignored, so Stripe does not retry it.

Stored: the session id, the code id, state, attempts, timestamps
(`bazibo_issuance`) and the watch row (`bazibo_watch`). Never the buyer's
email, never the code. Logged: the session id and the code id.

The signature check is Stripe's documented scheme, with the standard library
only (the `stripe` package is not a dependency): the header carries
`t=<unix seconds>` and one or more `v1=<hex>`; `v1` is HMAC-SHA256 with the
endpoint secret over `"<t>." + raw body`. A timestamp more than
`TOLERANCE_SECONDS` from now is refused (replay window), and every `v1` is
compared in constant time.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
from datetime import datetime
from decimal import Decimal

from fastapi import Request
from starlette.concurrency import run_in_threadpool

from netnl import bazibo_issuance, bmc, mail
from netnl.errors import NetnlHTTPError
from netnl.settings import Settings

_logger = logging.getLogger("netnl.stripe")

TOLERANCE_SECONDS = 300
MAX_BODY_BYTES = 262_144
_MAX_HEADER_LEN = 4096

# A session that completed with the money in: `checkout.session.completed`
# with `payment_status: "paid"` (cards, iDEAL), or, for a payment method that
# settles later, `checkout.session.async_payment_succeeded`.
HANDLED_EVENTS = frozenset({"checkout.session.completed", "checkout.session.async_payment_succeeded"})

_SESSION_RE = re.compile(r"^cs_[A-Za-z0-9_]{1,250}$")


def verify_signature(secret: str, header: str | None, raw: bytes, now: float) -> bool:
    """True only for a `Stripe-Signature` header with a timestamp within
    `TOLERANCE_SECONDS` of `now` and at least one `v1` that matches."""
    if not header or len(header) > _MAX_HEADER_LEN:
        return False
    timestamp = None
    candidates: list[str] = []
    for part in header.split(","):
        key, sep, value = part.strip().partition("=")
        if not sep:
            continue
        if key == "t":
            if not value.isascii() or not value.isdigit() or len(value) > 12:
                return False
            timestamp = int(value)
        elif key == "v1":
            candidates.append(value)
    if timestamp is None or not candidates:
        return False
    if abs(now - timestamp) > TOLERANCE_SECONDS:
        return False
    expected = hmac.new(
        secret.encode("utf-8"), f"{timestamp}.".encode("ascii") + raw, hashlib.sha256
    ).hexdigest()
    matched = False
    for candidate in candidates:
        # no early exit: every candidate is compared
        matched |= hmac.compare_digest(expected, candidate)
    return matched


def _str(value: object, max_len: int = 255) -> str | None:
    return value if isinstance(value, str) and 0 < len(value) <= max_len else None


async def _read_bounded_body(request: Request) -> bytes:
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > MAX_BODY_BYTES:
            raise NetnlHTTPError(400, "bad-request", f"request body exceeds the {MAX_BODY_BYTES}-byte limit")
    return bytes(body)


async def handle(request: Request, settings: Settings) -> dict:
    bz = settings.bazibo
    assert bz is not None and bz.stripe_webhook_secret and settings.supporter is not None

    raw = await _read_bounded_body(request)
    now: datetime = request.app.state.now()
    if not verify_signature(bz.stripe_webhook_secret, request.headers.get("Stripe-Signature"), raw, now.timestamp()):
        _logger.info("event=stripe-signature-rejected")
        raise NetnlHTTPError(401, "unauthorised", "invalid signature")

    try:
        event = json.loads(raw)
    except (ValueError, UnicodeDecodeError) as exc:
        raise NetnlHTTPError(400, "bad-request", "malformed JSON body") from exc
    if not isinstance(event, dict):
        raise NetnlHTTPError(400, "bad-request", "invalid field: event")
    event_type = _str(event.get("type"), 128)
    if event_type is None:
        raise NetnlHTTPError(400, "bad-request", "invalid field: type")
    if event_type not in HANDLED_EVENTS:
        safe = event_type if re.fullmatch(r"[a-z_.]{1,64}", event_type) else "other"
        _logger.info("event=ignored decision=stripe_event type=%s", safe)
        return {"status": "ignored"}

    data = event.get("data")
    session = data.get("object") if isinstance(data, dict) else None
    if not isinstance(session, dict):
        raise NetnlHTTPError(400, "bad-request", "invalid field: data.object")
    session_id = _str(session.get("id"))
    if session_id is None or not _SESSION_RE.fullmatch(session_id):
        raise NetnlHTTPError(400, "bad-request", "invalid field: id")
    ref = mail.safe_ref(session_id)

    # Which product first: another payment link of the same account is
    # none of our business, whatever else it looks like.
    if session.get("payment_link") != bz.stripe_payment_link_id:
        _logger.info("event=ignored decision=stripe_other_link txn_id=%s", ref)
        return {"status": "ignored"}
    if session.get("mode") not in (None, "payment") or session.get("payment_status") != "paid":
        _logger.info("event=ignored decision=stripe_unpaid txn_id=%s", ref)
        return {"status": "ignored"}
    live = session.get("livemode", event.get("livemode"))
    if not isinstance(live, bool):
        raise NetnlHTTPError(400, "bad-request", "invalid field: livemode")
    if not live and not bz.stripe_allow_test_mode:
        _logger.info("event=ignored decision=stripe_test_mode txn_id=%s", ref)
        return {"status": "ignored"}

    details = session.get("customer_details")
    email = details.get("email") if isinstance(details, dict) else None
    email = email if isinstance(email, str) and len(email) <= 254 else None
    amount = session.get("amount_total")
    currency = _str(session.get("currency"), 8) or ""
    delivery = bmc.Delivery(
        event="stripe",
        live_mode=live,
        transaction_id=session_id,
        amount=Decimal(amount) / 100 if isinstance(amount, int) and not isinstance(amount, bool) else Decimal(0),
        currency=currency,
        email=email,
    )
    decision = bmc.Decision.ISSUE if bmc.valid_recipient(email) else bmc.Decision.UNDELIVERABLE_NO_EMAIL

    outcome, code_id = await run_in_threadpool(
        bazibo_issuance._process, settings, delivery, decision, now, request.app.state.sender,
        ref, False, "stripe",
    )
    if code_id:
        _logger.info("event=bazibo-%s source=stripe txn_id=%s code_id=%s", outcome, ref, code_id)
    else:
        _logger.info("event=bazibo-%s source=stripe txn_id=%s", outcome, ref)
    if outcome in ("issued", "duplicate"):
        return {"status": "ok"}
    return {"status": "ignored"}
