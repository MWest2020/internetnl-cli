"""Operator CLI: `netnl-admin` — credential issuance/revocation and prune.

Exit codes: 0 ok, 1 usage/settings error, 2 argparse's own usage error
(unchanged convention — see `internetnl_cli.cli`, which lets argparse's
`SystemExit(2)` propagate rather than catching it).
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from typing import IO, Mapping

from netnl import auth, retention, store
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

    return parser


def _user_add(conn, name: str, now: datetime, stdout: IO[str], stderr: IO[str]) -> int:
    if store.find_credential(conn, name) is not None:
        print(f"error: user '{name}' already exists", file=stderr)
        return 1
    password = auth.new_password()
    salt = auth.new_salt()
    created_at = store.utcnow_iso(lambda: now)
    store.add_credential(
        conn,
        username=name,
        password_hash=auth.hash_password(password, salt),
        salt=salt.hex(),
        created_at=created_at,
    )
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

    return 1  # unreachable: argparse enforces valid subcommands


if __name__ == "__main__":
    raise SystemExit(main())
