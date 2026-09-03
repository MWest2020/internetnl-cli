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

# A bare `https://host[:port]` origin — the shape `Origin` headers and
# `NETNL_DEMO_ALLOWED_ORIGIN` take (RFC 6454): scheme, host, optional port,
# nothing else (no path, no trailing slash, no credentials).
_ORIGIN_RE = re.compile(r"^https://[A-Za-z0-9.-]+(:[0-9]{1,5})?$")

# Carve-out for local development only, mirroring `NETNL_ALLOW_HTTP`'s
# existing role for the upstream endpoint: a plain-http `localhost` origin
# is otherwise indistinguishable from any other insecure origin, so it is
# accepted only under the same explicit opt-in.
_LOCALHOST_HTTP_ORIGIN_RE = re.compile(r"^http://localhost(:[0-9]{1,5})?$")

# var -> (attribute, default)
_DEMO_NUMERIC_DEFAULTS = {
    "NETNL_DEMO_MAX_PER_HOUR": ("max_per_hour", 6),
    "NETNL_DEMO_MAX_CONCURRENT": ("max_concurrent", 2),
    "NETNL_DEMO_PER_IP_PER_HOUR": ("per_ip_per_hour", 2),
    "NETNL_DEMO_DOMAIN_COOLDOWN_SECONDS": ("domain_cooldown_seconds", 900),
    "NETNL_DEMO_RETENTION_HOURS": ("retention_hours", 24),
    # Builder-review fix (M2): bounds anonymous *polling* (GET status/
    # results), a cost the per-IP submit cap above does nothing about — a
    # single accepted run can otherwise be polled an unbounded number of
    # times. 120/hour comfortably covers the page's own documented poll
    # cadence (docs/reference/demo-api.md: 5s backing off to 15s, giving up
    # around 10 minutes — worst case ~45 polls for one run) with headroom
    # for more than one run polled at once from the same address.
    "NETNL_DEMO_POLLS_PER_IP_PER_HOUR": ("polls_per_ip_per_hour", 120),
}

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
class DemoSettings:
    """Configuration for the opt-in, anonymous `/demo/*` route family (see
    `openspec/changes/add-demo-run/design.md`, pinned decisions D1-D15).
    `None` on `Settings.demo` means the family does not exist as far as any
    client can tell (`NETNL_DEMO_ENABLED` unset) — this dataclass is only
    ever constructed once that opt-in is on and its two required variables
    are present.
    """

    allowed_origin: str
    tenant: str
    max_per_hour: int
    max_concurrent: int
    per_ip_per_hour: int
    client_ip_header: str
    domain_cooldown_seconds: int
    retention_hours: int
    polls_per_ip_per_hour: int


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
    security_contact: str | None
    demo: DemoSettings | None


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

    allow_http = env.get("NETNL_ALLOW_HTTP") == "1"

    return Settings(
        upstream_endpoint=env["NETNL_UPSTREAM_ENDPOINT"],
        upstream_username=env["NETNL_UPSTREAM_USERNAME"],
        upstream_password=env["NETNL_UPSTREAM_PASSWORD"],
        db=env["NETNL_DB"],
        instance=instance,
        allow_http=allow_http,
        security_contact=_resolve_security_contact(env),
        demo=_load_demo(env, allow_http=allow_http),
        **kwargs,
    )


def _validate_demo_origin(origin: str, allow_http: bool) -> None:
    """`origin` (D6) must be a bare `https://host[:port]` — the shape an
    `Origin` header itself takes, and the exact value the facade will send
    back verbatim as `Access-Control-Allow-Origin`, so it must already be
    in that wire form, not merely "a URL". `NETNL_ALLOW_HTTP=1` (the same
    escape hatch `NETNL_UPSTREAM_ENDPOINT` already uses) additionally
    permits a bare `http://localhost[:port]` origin, for local development
    against a facade run without TLS in front of it.
    """
    if _ORIGIN_RE.fullmatch(origin):
        return
    if allow_http and _LOCALHOST_HTTP_ORIGIN_RE.fullmatch(origin):
        return
    raise SettingsError(
        "NETNL_DEMO_ALLOWED_ORIGIN must be a bare https://host[:port] origin "
        "(or, under NETNL_ALLOW_HTTP=1, http://localhost[:port]): got "
        f"{origin!r}"
    )


def _load_demo(env: Mapping[str, str], *, allow_http: bool) -> DemoSettings | None:
    """`None` unless `NETNL_DEMO_ENABLED=1` — every other `NETNL_DEMO_*`
    variable is ignored (not even read) when the family is off, so an
    operator who never opts in gets exactly the pre-existing behaviour
    regardless of what else happens to be set in the environment.
    """
    if env.get("NETNL_DEMO_ENABLED") != "1":
        return None

    allowed_origin = env.get("NETNL_DEMO_ALLOWED_ORIGIN")
    if not allowed_origin:
        raise SettingsError(
            "missing required environment variable: NETNL_DEMO_ALLOWED_ORIGIN"
        )
    _validate_demo_origin(allowed_origin, allow_http)

    tenant = env.get("NETNL_DEMO_TENANT")
    if not tenant:
        raise SettingsError("missing required environment variable: NETNL_DEMO_TENANT")

    demo_kwargs: dict = {}
    for var, (attr, default) in _DEMO_NUMERIC_DEFAULTS.items():
        demo_kwargs[attr] = _resolve_numeric(env, var, default)

    return DemoSettings(
        allowed_origin=allowed_origin,
        tenant=tenant,
        client_ip_header=env.get("NETNL_DEMO_CLIENT_IP_HEADER", "CF-Connecting-IP"),
        **demo_kwargs,
    )


def _resolve_security_contact(env: Mapping[str, str]) -> str | None:
    """Opt-in: unset, empty, or whitespace-only means "no security.txt
    route" (see api.py), not an empty/placeholder contact value — a
    whitespace-only value would otherwise slip through `or None` and
    produce an RFC-invalid `security.txt` with a blank `Contact` line.

    A CR or LF in the value is rejected outright rather than silently
    stripped: the value is written verbatim into the `security.txt`
    response body as `Contact: {value}`, so a newline in it could inject
    an extra line into that body.
    """
    raw = (env.get("NETNL_SECURITY_CONTACT") or "").strip()
    if not raw:
        return None
    if "\r" in raw or "\n" in raw:
        raise SettingsError(
            "NETNL_SECURITY_CONTACT must not contain CR/LF (it is written "
            f"into security.txt as a body line): got {raw!r}"
        )
    return raw
