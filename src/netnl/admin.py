"""Operator CLI: `netnl-admin` — credential issuance/revocation and prune.

Exit codes: 0 ok, 1 usage/settings error, 2 argparse's own usage error
(unchanged convention — see `internetnl_cli.cli`, which lets argparse's
`SystemExit(2)` propagate rather than catching it).
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from decimal import Decimal
from typing import IO, Mapping

from netnl import auth, issue, retention, store
from netnl.errors import SettingsError
from netnl.settings import load


def _build_parser():
    import argparse

    parser = argparse.ArgumentParser(prog="netnl-admin")
    subparsers = parser.add_subparsers(dest="command", required=True)

    user = subparsers.add_parser("user", help="manage tenant credentials")
    user_sub = user.add_subparsers(dest="user_command", required=True)

    add = user_sub.add_parser("add", help="issue a new credential")
    add.add_argument("name")

    revoke = user_sub.add_parser("revoke", help="revoke a credential")
    revoke.add_argument("name")

    # Builder-review fix (S6=B3): the kill switch (`user revoke`) was
    # one-directional — re-enabling a revoked username with `user add`
    # fails ("already exists"). `reissue` works on the existing row
    # (revoked or not): fresh password/salt, `revoked_at` cleared.
    reissue = user_sub.add_parser(
        "reissue", help="re-key an existing credential (works whether revoked or not)"
    )
    reissue.add_argument("name")
    # Round-4 builder-review fix (N3): re-keying a row that is currently
    # *active* immediately invalidates that credential's live password for
    # anyone using it. Only a row that is already revoked (the intended
    # kill-switch-reversal use, D17) is reissued without this flag.
    reissue.add_argument(
        "--force",
        action="store_true",
        help="also re-key a currently active (non-revoked) credential",
    )

    user_sub.add_parser("list", help="list credentials")

    subparsers.add_parser("prune", help="apply retention windows")

    # openspec/changes/add-bazibo-unlock: operator side of the unlock codes.
    bz = subparsers.add_parser("bazibo", help="Bazibo unlock codes")
    bz_sub = bz.add_subparsers(dest="bazibo_command", required=True)
    mint = bz_sub.add_parser(
        "mint",
        help="mint one unlock code by hand: print it, or with --mail send it like the webhook does",
    )
    mint.add_argument(
        "--mail", metavar="ADDRESS",
        help="mail the code to ADDRESS (bilingual mail with links and QR) instead of printing it",
    )
    mint.add_argument(
        "--ref", metavar="TRANSACTION_ID",
        help="BMC transaction id (from the sale notification); the idempotency key, "
             "shared with the webhook, so a purchase is never mailed twice",
    )
    bz_sub.add_parser(
        "poll", help="mail codes for Bazibo Extra purchases listed by the BMC API (CronJob)"
    )
    bz_sub.add_parser(
        "watch",
        help="mail NETNL_BAZIBO_ALERT_TO once per transaction whose code was not delivered "
             "within NETNL_BAZIBO_ALERT_AFTER_MINUTES (CronJob)",
    )
    for name, text in (
        ("revoke", "revoke a code id (refused on every device from now on)"),
        ("unrevoke", "lift a revocation"),
        ("show", "device count and revocation state of a code id"),
    ):
        cmd = bz_sub.add_parser(name, help=text)
        cmd.add_argument("code_id", help="20 hex characters")

    return parser


def _reject_colon_in_username(name: str, stderr: IO[str]) -> bool:
    """Return True (and print an error) if `name` contains a ':'.

    Per RFC 7617 a Basic userid can never contain a colon; this facade's
    own `auth._parse_basic_auth` splits `Authorization: Basic` payloads on
    the *first* colon, so a username minted with one here could never
    authenticate — it would silently and permanently lock the tenant out,
    not just be an odd-looking name. Reject it at issuance instead.
    """
    if ":" in name:
        print(f"error: username must not contain ':' (got '{name}')", file=stderr)
        return True
    return False


def _user_add(conn, name: str, now: datetime, stdout: IO[str], stderr: IO[str]) -> int:
    if _reject_colon_in_username(name, stderr):
        return 1
    if store.find_credential(conn, name) is not None:
        print(f"error: user '{name}' already exists", file=stderr)
        return 1
    created_at = store.utcnow_iso(lambda: now)
    # Shared with the supporter webhook bridge (openspec/changes/
    # add-supporter-issuance) — see `netnl/issue.py`.
    password = issue.issue_credential(conn, username=name, created_at=created_at)
    store.record_audit(conn, at=created_at, credential=name, event="user-add")
    # The generated password is shown exactly once, here, and stored nowhere.
    print(password, file=stdout)
    return 0


def _user_revoke(conn, name: str, now: datetime, stderr: IO[str]) -> int:
    revoked_at = store.utcnow_iso(lambda: now)
    if not store.revoke_credential(conn, name, revoked_at):
        print(f"error: no active user '{name}'", file=stderr)
        return 1
    store.record_audit(conn, at=revoked_at, credential=name, event="user-revoke")
    return 0


def _user_reissue(
    conn, name: str, now: datetime, force: bool, stdout: IO[str], stderr: IO[str]
) -> int:
    """Builder-review fix (S6=B3): the other half of the kill switch — a
    revoked (or never-touched) row gets a fresh password/salt and
    `revoked_at` cleared, in place. Unlike `_user_add`, this never refuses
    on "already exists"; it refuses only when the username has no row at
    all (nothing to reissue — use `user add` for a brand new name).

    Round-4 builder-review fix (N3): re-keying a row that is currently
    *active* (not revoked) immediately invalidates that credential's live
    password for anyone using it — a much bigger blast radius than the
    intended kill-switch-reversal use (re-keying an already-revoked row,
    which nobody could authenticate as anyway). That now needs `--force`;
    reissuing an already-revoked row does not.
    """
    if _reject_colon_in_username(name, stderr):
        return 1
    credential = store.find_credential(conn, name)
    if credential is None:
        print(f"error: no user '{name}' to reissue (use 'user add' for a new name)", file=stderr)
        return 1
    previous_revoked_at = credential["revoked_at"]
    if previous_revoked_at is None and not force:
        print(
            f"error: user '{name}' is not revoked; reissuing it would immediately "
            "invalidate its current password for anyone using it — pass --force "
            "if that is intended",
            file=stderr,
        )
        return 1
    password = auth.new_password()
    salt = auth.new_salt()
    reissued_at = store.utcnow_iso(lambda: now)
    store.reissue_credential(
        conn, name, password_hash=auth.hash_password(password, salt), salt=salt.hex()
    )
    store.record_audit(
        conn,
        at=reissued_at,
        credential=name,
        event="user-reissue",
        detail=f"previous-revoked-at={previous_revoked_at or 'none'}",
    )
    # Same one-time-print convention as `_user_add`: shown here, stored
    # nowhere. For the demo tenant specifically, the demo never
    # authenticates as this credential (design.md, D9) — throw this away
    # exactly like the password `user add` printed originally.
    print(password, file=stdout)
    return 0


def _user_list(conn, stdout: IO[str]) -> int:
    for row in store.list_credentials(conn):
        state = row["revoked_at"] if row["revoked_at"] is not None else "active"
        print(f"{row['username']}\t{row['created_at']}\t{state}", file=stdout)
    return 0


def _prune(conn, settings, now: datetime, stdout: IO[str]) -> int:
    counts = retention.prune(conn, settings, now)
    print(f"requests pruned: {counts['requests_deleted']}", file=stdout)
    print(f"audit records pruned: {counts['audit_deleted']}", file=stdout)
    # openspec/changes/add-demo-run, D11: printed only when the demo family
    # is configured — an operator who never opted in sees output
    # byte-identical to before this change.
    if settings.demo is not None:
        print(f"demo requests pruned: {counts['demo_deleted']}", file=stdout)
    # openspec/changes/add-supporter-issuance: printed only when the
    # bridge is configured — an operator who never opted in sees output
    # byte-identical to before this change, mirroring the demo line above
    # (even though, unlike the demo delete, this counter is computed
    # unconditionally in retention.py — see that module's comment).
    if settings.supporter is not None:
        print(f"supporter issuance records pruned: {counts['issuance_deleted']}", file=stdout)
    return 0


def _valid_code_id(code_id: str, stderr: IO[str]) -> bool:
    if len(code_id) == 20 and all(ch in "0123456789abcdef" for ch in code_id):
        return True
    print("error: a code id is 20 lowercase hex characters", file=stderr)
    return False


def _bazibo_poll(settings, now: datetime, stdout: IO[str], stderr: IO[str], *, opener=None, sender=None) -> int:
    """Off (exit 0, one line saying why) unless Bazibo is enabled, the BMC
    bridge (mail) is configured and `NETNL_BMC_API_TOKEN` is set, so the
    CronJob can be deployed before any of that exists."""
    from netnl import bmc_poll, mail

    bz, cfg = settings.bazibo, settings.supporter
    if bz is None or not bz.enabled:
        print("poll off: Bazibo is not enabled (NETNL_BAZIBO_EXTRA_ID unset)", file=stdout)
        return 0
    if not bz.bmc_api_token:
        print("poll off: NETNL_BMC_API_TOKEN is not set", file=stdout)
        return 0
    if bz.source != "poll":
        print("poll off: NETNL_BAZIBO_SOURCE is not 'poll' (the webhook mints)", file=stdout)
        return 0
    assert cfg is not None  # Bazibo enabled implies the bridge (settings.py)
    if sender is None:
        sender = mail.smtp_sender(
            host=cfg.smtp_host, port=cfg.smtp_port, username=cfg.smtp_username,
            password=cfg.smtp_password, from_addr=cfg.smtp_from, mode=cfg.smtp_mode,
            timeout=cfg.smtp_timeout,
        )
    kwargs = {"opener": opener} if opener is not None else {}
    try:
        counts = bmc_poll.poll(settings, now, sender, **kwargs)
    except bmc_poll.PollError as exc:
        print(f"error: {exc}", file=stderr)
        return 1
    print(" ".join(f"{k}={v}" for k, v in counts.items()), file=stdout)
    return 0


def _bazibo_watch(conn, settings, now: datetime, stdout: IO[str], stderr: IO[str], *, sender=None) -> int:
    """`bazibo watch`: one alert mail per transaction that qualified for a
    code and has none delivered after `NETNL_BAZIBO_ALERT_AFTER_MINUTES`
    (also failed, parked, crashed and undeliverable issuance rows). Marked
    as alerted only after its mail went out, so a mail failure is retried on
    the next run (exit 1). Off (exit 0) without `NETNL_BAZIBO_ALERT_TO` or
    the bridge's mail settings."""
    from datetime import timedelta

    from netnl import mail

    bz, cfg = settings.bazibo, settings.supporter
    if bz is None or not bz.alert_to:
        print("watch off: NETNL_BAZIBO_ALERT_TO is not set", file=stdout)
        return 0
    if cfg is None:
        print("watch off: no mail settings (NETNL_BMC_WEBHOOK_SECRET, NETNL_SMTP_*)", file=stdout)
        return 0
    if sender is None:
        sender = mail.smtp_sender(
            host=cfg.smtp_host, port=cfg.smtp_port, username=cfg.smtp_username,
            password=cfg.smtp_password, from_addr=cfg.smtp_from, mode=cfg.smtp_mode,
            timeout=cfg.smtp_timeout,
        )
    cutoff = store.utcnow_iso(lambda: now - timedelta(minutes=bz.alert_after_minutes))
    at = store.utcnow_iso(lambda: now)
    alerted = failed = 0
    for row in store.bazibo_alert_candidates(conn, cutoff):
        message = mail.build_bazibo_alert_mail(
            to=bz.alert_to, txn_id=row["txn_id"], state=row["state"], attempts=row["attempts"],
            code_id=row["code_id"], seen_at=row["seen_at"],
        )
        try:
            sender(message)
        except Exception as exc:  # noqa: BLE001 - retried on the next run
            failed += 1
            print(f"error: alert not sent ({type(exc).__name__})", file=stderr)
            continue
        store.mark_bazibo_alerted(conn, row["txn_id"], at)
        store.record_audit(
            conn, at=at, credential=None, event="bazibo-alert", detail=f"state={row['state'] or 'none'}",
        )
        alerted += 1
    print(f"alerted={alerted} failed={failed}", file=stdout)
    return 1 if failed else 0


