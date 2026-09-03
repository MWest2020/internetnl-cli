"""Shared credential-minting primitive.

Used by both `netnl-admin user add` (`admin.py`) and the `POST /webhooks/
bmc` bridge (`supporter.py`) so the two paths cannot silently diverge in
*how* a credential is created — only in what happens before and after.
"""

from __future__ import annotations

import sqlite3

from netnl import auth, store


def issue_credential(conn: sqlite3.Connection, *, username: str, created_at: str) -> str:
    """Mint a brand-new credential row for `username` and return the
    plaintext password. The password exists only as this return value — it
    is never itself written to the database (only its scrypt hash is,
    inside `store.add_credential`) or logged. Raises `sqlite3.IntegrityError`
    unchanged if `username` already has a row (caller's responsibility to
    pick a free one first, or to catch and retry with a different name).
    """
    password = auth.new_password()
    salt = auth.new_salt()
    store.add_credential(
        conn,
        username=username,
        password_hash=auth.hash_password(password, salt),
        salt=salt.hex(),
        created_at=created_at,
    )
    return password
