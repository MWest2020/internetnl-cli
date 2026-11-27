"""Polling fallback for Bazibo Extra purchases (openspec/changes/
add-bazibo-support-created): `netnl-admin bazibo poll`, run every few
minutes by a CronJob.

Some BMC accounts offer only "support created" in their webhook settings,
so an Extra purchase may arrive by no webhook at all. BMC's developer API
lists Extra purchases (`GET /v1/extras`, Bearer token, read-only scope).
Documented response (developers.buymeacoffee.com/apireference.md, "Extra
purchases", fetched 2026-11-27; that site says it is no longer maintained):

    {"current_page": 1, "data": [{
        "purchase_id": 30, "purchased_on": "2020-05-22 13:17:48",
        "purchase_updated_on": "...", "purchase_is_revoked": 0,
        "purchase_amount": "8.00", "purchase_currency": "USD",
        "payer_email": "...", "payer_name": "", "total_paid_amount": "10.00",
        "extra": {"reward_id": 28, "reward_title": "...", ...}}, ...],
     "next_page_url": null, "last_page": 1, "per_page": 5, ...}

There is no transaction id in this response. Each purchase is keyed
`bmc-extra:<purchase_id>` and goes through the same persist-then-mail step
(`bazibo_issuance._process`), so the poller never mails one purchase twice.
It cannot recognise a purchase the webhook or `netnl-admin bazibo mint
--ref` already handled (those key on the transaction id): run either the
poller or the webhook path for Extras, not both. The poller is off without
`NETNL_BMC_API_TOKEN` and no CronJob runs it yet (BMC's developer site did
not issue the owner a token, 2026-11-27).

The parser is tolerant: ids as integers or digit strings, amounts as
numbers or strings, a missing field skips the item rather than failing the
run. The token is sent only to `NETNL_BMC_API_BASE`: pages are requested by
number, never by following a URL from the response, and redirects are
refused. Logged: counts and purchase ids. Never the token or an email.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

from internetnl_cli.client import urllib_opener
from internetnl_cli.errors import TransportError

from netnl import bazibo_issuance, bmc
from netnl.settings import Settings

_logger = logging.getLogger("netnl.supporter")


class PollError(Exception):
    """The API could not be read. Message is static-ish: status code or
    transport failure kind, never the token or a response body."""


@dataclass(frozen=True)
class ApiPurchase:
    purchase_id: str
    purchased_on: datetime | None
    revoked: bool
    amount: Decimal
    currency: str
    email: str | None
    reward_id: str | None
    reward_title: str | None


def _amount(value) -> Decimal | None:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return None
    try:
        amount = Decimal(str(value))
    except InvalidOperation:
        return None
    return amount if amount.is_finite() and amount >= 0 else None


def _when(value) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value.strip().replace(" ", "T", 1))
    except ValueError:
        return None
    # BMC documents no zone; treat it as UTC (only used for the lookback).
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def parse_api_purchase(item: object) -> ApiPurchase | None:
    if not isinstance(item, dict):
        return None
    purchase_id = bmc._extra_id(item.get("purchase_id"))
    amount = _amount(item.get("purchase_amount"))
    currency = item.get("purchase_currency")
    if purchase_id is None or amount is None or not isinstance(currency, str) or len(currency) > 8:
        return None
    extra = item.get("extra")
    if not isinstance(extra, dict):
        extra = {}
    title = extra.get("reward_title")
    email = item.get("payer_email")
    revoked = item.get("purchase_is_revoked")
    return ApiPurchase(
        purchase_id=purchase_id,
        purchased_on=_when(item.get("purchased_on")),
        revoked=bool(revoked) and revoked not in ("0", "false"),
        amount=amount,
        currency=currency,
        email=email if isinstance(email, str) and len(email) <= 254 else None,
        reward_id=bmc._extra_id(extra.get("reward_id")),
        reward_title=title if isinstance(title, str) and len(title) <= 512 else None,
    )


def fetch_page(settings: Settings, page: int, opener) -> tuple[list, bool]:
    bz = settings.bazibo
    assert bz is not None and bz.bmc_api_token
    url = f"{bz.bmc_api_base}/extras?page={page}"
    try:
        response = opener(
            "GET", url, None,
            {"Authorization": f"Bearer {bz.bmc_api_token}", "Accept": "application/json"},
            settings.timeout,
        )
    except TransportError as exc:
        raise PollError("transport failure contacting the BMC API") from exc
    if response.status != 200:
        raise PollError(f"BMC API answered HTTP {response.status}")
    try:
        body = json.loads(response.body)
    except (ValueError, UnicodeDecodeError) as exc:
        raise PollError("BMC API answered with something that is not JSON") from exc
    if not isinstance(body, dict):
        raise PollError("BMC API answered with an unexpected shape")
    data = body.get("data")
    if not isinstance(data, list):
        # An account without any purchase may answer without a list.
        return [], False
    has_more = bool(body.get("next_page_url"))
    last_page = body.get("last_page")
    if isinstance(last_page, int) and not isinstance(last_page, bool):
        has_more = has_more and page < last_page
    return data, has_more


def poll(settings: Settings, now: datetime, sender, opener=urllib_opener) -> dict:
    """One polling run. Returns counts; raises `PollError` when the API
    cannot be read (nothing is minted from a partial read of page 1; pages
    already processed stay processed)."""
    cfg = settings.supporter
    bz = settings.bazibo
    assert cfg is not None and bz is not None and bz.enabled and bz.bmc_api_token
    match = bmc.BaziboMatch(bz.extra_id, bz.extra_title)
    qualify = bmc.QualifyConfig(
        accept_test_mode=True, min_amount=bz.min_amount, currency=cfg.currency,
    )
    cutoff = now - timedelta(hours=bz.poll_lookback_hours)
    counts = {"seen": 0, "matched": 0, "issued": 0, "duplicate": 0, "ignored": 0, "failed": 0}

    for page in range(1, bz.poll_max_pages + 1):
        items, has_more = fetch_page(settings, page, opener)
        for item in items:
            purchase = parse_api_purchase(item)
            if purchase is None:
                continue
            counts["seen"] += 1
            if not match.matches(bmc.ExtraItem(id=purchase.reward_id, title=purchase.reward_title)):
                continue
            if purchase.revoked:
                continue
            if purchase.purchased_on is not None and purchase.purchased_on < cutoff:
                continue
            counts["matched"] += 1
            delivery = bmc.Delivery(
                event="api.extra_purchase",
                live_mode=True,
                transaction_id=f"bmc-extra:{purchase.purchase_id}",
                amount=purchase.amount,
                currency=purchase.currency,
                email=purchase.email,
            )
            decision = bmc.qualifies_bazibo(bmc.ExtraPurchase(delivery=delivery, extras=()), qualify)
            if decision in bmc.IGNORE_DECISIONS:
                counts["ignored"] += 1
                _logger.info("event=poll-ignored purchase_id=%s decision=%s",
                             purchase.purchase_id, decision.value)
                continue
            try:
                outcome, code_id = bazibo_issuance._process(
                    settings, delivery, decision, now, sender, delivery.transaction_id,
                )
            except Exception as exc:  # 503-shaped outcomes: retried next run
                counts["failed"] += 1
                _logger.warning("event=poll-failed purchase_id=%s reason=%s",
                                purchase.purchase_id, type(exc).__name__)
                continue
            counts["issued" if outcome == "issued" else
                   "duplicate" if outcome == "duplicate" else "ignored"] += 1
            if outcome == "issued":
                _logger.info("event=poll-issued purchase_id=%s code_id=%s",
                             purchase.purchase_id, code_id)
        if not has_more:
            break
    return counts