def _bazibo_mint_mail(settings, args, now: datetime, stdout: IO[str], stderr: IO[str], *, sender=None) -> int:
    """`bazibo mint --mail ADDRESS --ref TRANSACTION_ID`: the webhook's own
    persist-then-mail step (`bazibo_issuance._process`), keyed on the BMC
    transaction id in the same `bazibo_issuance` table. Re-running it, or a
    webhook arriving later for the same transaction, sends nothing new. The
    code is mailed, not printed; the address is used for sending only and
    is not stored or printed back.
    """
    from netnl import bazibo_issuance, bmc, mail

    if not args.mail or not args.ref:
        print("error: --mail and --ref go together", file=stderr)
        return 1
    cfg = settings.supporter
    if cfg is None:
        print("error: mailing needs the BMC bridge mail settings (NETNL_BMC_WEBHOOK_SECRET, "
              "NETNL_SMTP_*)", file=stderr)
        return 1
    ref = args.ref.strip()
    if not ref or len(ref) > 128 or not ref.isprintable() or any(ch.isspace() for ch in ref):
        print("error: --ref must be a BMC transaction id (printable, no spaces, at most 128)", file=stderr)
        return 1
    if not bmc.valid_recipient(args.mail):
        print("error: --mail is not a usable single address", file=stderr)
        return 1
    if sender is None:
        sender = mail.smtp_sender(
            host=cfg.smtp_host, port=cfg.smtp_port, username=cfg.smtp_username,
            password=cfg.smtp_password, from_addr=cfg.smtp_from, mode=cfg.smtp_mode,
            timeout=cfg.smtp_timeout,
        )
    delivery = bmc.Delivery(
        event="manual", live_mode=True, transaction_id=ref, amount=Decimal("0"),
        currency="", email=args.mail,
    )
    try:
        outcome, code_id = bazibo_issuance._process(
            settings, delivery, bmc.Decision.ISSUE, now, sender, ref, manual=True,
        )
    except Exception as exc:
        print(f"error: not delivered ({getattr(exc, 'msg', type(exc).__name__)}); "
              "run the same command again to retry", file=stderr)
        return 1
    if outcome == "issued":
        print(f"mailed: ref {ref} code_id {code_id}", file=stdout)
        return 0
    if outcome == "duplicate":
        print(f"already delivered for ref {ref}; nothing sent", file=stdout)
        return 0
    print(f"not sent for ref {ref}: {outcome}", file=stderr)
    return 1


