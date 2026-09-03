"""`POST /webhooks/bmc`: the Buy Me a Coffee webhook bridge that turns a
qualifying donation into a `netnl` tenant credential, mailed to the donor.

See `openspec/changes/add-supporter-issuance/design.md` for the pinned
decisions (D1-D5) this module implements. Registered from `api.py` only
when `settings.supporter` is not `None` — see that module's `create_app`.
"""

from __future__ import annotations

import json
import logging
import secrets
import sqlite3
from datetime import datetime, timedelta

from fastapi import Request
from starlette.concurrency import run_in_threadpool

from netnl import bmc, issue, mail, store
from netnl.errors import NetnlHTTPError
from netnl.settings import Settings, SupporterSettings

_logger = logging.getLogger("netnl.supporter")

# Regenerate the randomly-generated username on a collision this many
# times before giving up — a collision on 8 hex characters (32 bits) is
# astronomically unlikely for any realistic number of supporters; this
# bound exists so a pathological run of bad luck fails loudly (503) rather
# than looping forever.
_MAX_USERNAME_ATTEMPTS = 5

# Printable-only, length-capped — mirrors `netnl.auth._sanitize_username`'s
# own treatment of attacker-controlled input before it is written into
# `audit.detail`. A BMC transaction id is external input, even though it
# arrived over a signed channel.
_MAX_SANITIZED_LEN = 64


def _sanitize(value: str) -> str:
    cleaned = "".join(ch for ch in value if ch.isprintable())
    return cleaned[:_MAX_SANITIZED_LEN]


async def _read_bounded_body(request: Request, max_bytes: int) -> bytes:
    """Reads the request body with an explicit cap on bytes actually
    received, not `Content-Length` (round-1 fix, M6, `api.py`'s
    `enforce_body_size` middleware) — a chunked-encoded request can omit or
    understate that header entirely, bypassing that check. Rejecting while
    still reading (rather than buffering an unbounded body and only
    checking its length afterwards) also bounds how much of an oversized
    body this process ever holds in memory at once.
    """
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > max_bytes:
            raise NetnlHTTPError(
                400, "bad-request", f"request body exceeds the {max_bytes}-byte limit"
            )
    return bytes(body)


def _delivery_failed(msg: str) -> NetnlHTTPError:
    return NetnlHTTPError(503, "delivery-failed", msg)


def _mint_username(
    conn: sqlite3.Connection, cfg: SupporterSettings, now_iso: str
) -> tuple[str, str]:
    """Generates `<prefix><8 hex chars>`, regenerating on a username
    collision (`sqlite3.IntegrityError` from `issue.issue_credential`) up
    to `_MAX_USERNAME_ATTEMPTS` times.
    """
    for _ in range(_MAX_USERNAME_ATTEMPTS):
        username = f"{cfg.username_prefix}{secrets.token_hex(4)}"
        try:
            password = issue.issue_credential(conn, username=username, created_at=now_iso)
        except sqlite3.IntegrityError:
            continue
        return username, password
    raise _delivery_failed("could not allocate a supporter username")


