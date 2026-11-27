"""`POST /bazibo/activate`: the device-count check for Bazibo unlock codes
(openspec/changes/add-bazibo-unlock, "Activation").

Bazibo's web build calls this once per browser with the unlock code and a
random 128-bit device id the browser generated and keeps locally. The
facade verifies the code's signature first, refuses revoked code ids, and
counts distinct devices per code: up to `NETNL_BAZIBO_DEVICE_LIMIT` (10)
devices succeed, and keep succeeding; the next one gets 409 `limit`; once
`NETNL_BAZIBO_AUTO_REVOKE_AT` (100) distinct devices have tried one code,
the code is revoked for every device.

What is kept (`bazibo_device`): the code id, an HMAC-SHA256 of the device
id under the server secret `NETNL_BAZIBO_DEVICE_SALT`, and the first- and
last-seen *dates*. Nothing else: no IP, no user agent, no email. The
per-IP rate limit lives in process memory only and is never persisted.
Nothing about this route's input (code, device id, IP) is ever logged;
only a code id, and only when it crosses the device limit or is revoked.

Replies are flat JSON for a browser, not the batch-API error shape:
`{"status": "ok"}` or `{"error": "<label>"}` with label one of `off`,
`origin`, `rate`, `invalid`, `revoked`, `limit`.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import sqlite3
import threading
import time
from datetime import datetime

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from starlette.concurrency import run_in_threadpool

from netnl import bazibo, store
from netnl.demo import _client_ip_key
from netnl.settings import BaziboSettings, Settings

_logger = logging.getLogger("netnl.bazibo")

PATH = "/bazibo/activate"

# `{"code": <114 chars>, "device": <36 chars>}` is ~170 bytes; 1 KiB leaves
# room for whitespace and nothing else.
_MAX_BODY_BYTES = 1024

_DEVICE_HEX_RE = re.compile(r"^[0-9a-fA-F]{32}$")
_DEVICE_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


def _reply(status: int, body: dict, headers: dict | None = None) -> JSONResponse:
    return JSONResponse(body, status_code=status, headers=headers)


def _error(status: int, label: str, headers: dict | None = None) -> JSONResponse:
    return _reply(status, {"error": label}, headers)


# --- per-IP rate limit, memory only -----------------------------------------

_MAX_BUCKETS = 4096
_OVERFLOW_KEY = "<overflow>"
_WINDOW_SECONDS = 60.0

_rate_lock = threading.Lock()
_rate: dict[str, list[float]] = {}


def reset_state() -> None:
    with _rate_lock:
        _rate.clear()


def _allow(key: str, per_minute: int, now: float) -> bool:
    """Sliding one-minute window; sweep, check and record under one lock.
    Bounded to `_MAX_BUCKETS` distinct keys (new keys beyond that share one
    overflow bucket), so a flood of spoofed addresses cannot grow it."""
    cutoff = now - _WINDOW_SECONDS
    with _rate_lock:
        for k in list(_rate):
            kept = [t for t in _rate[k] if t > cutoff]
            if kept:
                _rate[k] = kept
            else:
                del _rate[k]
        if key not in _rate and len(_rate) >= _MAX_BUCKETS:
            key = _OVERFLOW_KEY
        bucket = _rate.setdefault(key, [])
        if len(bucket) >= per_minute:
            return False
        bucket.append(now)
        return True


# --- strict input ---------------------------------------------------------


class _Invalid(Exception):
    pass


def _no_duplicates(pairs):
    keys = [k for k, _ in pairs]
    if len(keys) != len(set(keys)):
        raise _Invalid()
    return dict(pairs)


def _no_constants(_name):
    raise _Invalid()


def parse_body(raw: bytes) -> tuple[str, bytes]:
    """Exactly `{"code": str, "device": str}`: UTF-8 JSON object, no
    duplicate keys, no extra keys, no NaN/Infinity. `device` is 128 bits
    as 32 hex characters (a dashed UUID is accepted too). Returns the code
    string and the 16 device bytes; raises `_Invalid` for anything else.
    """
    try:
        payload = json.loads(
            raw.decode("utf-8"), object_pairs_hook=_no_duplicates, parse_constant=_no_constants
        )
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise _Invalid() from exc
    if not isinstance(payload, dict) or set(payload) != {"code", "device"}:
        raise _Invalid()
    code, device = payload["code"], payload["device"]
    if not isinstance(code, str) or not isinstance(device, str):
        raise _Invalid()
    if _DEVICE_UUID_RE.fullmatch(device):
        device = device.replace("-", "")
    if not _DEVICE_HEX_RE.fullmatch(device):
        raise _Invalid()
    return code, bytes.fromhex(device)


def device_hash(salt: bytes, device: bytes) -> str:
    return hmac.new(salt, device, hashlib.sha256).hexdigest()


# --- the counting step ----------------------------------------------------


def record_activation(
    conn: sqlite3.Connection, bz: BaziboSettings, code_id: str, device: bytes, now: datetime
) -> str:
    """One `BEGIN IMMEDIATE` step. Returns `"ok"`, `"limit"` or `"revoked"`."""
    assert bz.device_salt is not None
    today = now.date().isoformat()
    hashed = device_hash(bz.device_salt, device)

    conn.execute("BEGIN IMMEDIATE")
    try:
        if store.bazibo_revocation(conn, code_id) is not None:
            conn.execute("COMMIT")
            return "revoked"

        row = store.find_bazibo_device(conn, code_id, hashed)
        if row is not None:
            if row["last_seen"] != today:
                store.touch_bazibo_device(conn, row["id"], today)
            rank = store.bazibo_device_rank(conn, code_id, row["id"])
        else:
            seen = store.count_bazibo_devices(conn, code_id)
            store.insert_bazibo_device(conn, code_id, hashed, today)
            if seen + 1 >= bz.auto_revoke_at:
                store.revoke_bazibo_code(
                    conn, code_id, reason="auto", at=store.utcnow_iso(lambda: now)
                )
                store.record_audit(
                    conn, at=store.utcnow_iso(lambda: now), credential=None,
                    event="bazibo-auto-revoke", detail=f"code_id={code_id}",
                )
                conn.execute("COMMIT")
                _logger.warning("event=bazibo-auto-revoked code_id=%s devices=%d", code_id, seen + 1)
                return "revoked"
            rank = seen + 1
            if rank == bz.device_limit + 1:
                _logger.warning("event=bazibo-device-limit code_id=%s", code_id)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return "ok" if rank <= bz.device_limit else "limit"


def _activate_blocking(settings: Settings, code_id: str, device: bytes, now: datetime) -> str:
    conn = store.connect(settings.db)
    try:
        assert settings.bazibo is not None
        return record_activation(conn, settings.bazibo, code_id, device, now)
    finally:
        conn.close()


# --- CORS -----------------------------------------------------------------


def response_headers(request: Request, settings: Settings) -> dict[str, str]:
    """Added to every `/bazibo/*` reply (by the middleware in `api.py`, and
    by its 500 handler). `Access-Control-Allow-Origin` echoes the request's
    Origin only when it is exactly one of the allowed origins; never with
    `Access-Control-Allow-Credentials`."""
    bz = settings.bazibo
    if bz is None or not request.url.path.startswith("/bazibo/"):
        return {}
    headers = {"Cache-Control": "no-store", "Vary": "Origin"}
    origin = request.headers.get("origin")
    if origin is not None and origin in bz.allowed_origins:
        headers["Access-Control-Allow-Origin"] = origin
    return headers


def register_routes(app: FastAPI, settings: Settings) -> None:
    bz = settings.bazibo
    if bz is None:
        return

    @app.options(PATH)
    def activate_preflight(request: Request) -> Response:
        response = Response(status_code=204)
        if request.headers.get("origin") in bz.allowed_origins:
            response.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
            response.headers["Access-Control-Allow-Headers"] = "content-type"
            response.headers["Access-Control-Max-Age"] = "600"
        return response

    @app.post(PATH)
    async def activate(request: Request) -> Response:
        origin = request.headers.get("origin")
        if origin is not None and origin not in bz.allowed_origins:
            return _error(403, "origin")
        if not bz.enabled:
            return _error(503, "off")

        key = _client_ip_key(request, bz.client_ip_header)
        if not _allow(key, bz.activate_per_minute, time.monotonic()):
            return _error(429, "rate", {"Retry-After": "60"})

        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > _MAX_BODY_BYTES:
                return _error(400, "invalid")
        try:
            code, device = parse_body(bytes(body))
        except _Invalid:
            return _error(400, "invalid")

        # Signature first: nothing below (database included) is reached
        # for a code that does not verify.
        verified = bazibo.verify(bz.public_key, code)
        if verified is None:
            return _error(400, "invalid")

        outcome = await run_in_threadpool(
            _activate_blocking, settings, verified.code_id_hex, device, request.app.state.now()
        )
        if outcome == "ok":
            return _reply(200, {"status": "ok"})
        if outcome == "limit":
            return _error(409, "limit")
        return _error(403, "revoked")
