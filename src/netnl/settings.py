"""Facade configuration: environment only, prefix `NETNL_`.

See design.md's configuration table. No default ever points at a host;
missing required variables refuse to start, naming the variable.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Mapping

from netnl.errors import SettingsError

# Header-safe: ASCII, no CR/LF, bounded length (used as X-Netnl-Instance).
_INSTANCE_RE = re.compile(r"^[A-Za-z0-9._ -]{1,64}$")

_REQUIRED = (
    "NETNL_UPSTREAM_ENDPOINT",
    "NETNL_UPSTREAM_USERNAME",
    "NETNL_UPSTREAM_PASSWORD",
    "NETNL_DB",
)

# var -> (attribute, default)
_NUMERIC_DEFAULTS = {
    "NETNL_RATE_LIMIT": ("rate_limit", 10),
    "NETNL_MAX_DOMAINS": ("max_domains", 500),
    "NETNL_MAX_CONCURRENT": ("max_concurrent", 2),
    "NETNL_RESULT_RETENTION_DAYS": ("result_retention_days", 7),
    "NETNL_AUDIT_RETENTION_DAYS": ("audit_retention_days", 90),
    "NETNL_METADATA_TTL": ("metadata_ttl", 3600),
    "NETNL_TIMEOUT": ("timeout", 30),
    # Round-1 fix (M6): per-domain length cap and a total request-body size
    # cap, so a tenant cannot push arbitrarily large or malformed strings
    # through to the private instance. 253 is the DNS hostname length limit.
    "NETNL_MAX_DOMAIN_LENGTH": ("max_domain_length", 253),
    "NETNL_MAX_BODY_BYTES": ("max_body_bytes", 1_048_576),
    # Round-2 fix (security-LOW): a `reserving` row whose upstream submit
    # never completed (crash, timeout) would otherwise pin a concurrency
    # slot forever — see design.md, "Audit" (reserving-prune) and
    # retention.py.
    "NETNL_RESERVING_GRACE_SECONDS": ("reserving_grace_seconds", 300),
}


@dataclass(frozen=True)
class Settings:
    upstream_endpoint: str
    upstream_username: str
    upstream_password: str
    db: str
    instance: str
    rate_limit: int
    max_domains: int
    max_concurrent: int
    result_retention_days: int
    audit_retention_days: int
    metadata_ttl: int
    timeout: int
    max_domain_length: int
    max_body_bytes: int
    reserving_grace_seconds: int
    allow_http: bool


def _resolve_numeric(env: Mapping[str, str], var: str, default: int) -> int:
    raw = env.get(var)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise SettingsError(f"{var} must be a number: got '{raw}'") from exc
    if value < 0:
        raise SettingsError(f"{var} must not be negative: got '{raw}'")
    if value != int(value):
        raise SettingsError(f"{var} must be an integer: got '{raw}'")
    return int(value)


def load(env: Mapping[str, str] | None = None) -> Settings:
    if env is None:
        env = os.environ

    for var in _REQUIRED:
        if not env.get(var):
            raise SettingsError(f"missing required environment variable: {var}")

    instance = env.get("NETNL_INSTANCE", "netnl")
    if not _INSTANCE_RE.fullmatch(instance):
        raise SettingsError(
            "NETNL_INSTANCE must match [A-Za-z0-9._ -]{1,64} (it is sent as a "
            f"header value): got {instance!r}"
        )

    kwargs: dict = {}
    for var, (attr, default) in _NUMERIC_DEFAULTS.items():
        kwargs[attr] = _resolve_numeric(env, var, default)

    return Settings(
        upstream_endpoint=env["NETNL_UPSTREAM_ENDPOINT"],
        upstream_username=env["NETNL_UPSTREAM_USERNAME"],
        upstream_password=env["NETNL_UPSTREAM_PASSWORD"],
        db=env["NETNL_DB"],
        instance=instance,
        allow_http=env.get("NETNL_ALLOW_HTTP") == "1",
        **kwargs,
    )
