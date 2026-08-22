"""HTTP Basic authentication for the facade's own tenants.

Passwords are hashed with stdlib `hashlib.scrypt`, per-credential salt,
compared with `hmac.compare_digest`. An unknown username still costs one
scrypt computation (against a fixed dummy salt) before being rejected, so
"unknown user" and "wrong password" take the same time.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import secrets

from fastapi import Request

from netnl import store
from netnl.errors import NetnlHTTPError

_SCRYPT_PARAMS = {"n": 2**14, "r": 8, "p": 1, "dklen": 32}
_DUMMY_SALT = bytes(16)


def new_salt() -> bytes:
    return secrets.token_bytes(16)


def hash_password(password: str, salt: bytes) -> str:
    return hashlib.scrypt(password.encode("utf-8"), salt=salt, **_SCRYPT_PARAMS).hex()


def verify(stored_hash: str, salt: bytes, password: str) -> bool:
    candidate = hash_password(password, salt)
    return hmac.compare_digest(candidate, stored_hash)


def new_password() -> str:
    return secrets.token_urlsafe(24)


def _unauthorized() -> NetnlHTTPError:
    return NetnlHTTPError(401, "unauthorised", "invalid credentials")


def _parse_basic_auth(header: str | None) -> tuple[str, str] | None:
    if not header or not header.startswith("Basic "):
        return None
    token = header[len("Basic "):]
    try:
        decoded = base64.b64decode(token, validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return None
    if ":" not in decoded:
        return None
    username, _, password = decoded.partition(":")
    return username, password


def authenticate(request: Request):
    """FastAPI dependency: validate `Authorization: Basic`, return the
    `credentials` row on success.

    Deliberately does not use FastAPI's `HTTPBasic` — that raises its own
    non-v2-shaped error. Every failure path raises the same
    `NetnlHTTPError(401, "unauthorised", ...)`, whose handler adds the
    `WWW-Authenticate` header.
    """
    conn = request.app.state.conn
    parsed = _parse_basic_auth(request.headers.get("authorization"))
    if parsed is None:
        hash_password("", _DUMMY_SALT)  # keep "no header" as slow as a real check
        raise _unauthorized()

    username, password = parsed
    credential = store.find_credential(conn, username)
    if credential is None:
        hash_password(password, _DUMMY_SALT)
        raise _unauthorized()

    salt = bytes.fromhex(credential["salt"])
    ok = verify(credential["password_hash"], salt, password)
    if not ok or credential["revoked_at"] is not None:
        raise _unauthorized()

    return credential
