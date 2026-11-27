"""Bazibo unlock codes from a BMC Extra purchase (openspec/changes/
add-bazibo-unlock). Called from `netnl.supporter.handle_webhook` *after*
HMAC verification and JSON parsing, only for an `extra_purchase.created`
delivery while `settings.bazibo.enabled`.

Same shape as the netnl credential path in `netnl.supporter` — persist in
one `BEGIN IMMEDIATE` step, mail outside it, record the outcome in its own
short write, 503 on a mail failure so BMC retries — with one simplification
that removes the class of race that path's docstring documents (B1/B2):
there is nothing to revoke. The code is a deterministic function of the
signing key, the code id and the issued day, so the code id is drawn once,
on the transaction's first attempt, and every retry re-derives and re-sends
the *same* code. Concurrent deliveries for one transaction can at worst
mail the same code twice; they can never put two different valid codes
into the world for one purchase.

Stored per transaction: the transaction id, the code id (hex), state,
attempt counter and timestamps. Never the buyer's email, never the code.
Logged: the transaction id and the code id. Never the code or the email.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta

from starlette.concurrency import run_in_threadpool

from netnl import bazibo, bmc, mail, store
from netnl.errors import NetnlHTTPError
from netnl.settings import BaziboSettings, Settings, SupporterSettings

_logger = logging.getLogger("netnl.supporter")


def _delivery_failed(msg: str) -> NetnlHTTPError:
    return NetnlHTTPError(503, "delivery-failed", msg)


def _persist(
    conn: sqlite3.Connection,
    cfg: SupporterSettings,
    delivery: bmc.Delivery,
    decision: bmc.Decision,
    now: datetime,
    sanitized_txn: str,
) -> tuple[str, str, int] | str:
    """Returns `"duplicate"`, `"ignored"` or `"parked"` (all 200), or
    `(code_id_hex, created_at, attempts)` for the caller to mint and mail.
    Raises `NetnlHTTPError` (503) for the hourly cap and for a `pending`
    row still inside its lease.
    """
    now_iso = store.utcnow_iso(lambda: now)
    txn_id = delivery.transaction_id

    conn.execute("BEGIN IMMEDIATE")
    try:
        existing = store.find_bazibo_issuance(conn, txn_id)
        if existing is not None:
            if existing["state"] == store.SUPPORTER_DELIVERED:
                conn.execute("COMMIT")
                return "duplicate"
            if existing["state"] == store.SUPPORTER_UNDELIVERABLE:
                conn.execute("COMMIT")
                return "ignored"
            if existing["attempts"] >= cfg.max_attempts:
                # Parked: no further attempt will be made for this
                # transaction, so a 503 would only make BMC retry for
                # nothing and count towards the consecutive failures that
                # disable the whole webhook (BMC disables it after 10).
                conn.execute("COMMIT")
                return "parked"
            if existing["state"] == store.SUPPORTER_PENDING:
                lease_cutoff = store.utcnow_iso(
                    lambda: now - timedelta(seconds=cfg.pending_lease_seconds)
                )
                if existing["updated_at"] > lease_cutoff:
                    raise _delivery_failed(
                        "this transaction's issuance is already in progress; try again shortly"
                    )

        cutoff = store.utcnow_iso(lambda: now - timedelta(hours=1))
        if existing is None and store.count_bazibo_issuances_since(conn, cutoff) >= cfg.max_per_hour:
            raise _delivery_failed("the hourly issuance limit has been reached")

        if decision == bmc.Decision.UNDELIVERABLE_NO_EMAIL:
            if existing is None:
                store.insert_bazibo_issuance(
                    conn, txn_id=txn_id, code_id="", state=store.SUPPORTER_UNDELIVERABLE,
                    attempts=0, created_at=now_iso, updated_at=now_iso,
                )
            else:
                store.update_bazibo_issuance(
                    conn, txn_id, state=store.SUPPORTER_UNDELIVERABLE,
                    attempts=existing["attempts"], updated_at=now_iso,
                )
            store.record_audit(
                conn, at=now_iso, credential=None, event="bazibo-undeliverable",
                detail=f"txn={sanitized_txn}",
            )
            conn.execute("COMMIT")
            return "ignored"

        if existing is None:
            code_id = bazibo.new_code_id().hex()
            created_at = now_iso
            attempts = 1
            store.insert_bazibo_issuance(
                conn, txn_id=txn_id, code_id=code_id, state=store.SUPPORTER_PENDING,
                attempts=attempts, created_at=created_at, updated_at=now_iso,
            )
        else:
            code_id = existing["code_id"] or bazibo.new_code_id().hex()
            created_at = existing["created_at"]
            attempts = existing["attempts"] + 1
            if existing["code_id"]:
                store.update_bazibo_issuance(
                    conn, txn_id, state=store.SUPPORTER_PENDING, attempts=attempts,
                    updated_at=now_iso,
                )
            else:  # pragma: no cover - a row without a code id is only ever undeliverable
                raise _delivery_failed("inconsistent issuance row")
        store.record_audit(
            conn, at=now_iso, credential=None, event="bazibo-issue",
            detail=f"txn={sanitized_txn} code_id={code_id}",
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return code_id, created_at, attempts


def _record_outcome(
    conn: sqlite3.Connection, *, txn_id: str, sanitized_txn: str, code_id: str,
    attempts: int, now: datetime, delivered: bool,
) -> None:
    """Conditional on `attempts` still being this attempt's own number, so a
    stale attempt never overwrites a newer takeover's state. Losing that
    race is harmless here (the other attempt mails the same code)."""
    at = store.utcnow_iso(lambda: now)
    state = store.SUPPORTER_DELIVERED if delivered else store.SUPPORTER_FAILED
    store.update_bazibo_issuance(
        conn, txn_id, state=state, attempts=attempts, updated_at=at,
        expected_attempts=attempts,
    )
    store.record_audit(
        conn, at=at, credential=None,
        event="bazibo-deliver" if delivered else "bazibo-deliver-failed",
        detail=f"txn={sanitized_txn} code_id={code_id}",
    )


def _process(
    settings: Settings, delivery: bmc.Delivery, decision: bmc.Decision,
    now: datetime, sender, sanitized_txn: str,
) -> tuple[str, str]:
    cfg = settings.supporter
    bz = settings.bazibo
    assert cfg is not None and bz is not None

    conn = store.connect(settings.db)
    try:
        outcome = _persist(conn, cfg, delivery, decision, now, sanitized_txn)
        if isinstance(outcome, str):
            return outcome, ""
        code_id, created_at, attempts = outcome

        code = bazibo.mint(
            bz.signing_seed, day=bazibo.day_from_iso(created_at), code_id=bytes.fromhex(code_id)
        )
        link, link_pages = bazibo.links(code)
        code_mail = mail.build_bazibo_mail(
            to=delivery.email, code=code, link=link, link_pages=link_pages,
            device_limit=bz.device_limit,
        )
        try:
            sender(code_mail)
        except Exception:
            # `mail.smtp_sender` only ever raises `DeliveryError`; anything
            # else from an injected sender is treated the same way rather
            # than left as a `pending` row until its lease runs out.
            _record_outcome(
                conn, txn_id=delivery.transaction_id, sanitized_txn=sanitized_txn,
                code_id=code_id, attempts=attempts, now=now, delivered=False,
            )
            raise _delivery_failed("could not deliver the unlock code mail")

        _record_outcome(
            conn, txn_id=delivery.transaction_id, sanitized_txn=sanitized_txn,
            code_id=code_id, attempts=attempts, now=now, delivered=True,
        )

        if cfg.notify:
            try:
                sender(mail.build_bazibo_notify_mail(
                    to=cfg.notify, code_id=code_id, txn_id=sanitized_txn,
                ))
            except Exception:
                _logger.warning("event=bazibo-notify-failed txn_id=%s", sanitized_txn)
        return "issued", code_id
    finally:
        conn.close()


async def handle(payload: dict, request, settings: Settings, sanitize) -> dict:
    """Route body for an `extra_purchase.created` delivery while Bazibo is
    enabled. Never touches the netnl credential tables."""
    cfg = settings.supporter
    bz: BaziboSettings | None = settings.bazibo
    assert cfg is not None and bz is not None and bz.enabled

    try:
        purchase = bmc.parse_extra_purchase(payload)
    except bmc.MalformedDelivery as exc:
        raise NetnlHTTPError(400, "bad-request", f"invalid field: {exc.field}") from exc

    if not bmc.is_bazibo_purchase(purchase, bmc.BaziboMatch(bz.extra_id, bz.extra_title)):
        _logger.info("event=ignored decision=other_extra")
        return {"status": "ignored"}

    decision = bmc.qualifies_bazibo(purchase, bmc.QualifyConfig(
        accept_test_mode=cfg.accept_test_mode, min_amount=bz.min_amount, currency=cfg.currency,
    ))
    if decision in bmc.IGNORE_DECISIONS:
        _logger.info("event=ignored decision=bazibo_%s", decision.value)
        return {"status": "ignored"}

    sanitized_txn = sanitize(purchase.delivery.transaction_id)
    outcome, code_id = await run_in_threadpool(
        _process, settings, purchase.delivery, decision, request.app.state.now(),
        request.app.state.sender, sanitized_txn,
    )
    if code_id:
        _logger.info("event=bazibo-%s txn_id=%s code_id=%s", outcome, sanitized_txn, code_id)
    else:
        _logger.info("event=bazibo-%s txn_id=%s", outcome, sanitized_txn)
    if outcome in ("issued", "duplicate"):
        return {"status": "ok"}
    return {"status": "ignored"}
