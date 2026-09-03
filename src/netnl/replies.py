"""v2-shaped reply bodies and the fixed error-label/status pins.

`API_VERSION` matches `tests/fakes.py`'s sample payloads (vendored from the
v2.6.0 OpenAPI document).
"""

from __future__ import annotations

API_VERSION = "2.6.0"

# Sent on every reply — success and error — so a client can always tell it
# is talking to an independent facade, not internet.nl itself.
NOTICE = "independent instance; not internet.nl and not Platform Internetstandaarden"

# label -> default HTTP status, kept here as the single source of truth.
LABEL_STATUS = {
    "bad-request": 400,
    "unauthorised": 401,
    "unknown-request": 404,
    "rate-limited": 429,
    "not-implemented": 501,
    "upstream-unreachable": 502,
    "upstream-error": 502,
    "server-error": 500,
    "overloaded": 503,
    # openspec/changes/add-demo-run, D13: the anonymous demo family's own
    # labels. "overloaded" above already exists from the facade-hardening
    # round and is deliberately not duplicated for the demo path.
    "demo-unavailable": 503,
    "forbidden-origin": 403,
}


def error_body(label: str, msg: str) -> dict:
    return {"api_version": API_VERSION, "error": {"label": label, "msg": msg}}
