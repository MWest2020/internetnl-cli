"""Bazibo web unlock codes, format v1 — pure functions, no I/O.

The format is shared with the Bazibo verifier (a separate codebase) and is
fixed; see openspec/changes/add-bazibo-unlock/design.md, "Code format":

    code    = "BZB1." + b64url(payload) + "." + b64url(sig)   (no padding)
    payload = 16 bytes:
      [0]      version = 1
      [1]      product = 1 (full version)
      [2..5]   issued day, uint32 big-endian, days since 1970-01-01 UTC
      [6..15]  10 random bytes (the code id)
    sig     = Ed25519 over b"bazibo-unlock-v1" || payload   (64 bytes)

Ed25519 signatures are deterministic: the same seed, issued day and code id
always give the same code. `netnl.bazibo_issuance` relies on that to re-send
the *same* code on a retry after a mail failure, storing only the code id
and the row's creation timestamp, never the code itself.
"""

from __future__ import annotations

import base64
import binascii
import secrets
import struct
from dataclasses import dataclass
from datetime import date, datetime, timezone

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

PREFIX = "BZB1."
DOMAIN = b"bazibo-unlock-v1"
VERSION = 1
PRODUCT_FULL = 1
CODE_ID_LEN = 10
PAYLOAD_LEN = 16
SIG_LEN = 64
# 5 + 22 + 1 + 86: the only length a well-formed v1 code can have. Checked
# before any decoding, so an oversized string is refused for free.
CODE_LEN = len(PREFIX) + 22 + 1 + 86

_EPOCH = date(1970, 1, 1)

LINK_PRIMARY = "https://bazibo.nl/?unlock="
LINK_PAGES = "https://mwest2020.github.io/bazibo/?unlock="


def b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(value: str) -> bytes | None:
    if not value or any(ch not in _B64URL_ALPHABET for ch in value):
        return None
    try:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (binascii.Error, ValueError):
        return None


_B64URL_ALPHABET = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
)


def public_key_from_seed(seed: bytes) -> bytes:
    key = Ed25519PrivateKey.from_private_bytes(seed)
    return key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)


def new_code_id() -> bytes:
    return secrets.token_bytes(CODE_ID_LEN)


def day_number(moment: datetime) -> int:
    """Days since 1970-01-01 in UTC for `moment` (must be tz-aware)."""
    return (moment.astimezone(timezone.utc).date() - _EPOCH).days


def day_from_iso(iso: str) -> int:
    """Issued day for a stored `utcnow_iso()` timestamp (always `+00:00`)."""
    return day_number(datetime.fromisoformat(iso))


def mint(seed: bytes, *, day: int, code_id: bytes) -> str:
    if len(code_id) != CODE_ID_LEN:
        raise ValueError("code id must be 10 bytes")
    payload = struct.pack(">BBI", VERSION, PRODUCT_FULL, day) + code_id
    sig = Ed25519PrivateKey.from_private_bytes(seed).sign(DOMAIN + payload)
    return PREFIX + b64url_encode(payload) + "." + b64url_encode(sig)


@dataclass(frozen=True)
class VerifiedCode:
    code_id: bytes
    day: int

    @property
    def code_id_hex(self) -> str:
        return self.code_id.hex()


def verify(public_key: bytes, code: object) -> VerifiedCode | None:
    """Returns the verified code's id and day, or `None` for anything that
    is not a well-formed v1 code with a valid signature under `public_key`.
    Never raises on hostile input.
    """
    if not isinstance(code, str) or len(code) != CODE_LEN or not code.startswith(PREFIX):
        return None
    body = code[len(PREFIX):]
    parts = body.split(".")
    if len(parts) != 2:
        return None
    payload = _b64url_decode(parts[0])
    sig = _b64url_decode(parts[1])
    if payload is None or sig is None or len(payload) != PAYLOAD_LEN or len(sig) != SIG_LEN:
        return None
    # Canonical encoding only: a second spelling of the same bytes (non-zero
    # trailing bits in the last base64 character) would otherwise be a
    # second "code" for one code id.
    if b64url_encode(payload) != parts[0] or b64url_encode(sig) != parts[1]:
        return None
    version, product, day = struct.unpack(">BBI", payload[:6])
    if version != VERSION or product != PRODUCT_FULL:
        return None
    try:
        Ed25519PublicKey.from_public_bytes(public_key).verify(sig, DOMAIN + payload)
    except InvalidSignature:
        return None
    return VerifiedCode(code_id=payload[6:], day=day)


def links(code: str) -> tuple[str, str]:
    """The two deep links for a code. A v1 code is base64url plus `.` and
    the `BZB1` prefix only, so it needs no URL-escaping."""
    return LINK_PRIMARY + code, LINK_PAGES + code
