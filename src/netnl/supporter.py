"""`POST /webhooks/bmc`: the Buy Me a Coffee webhook bridge that turns a
qualifying donation into a `netnl` tenant credential, mailed to the donor.

See `openspec/changes/add-supporter-issuance/design.md` for the pinned
decisions (D1-D5) this module implements. Registered from `api.py` only
when `settings.supporter` is not `None` — see that module's `create_app`.

Security review round (post-147903c): a concurrency bug (B1) and a
crash-loop/accounting bug (B2) in the idempotency/mint step below — see
`_persist_and_mint` and `_record_delivery_outcome`'s docstrings for the
measured races and the fixes.
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
# `audit.detail`. Security review fix: raised from 64 to 128 to match
# `bmc.parse_delivery`'s own cap on `transaction_id` (`max_len=128`) — a
# shorter cap here silently truncated the id an operator would otherwise
# use to correlate an audit row with its `supporter_issuance` row.
_MAX_SANITIZED_LEN = 128


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

    `starlette.requests.ClientDisconnect` (raised by `request.stream()`
    itself if the client goes away mid-upload) is deliberately let through
    unchanged rather than wrapped into a `NetnlHTTPError` — there is no
    client left to answer, and `api.py` registers a dedicated, quiet
    handler for it precisely so this ordinary case does not fall into the
    generic "unexpected error" handler and get logged as one.
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


def _pending_lease_seconds(cfg: SupporterSettings) -> int:
    """How long a `pending` row is treated as "another delivery attempt is
    still genuinely in flight" before a later call for the same
    transaction is allowed to take it over (security review fix, B1(a)).

    Derived from `cfg.smtp_timeout` plus a fixed 30s safety margin rather
    than a bare constant: the only thing that can legitimately keep a row
    `pending` is the mail send itself, which is bounded by that timeout —
    a lease shorter than it could take over (and revoke) a credential
    whose mail send is still genuinely running. Measured without this
    fix: 5 concurrent, identically-signed deliveries for the same
    transaction minted 5 credentials and left an active orphan no
    `supporter_issuance` row referenced — see `_persist_and_mint`'s
    docstring.
    """
    return cfg.smtp_timeout + 30


def _eligible_for_takeover(existing_row: sqlite3.Row, lease_cutoff_iso: str) -> bool:
    """A `failed` row is always eligible (its delivery attempt already
    concluded). A `pending` row is eligible only once it is older than the
    lease — see `_pending_lease_seconds`. `delivered`/`undeliverable` are
    handled by their own early returns before this is ever called.
    """
    if existing_row["state"] == store.SUPPORTER_FAILED:
        return True
    if existing_row["state"] == store.SUPPORTER_PENDING:
        return existing_row["updated_at"] <= lease_cutoff_iso
    return False


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
    hourly cap, lease-in-progress, username allocation failure) — the
    transaction is rolled back before it propagates.

    Security review fix (B1): `BEGIN IMMEDIATE` serialises concurrent
    writers on *this* transaction, but mail is sent *outside* it (D3) — a
    row committed here as `pending` is visible, and was previously treated
    as "abandoned, safe to take over", to any concurrent call for the same
    transaction that acquires the write lock next, even though the first
    call's own mail-send is still genuinely running. Measured without the
    lease check below: 5 concurrent, identically-signed deliveries for one
    transaction minted 5 credentials, each one's `_persist_and_mint`
    immediately revoking the previous call's still-in-flight credential —
    and because `_record_delivery_outcome` used to overwrite the row
    unconditionally, whichever call's mail happened to finish *last*
    stamped the row with its own username, leaving every other call's
    (already revoked, or in one case never-revoked-because-it-finished-
    after-being-overwritten) credential an active orphan no row
    referenced. B1(a) (`_eligible_for_takeover`/lease check below) closes
    the race at the write-lock boundary: a `pending` row younger than the
    lease refuses the takeover outright (503, so BMC retries once the
    in-flight attempt has resolved). B1(b) (`_record_delivery_outcome`'s
    conditional `expected_username` write) is the second, independent
    layer for the residual edge case a lease boundary/clock skew could
    still allow.
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
            lease_cutoff = store.utcnow_iso(
                lambda: now - timedelta(seconds=_pending_lease_seconds(cfg))
            )
            if not _eligible_for_takeover(existing, lease_cutoff):
                # B1(a): another call for this exact transaction is still
                # within its lease window — its own mail-send has not yet
                # concluded one way or the other. 503 so BMC retries
                # later, rather than racing that call's own eventual
                # `_record_delivery_outcome`.
                raise _delivery_failed(
                    "this transaction's issuance is already in progress; try again shortly"
                )

        # Security review fix (undeliverable-asymmetry): the hourly cap
        # gates *every* mint attempt *and* every newly-recorded
        # undeliverable outcome — checked once, before branching into
        # either path below. Previously an undeliverable delivery wrote an
        # uncapped `supporter_issuance` row regardless of volume; folding
        # it in here means a flood of no-usable-email deliveries is now
        # bounded exactly like a flood of qualifying ones is, at the
        # (deliberately conservative) cost of a legitimate donation
        # occasionally answering 503 sooner because unmailable events
        # already consumed the hour's budget. A takeover (an existing,
        # stale/failed row) is included too (B2(c)) — a crash-loop of
        # retries must not be able to mint past the hourly cap just
        # because each retry targets an *existing* transaction id.
        if existing is None or existing["state"] != store.SUPPORTER_UNDELIVERABLE:
            cutoff = store.utcnow_iso(lambda: now - timedelta(hours=1))
            if store.count_issuances_since(conn, cutoff) >= cfg.max_per_hour:
                raise _delivery_failed(
                    "the hourly supporter-issuance limit has been reached"
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

        # Security review fix (B2(b)): `attempts` counts credentials
        # *minted* for this transaction so far, incremented here — at
        # mint/takeover time — rather than only when a delivery attempt is
        # later confirmed to have failed. A process that crashes between
        # minting and ever reaching `_record_delivery_outcome` (measured:
        # an unhandled `UnicodeEncodeError` from `smtplib.login` on a
        # non-ASCII SMTP credential, before `mail.py`'s `except Exception`
        # root-fix) previously left `attempts` at its stale value forever,
        # so a crash-loop retried without bound. Counting *this* mint
        # immediately means `NETNL_SUPPORTER_MAX_ATTEMPTS` bounds the
        # number of credentials ever minted for one transaction,
        # regardless of whether a given attempt fails cleanly, is
        # abandoned past its lease, or crashes outright: a brand new
        # transaction's first mint is attempt 1; the row is only ever
        # taken over again while `existing["attempts"] < max_attempts`
        # (checked above), so at most `max_attempts` credentials are ever
        # minted in total.
        attempts = existing["attempts"] + 1 if existing is not None else 1
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
                attempts=attempts,
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

    Security review fix (B1(b)): the write is conditional on `username`
    still being the one this call itself minted (`store.update_issuance`'s
    `expected_username`) — a plain, unconditional `WHERE txn_id = ?` update
    would blindly overwrite a newer takeover's row with this (possibly
    stale) attempt's outcome, which is exactly how the B1 race left an
    active orphaned credential (see `_persist_and_mint`'s docstring). With
    B1(a) in place this should be effectively unreachable in practice, but
    is kept as an independent second layer for a lease-boundary/clock-skew
    edge case: if this call's write loses that race, its own credential
    (even one that *did* successfully deliver) is revoked instead of being
    left active-but-unreferenced by any row.
    """
    at = store.utcnow_iso(lambda: now)
    if delivered:
        updated = store.update_issuance(
            conn, txn_id, username=username, state=store.SUPPORTER_DELIVERED,
            attempts=attempts, updated_at=at, expected_username=username,
        )
        if not updated:
            store.revoke_credential(conn, username, at)
            store.record_audit(
                conn, at=at, credential=username, event="supporter-deliver-orphaned",
                detail=f"txn={sanitized_txn}",
            )
            _logger.warning(
                "event=deliver-orphaned txn_id=%s username=%s", sanitized_txn, username
            )
            return
        store.record_audit(
            conn, at=at, credential=username, event="supporter-deliver",
            detail=f"txn={sanitized_txn}",
        )
    else:
        store.revoke_credential(conn, username, at)
        store.update_issuance(
            conn, txn_id, username=username, state=store.SUPPORTER_FAILED,
            attempts=attempts, updated_at=at, expected_username=username,
        )
        # Whether or not the row write above actually matched (a takeover
        # may already have moved this row on): the credential this attempt
        # minted is unconditionally revoked either way, so there is
        # nothing further to reconcile — this attempt's own failure is
        # still worth an audit entry regardless.
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
            except Exception:
                # Best-effort, non-fatal (T5): a failed operator
                # notification must never turn a successful delivery into
                # a failure the donor would see reflected as a 503/retry.
                # Security review fix (M1): broadened from `mail.
                # DeliveryError` to `Exception` — `sender` here is the
                # same injectable seam tests use directly (not always
                # routed through `mail.smtp_sender`, whose own `except
                # Exception` root-fix guarantees only `DeliveryError` for
                # the *production* sender), so this call site does not
                # rely on that guarantee to keep its own "never fatal"
                # promise.
                _logger.warning(
                    "event=notify-failed txn_id=%s username=%s", sanitized_txn, username
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