def _persist_and_mint(
    conn: sqlite3.Connection,
    cfg: SupporterSettings,
    delivery: bmc.Delivery,
    decision: "bmc.Decision",
    now: datetime,
) -> tuple[str, str, str, int] | str:
    """The whole `BEGIN IMMEDIATE` step (design.md, D3, step 6). Returns
    either a 200-shaped outcome string (`"duplicate"`, `"ignored"`) or a
    `(username, password, txn_id, attempts)` tuple for the caller to mail.
    Raises `NetnlHTTPError` for every 503 outcome (attempts exhausted,
    hourly cap, username allocation failure) — the transaction is rolled
    back before it propagates.
    """
    now_iso = store.utcnow_iso(lambda: now)
    txn_id = delivery.transaction_id
    sanitized_txn = _sanitize(txn_id)

    conn.execute("BEGIN IMMEDIATE")
    try:
        existing = store.find_issuance(conn, txn_id)

        if existing is not None:
            if existing["state"] == store.SUPPORTER_DELIVERED:
                conn.execute("COMMIT")
                return "duplicate"
            if existing["state"] == store.SUPPORTER_UNDELIVERABLE:
                conn.execute("COMMIT")
                return "ignored"
            if existing["attempts"] >= cfg.max_attempts:
                # Nothing to persist for this outcome — raising here (and
                # letting the blanket `except`/`ROLLBACK` below handle it)
                # is equivalent to a no-op commit, without the invalid
                # "ROLLBACK after COMMIT" a commit-then-raise shape here
                # would otherwise attempt on this same connection.
                raise _delivery_failed(
                    "this transaction has exhausted its delivery attempts"
                )

        if decision == bmc.Decision.UNDELIVERABLE_NO_EMAIL:
            if existing is None:
                store.insert_issuance(
                    conn,
                    txn_id=txn_id,
                    username="",
                    state=store.SUPPORTER_UNDELIVERABLE,
                    attempts=0,
                    created_at=now_iso,
                    updated_at=now_iso,
                )
            else:
                store.update_issuance(
                    conn,
                    txn_id,
                    username=existing["username"],
                    state=store.SUPPORTER_UNDELIVERABLE,
                    attempts=existing["attempts"],
                    updated_at=now_iso,
                )
            store.record_audit(
                conn,
                at=now_iso,
                credential=None,
                event="supporter-undeliverable",
                detail=f"txn={sanitized_txn}",
            )
            conn.execute("COMMIT")
            return "ignored"

        # The hourly cap only gates a *new* transaction id — retrying an
        # existing one never counted twice in `count_issuances_since`
        # (its `created_at` never changes), so it must not be blocked by
        # a cap meant to bound *new* issuance volume.
        if existing is None:
            cutoff = store.utcnow_iso(lambda: now - timedelta(hours=1))
            if store.count_issuances_since(conn, cutoff) >= cfg.max_per_hour:
                raise _delivery_failed(
                    "the hourly supporter-issuance limit has been reached"
                )

        attempts = existing["attempts"] if existing is not None else 0
        if existing is not None:
            # D3: never leave the previous, undelivered credential active
            # once a fresh one is about to be minted for the same
            # transaction.
            store.revoke_credential(conn, existing["username"], now_iso)

        username, password = _mint_username(conn, cfg, now_iso)

        if existing is None:
            store.insert_issuance(
                conn,
                txn_id=txn_id,
                username=username,
                state=store.SUPPORTER_PENDING,
                attempts=0,
                created_at=now_iso,
                updated_at=now_iso,
            )
        else:
            store.update_issuance(
                conn,
                txn_id,
                username=username,
                state=store.SUPPORTER_PENDING,
                attempts=attempts,
                updated_at=now_iso,
            )
        store.record_audit(
            conn,
            at=now_iso,
            credential=username,
            event="supporter-issue",
            detail=f"txn={sanitized_txn}",
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise

    return username, password, sanitized_txn, attempts


def _record_delivery_outcome(
    conn: sqlite3.Connection,
    *,
    txn_id: str,
    username: str,
    sanitized_txn: str,
    attempts: int,
    now: datetime,
    delivered: bool,
) -> None:
    """Mail happens outside the `BEGIN IMMEDIATE` transaction (D3) — this
    records its outcome in its own short write, never holding the write
    lock for the duration of a network call.
    """
    at = store.utcnow_iso(lambda: now)
    if delivered:
        store.update_issuance(
            conn, txn_id, username=username, state=store.SUPPORTER_DELIVERED,
            attempts=attempts, updated_at=at,
        )
        store.record_audit(
            conn, at=at, credential=username, event="supporter-deliver",
            detail=f"txn={sanitized_txn}",
        )
    else:
        store.revoke_credential(conn, username, at)
        store.update_issuance(
            conn, txn_id, username=username, state=store.SUPPORTER_FAILED,
            attempts=attempts + 1, updated_at=at,
        )
        store.record_audit(
            conn, at=at, credential=username, event="supporter-deliver-failed",
            detail=f"txn={sanitized_txn}",
        )


def _process(settings: Settings, delivery: bmc.Delivery, decision: "bmc.Decision", now: datetime, sender) -> str:
    """Everything blocking for this request — the transaction, the mint,
    the mail send, the revoke-on-failure — runs here, on one connection
    this function opens and closes itself.

    Deliberately `store.connect` (the ordinary, `check_same_thread=True`
    default), never `Depends(store.get_conn)`/`allow_cross_thread=True`:
    that dependency exists specifically because FastAPI can resolve a
    *sync* dependency, the route handler, and that dependency's own
    cleanup on three different real worker threads for one ordinary
    request (see `store.get_conn`'s docstring for the measured
    `ProgrammingError` this works around). This route is different: it is
    `async def` and *itself* chooses to run all of its blocking work
    inside exactly one `run_in_threadpool` call (see `handle_webhook`
    below) — the connection opened here is used entirely inside that one
    call, on whichever single thread the threadpool hands it, so the
    cross-thread need `get_conn` exists for does not apply, and the
    stricter default is the correct, narrower tool.

    Returns one of `"issued"`, `"duplicate"`, `"ignored"` (all 200
    outcomes); raises `NetnlHTTPError` for every 503 outcome.
    """
    cfg = settings.supporter
    assert cfg is not None

    conn = store.connect(settings.db)
    try:
        outcome = _persist_and_mint(conn, cfg, delivery, decision, now)
        if isinstance(outcome, str):
            return outcome

        username, password, sanitized_txn, attempts = outcome

        credential_mail = mail.build_credential_mail(
            to=delivery.email,
            username=username,
            password=password,
            public_endpoint=cfg.public_endpoint,
        )
        try:
            sender(credential_mail)
        except mail.DeliveryError:
            _record_delivery_outcome(
                conn, txn_id=delivery.transaction_id, username=username,
                sanitized_txn=sanitized_txn, attempts=attempts, now=now, delivered=False,
            )
            raise _delivery_failed("could not deliver the supporter credential mail")

        _record_delivery_outcome(
            conn, txn_id=delivery.transaction_id, username=username,
            sanitized_txn=sanitized_txn, attempts=attempts, now=now, delivered=True,
        )

        if cfg.notify:
            notify_mail = mail.build_notify_mail(
                to=cfg.notify, username=username, txn_id=sanitized_txn
            )
            try:
                sender(notify_mail)
            except mail.DeliveryError:
                # Best-effort, non-fatal (T5): a failed operator
                # notification must never turn a successful delivery into
                # a failure the donor would see reflected as a 503/retry.
                _logger.warning(
                    "supporter notify mail failed for txn=%s username=%s",
                    sanitized_txn, username,
                )

        return "issued"
    finally:
        conn.close()


async def handle_webhook(request: Request, settings: Settings) -> dict:
    """The route body registered as `POST /webhooks/bmc` in `api.py`.

    Ordering (design.md): size cap on bytes read -> HMAC verification (no
    DB/mail/audit on failure) -> parse -> qualify (an `IGNORE_*` decision
    short-circuits here, writing nothing) -> the blocking transaction+mail
    step, in one `run_in_threadpool` call.
    """
    cfg = settings.supporter
    assert cfg is not None  # route only registered when configured

    raw = await _read_bounded_body(request, cfg.max_body_bytes)

    header_value = request.headers.get(cfg.signature_header)
    if not bmc.verify_signature(cfg.webhook_secret, header_value, raw):
        _logger.info("event=signature-rejected")
        raise NetnlHTTPError(401, "unauthorised", "invalid signature")

    try:
        payload = json.loads(raw)
    except (ValueError, UnicodeDecodeError) as exc:
        raise NetnlHTTPError(400, "bad-request", "malformed JSON body") from exc

    try:
        delivery = bmc.parse_delivery(payload)
    except bmc.MalformedDelivery as exc:
        raise NetnlHTTPError(400, "bad-request", f"invalid field: {exc.field}") from exc

    decision = bmc.qualifies(delivery, bmc.QualifyConfig(
        accept_test_mode=cfg.accept_test_mode, min_amount=cfg.min_amount, currency=cfg.currency,
    ))
    if decision in bmc.IGNORE_DECISIONS:
        _logger.info("event=ignored decision=%s", decision.value)
        return {"status": "ignored"}

    now = request.app.state.now()
    sender = request.app.state.sender

    outcome = await run_in_threadpool(_process, settings, delivery, decision, now, sender)

    _logger.info("event=%s txn_id=%s", outcome, _sanitize(delivery.transaction_id))
    if outcome == "issued":
        return {"status": "ok"}
    if outcome == "duplicate":
        # Indistinguishable on the wire from a fresh issuance (design.md)
        # — this never reveals to the caller which one happened.
        return {"status": "ok"}
    # outcome == "ignored" (undeliverable)
    return {"status": "ignored"}