def _bazibo(conn, settings, args, now: datetime, stdout: IO[str], stderr: IO[str]) -> int:
    from netnl import bazibo

    bz = settings.bazibo
    if bz is None:
        print("error: NETNL_BAZIBO_SIGNING_KEY is not set", file=stderr)
        return 1
    at = store.utcnow_iso(lambda: now)
    if args.bazibo_command == "poll":
        return _bazibo_poll(settings, now, stdout, stderr)
    if args.bazibo_command == "watch":
        return _bazibo_watch(conn, settings, now, stdout, stderr)
    if args.bazibo_command == "mint" and (args.mail or args.ref):
        return _bazibo_mint_mail(settings, args, now, stdout, stderr)
    if args.bazibo_command == "mint":
        code_id = bazibo.new_code_id()
        code = bazibo.mint(bz.signing_seed, day=bazibo.day_number(now), code_id=code_id)
        store.insert_bazibo_issuance(
            conn, txn_id=f"manual-{code_id.hex()}", code_id=code_id.hex(),
            state=store.SUPPORTER_DELIVERED, attempts=1, created_at=at, updated_at=at,
        )
        store.record_audit(
            conn, at=at, credential=None, event="bazibo-manual-mint",
            detail=f"code_id={code_id.hex()}",
        )
        # Shown once, here, like `user add`'s password; stored nowhere.
        print(f"code_id {code_id.hex()}", file=stdout)
        print(code, file=stdout)
        for link in bazibo.links(code):
            print(link, file=stdout)
        return 0
    if not _valid_code_id(args.code_id, stderr):
        return 1
    if args.bazibo_command == "revoke":
        if not store.revoke_bazibo_code(conn, args.code_id, reason="manual", at=at):
            print("error: already revoked", file=stderr)
            return 1
        store.record_audit(
            conn, at=at, credential=None, event="bazibo-revoke", detail=f"code_id={args.code_id}"
        )
        return 0
    if args.bazibo_command == "unrevoke":
        if not store.unrevoke_bazibo_code(conn, args.code_id):
            print("error: not revoked", file=stderr)
            return 1
        store.record_audit(
            conn, at=at, credential=None, event="bazibo-unrevoke", detail=f"code_id={args.code_id}"
        )
        return 0
    # show
    devices = store.count_bazibo_devices(conn, args.code_id)
    revoked = store.bazibo_revocation(conn, args.code_id)
    state = f"revoked ({revoked['reason']}, {revoked['revoked_at']})" if revoked else "active"
    print(f"devices {devices}\tlimit {bz.device_limit}\t{state}", file=stdout)
    return 0


def main(
    argv: list[str] | None = None,
    *,
    stdout: IO[str] | None = None,
    stderr: IO[str] | None = None,
    env: Mapping[str, str] | None = None,
) -> int:
    stdout = stdout if stdout is not None else sys.stdout
    stderr = stderr if stderr is not None else sys.stderr
    env = env if env is not None else os.environ

    parser = _build_parser()
    args = parser.parse_args(argv)  # argparse's own usage errors: SystemExit(2)

    try:
        settings = load(env)
    except SettingsError as exc:
        print(f"error: {exc}", file=stderr)
        return 1

    conn = store.connect(settings.db)
    store.migrate(conn)
    now = datetime.now(timezone.utc)

    if args.command == "user":
        if args.user_command == "add":
            return _user_add(conn, args.name, now, stdout, stderr)
        if args.user_command == "revoke":
            return _user_revoke(conn, args.name, now, stderr)
        if args.user_command == "reissue":
            return _user_reissue(conn, args.name, now, args.force, stdout, stderr)
        if args.user_command == "list":
            return _user_list(conn, stdout)
    if args.command == "prune":
        return _prune(conn, settings, now, stdout)
    if args.command == "bazibo":
        return _bazibo(conn, settings, args, now, stdout, stderr)

    return 1  # unreachable: argparse enforces valid subcommands


if __name__ == "__main__":
    raise SystemExit(main())
