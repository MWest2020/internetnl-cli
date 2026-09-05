"""Facade configuration: environment only, prefix `NETNL_`.

See design.md's configuration table. No default ever points at a host;
missing required variables refuse to start, naming the variable.
"""

from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Mapping

from netnl import bmc
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


_SUPPORTER_NUMERIC_DEFAULTS = {
    "NETNL_BMC_MAX_BODY_BYTES": ("max_body_bytes", 65536),
    "NETNL_SUPPORTER_MAX_PER_HOUR": ("max_per_hour", 20),
    "NETNL_SUPPORTER_MAX_ATTEMPTS": ("max_attempts", 3),
    "NETNL_SMTP_PORT": ("smtp_port", 587),
    "NETNL_SMTP_TIMEOUT": ("smtp_timeout", 15),
}

# Security review fix (N1): `NETNL_SMTP_TIMEOUT` bounds a single socket
# operation (connect, or one read/write), not an entire send — a full
# credential-mail send is several sequential round trips (connect, EHLO,
# STARTTLS, EHLO again, AUTH LOGIN's exchange, MAIL FROM, RCPT TO, DATA +
# body, QUIT), each individually subject to that timeout. Measured: a
# genuinely successful send took ~5.3x the configured timeout end to end
# across those ~8 round trips. An earlier version of the pending-lease
# derivation (`netnl.supporter._pending_lease_seconds`) used
# `smtp_timeout + 30`, which could be *shorter* than a real, still
# in-flight send — exactly the race B1 exists to close. `8x` leaves
# headroom above the measured 5.3x; the margin absorbs the DB round-trips
# either side of the SMTP call itself. See `NETNL_SUPPORTER_LEASE_SECONDS`
# below for an explicit override, for a relay whose own behaviour differs.
_LEASE_TIMEOUT_MULTIPLIER = 8
_LEASE_MARGIN_SECONDS = 30

_SMTP_MODES = {"starttls", "ssl", "plaintext"}


@dataclass(frozen=True)
class SupporterSettings:
    """Configuration for the opt-in `POST /webhooks/bmc` bridge (openspec/
    changes/add-supporter-issuance, pinned decisions D1-D5). `None` on
    `Settings.supporter` means the route does not exist as far as any
    client can tell (`NETNL_BMC_WEBHOOK_SECRET` unset) — mirrors
    `DemoSettings`' own "opt-in, not even read" shape; this dataclass is
    only ever constructed once that opt-in is on and every variable
    required alongside it is present.
    """

    webhook_secret: str
    signature_header: str
    max_body_bytes: int
    accept_test_mode: bool
    min_amount: Decimal
    currency: str | None
    max_per_hour: int
    max_attempts: int
    username_prefix: str
    public_endpoint: str
    smtp_host: str
    smtp_port: int
    smtp_username: str | None
    smtp_password: str | None
    smtp_from: str
    smtp_mode: str
    smtp_timeout: int
    notify: str | None
    pending_lease_seconds: int


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
    supporter: SupporterSettings | None


def _resolve_numeric(env: Mapping[str, str], var: str, default: int) -> int:
    raw = env.get(var)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise SettingsError(f"{var} must be a number: got '{raw}'") from exc
    # Security review fix (N3): `float("nan")`/`float("inf")`/`float(
    # "1e400")` (float overflow) all parse without raising above — `int()`
    # on a non-finite float is what actually raises (a raw `ValueError`
    # for NaN, `OverflowError` for +/-inf), neither of which is
    # `SettingsError`. Checked explicitly, before ever calling `int()`, so
    # this always fails closed with a clean, variable-naming error.
    if not math.isfinite(value):
        raise SettingsError(f"{var} must be a finite number: got '{raw}'")
    if value < 0:
        raise SettingsError(f"{var} must not be negative: got '{raw}'")
    if value != int(value):
        raise SettingsError(f"{var} must be an integer: got '{raw}'")
    return int(value)


def _resolve_decimal(env: Mapping[str, str], var: str, default: Decimal) -> Decimal:
    """Round-1 (add-supporter-issuance): parsed with `decimal.Decimal`
    directly from the raw string, never via `float` first — a monetary
    threshold compared against a delivery's own decimal amount must not
    pick up binary-float rounding error.
    """
    raw = env.get(var)
    if raw is None:
        return default
    try:
        value = Decimal(raw)
    except InvalidOperation as exc:
        raise SettingsError(f"{var} must be a decimal number: got '{raw}'") from exc
    # Security review fix: `Decimal("NaN")`/`Decimal("Infinity")` parse
    # without raising `InvalidOperation` above, and comparing a NaN/sNaN
    # `Decimal` with `<` itself raises `InvalidOperation` (not "quietly
    # False") in the default context — checked *before* the comparison
    # below so that never escapes as anything other than a clean,
    # variable-naming `SettingsError`.
    if not value.is_finite():
        raise SettingsError(f"{var} must be a finite decimal number: got '{raw}'")
    if value < 0:
        raise SettingsError(f"{var} must not be negative: got '{raw}'")
    return value


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
        supporter=_load_supporter(env),
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


def _reject_crlf(var: str, value: str) -> None:
    if "\r" in value or "\n" in value:
        raise SettingsError(f"{var} must not contain CR/LF: got {value!r}")


def _load_supporter(env: Mapping[str, str]) -> SupporterSettings | None:
    """`None` unless `NETNL_BMC_WEBHOOK_SECRET` is set — every other
    `NETNL_BMC_*`/`NETNL_SUPPORTER_*`/`NETNL_SMTP_*`/`NETNL_PUBLIC_ENDPOINT`
    variable is ignored (not even read) when the bridge is off, mirroring
    `_load_demo`'s own "opt-in, not read at all" shape (openspec/changes/
    add-supporter-issuance, D5).

    `NETNL_SMTP_USERNAME`/`NETNL_SMTP_PASSWORD` are deliberately optional:
    some relays authenticate by network origin or client certificate, not a
    username/password pair. `netnl.mail.smtp_sender` only attempts
    `login()` when a username is configured — see that module.
    """
    secret = env.get("NETNL_BMC_WEBHOOK_SECRET")
    if not secret:
        return None
    # Security review fix: the shared secret is the *entire* gate on this
    # route (see docs/how-to/supporter-webhook.md, "Security notes") — a
    # short value is brute-forceable against the HMAC comparison in a way
    # a `openssl rand -hex 32` (256 bits) value, the documented generation
    # command, never is. 32 characters is a floor, not a strength target;
    # it exists to catch an obviously-too-short accidental value (a typo,
    # a placeholder left in place) at startup rather than silently running
    # with one.
    if len(secret) < 32:
        raise SettingsError(
            f"NETNL_BMC_WEBHOOK_SECRET must be at least 32 characters (got {len(secret)}); "
            "generate one with: openssl rand -hex 32"
        )

    signature_header = env.get("NETNL_BMC_SIGNATURE_HEADER", "X-Signature-Sha256")
    accept_test_mode = env.get("NETNL_BMC_ACCEPT_TEST_MODE") == "1"
    min_amount = _resolve_decimal(env, "NETNL_SUPPORTER_MIN_AMOUNT", Decimal("2"))
    raw_currency = env.get("NETNL_SUPPORTER_CURRENCY")
    currency = raw_currency.upper() if raw_currency else None
    username_prefix = env.get("NETNL_SUPPORTER_USERNAME_PREFIX", "supporter-")

    public_endpoint = env.get("NETNL_PUBLIC_ENDPOINT")
    if not public_endpoint:
        raise SettingsError("missing required environment variable: NETNL_PUBLIC_ENDPOINT")
    _reject_crlf("NETNL_PUBLIC_ENDPOINT", public_endpoint)

    smtp_host = env.get("NETNL_SMTP_HOST")
    if not smtp_host:
        raise SettingsError("missing required environment variable: NETNL_SMTP_HOST")

    smtp_from = env.get("NETNL_SMTP_FROM")
    if not smtp_from:
        raise SettingsError("missing required environment variable: NETNL_SMTP_FROM")
    _reject_crlf("NETNL_SMTP_FROM", smtp_from)

    smtp_mode = env.get("NETNL_SMTP_MODE", "starttls")
    if smtp_mode not in _SMTP_MODES:
        raise SettingsError(
            f"NETNL_SMTP_MODE must be one of {sorted(_SMTP_MODES)}: got {smtp_mode!r}"
        )
    if smtp_mode == "plaintext" and env.get("NETNL_SMTP_ALLOW_PLAINTEXT") != "1":
        raise SettingsError(
            "NETNL_SMTP_MODE=plaintext requires NETNL_SMTP_ALLOW_PLAINTEXT=1 "
            "(the same explicit opt-in NETNL_ALLOW_HTTP already uses for an "
            "insecure upstream hop) — in plaintext mode, the SMTP relay "
            "password (NETNL_SMTP_PASSWORD, if set) travels the connection "
            "unencrypted, same as the credential mail body itself"
        )

    smtp_username = env.get("NETNL_SMTP_USERNAME") or None
    smtp_password = env.get("NETNL_SMTP_PASSWORD") or None

    # Security review fix (M1): validated the same way `NETNL_SMTP_FROM`
    # already is (CR/LF rejected — this address is placed in a mail
    # header, `netnl.mail.build_notify_mail`'s `To:`) plus `bmc.
    # valid_recipient`'s own conservative address shape — the same guard
    # used for the untrusted, donor-supplied address, applied here to an
    # operator-configured one so a malformed value fails at startup, not
    # silently on the first successful delivery.
    raw_notify = env.get("NETNL_SUPPORTER_NOTIFY") or None
    notify: str | None = None
    if raw_notify is not None:
        _reject_crlf("NETNL_SUPPORTER_NOTIFY", raw_notify)
        if not bmc.valid_recipient(raw_notify):
            raise SettingsError(
                f"NETNL_SUPPORTER_NOTIFY is not a usable mail address: got {raw_notify!r}"
            )
        notify = raw_notify

    supporter_kwargs: dict = {}
    for var, (attr, default) in _SUPPORTER_NUMERIC_DEFAULTS.items():
        supporter_kwargs[attr] = _resolve_numeric(env, var, default)

    # Security review fix (N1): the pending-lease default is derived from
    # `smtp_timeout`, not a bare constant — see `_LEASE_TIMEOUT_MULTIPLIER`
    # for the measured basis — with an explicit `NETNL_SUPPORTER_
    # LEASE_SECONDS` override for an operator whose relay's own behaviour
    # differs. Resolved via the same `_resolve_numeric` (finite,
    # non-negative, integer) as every other numeric setting.
    derived_lease = (
        supporter_kwargs["smtp_timeout"] * _LEASE_TIMEOUT_MULTIPLIER + _LEASE_MARGIN_SECONDS
    )
    supporter_kwargs["pending_lease_seconds"] = _resolve_numeric(
        env, "NETNL_SUPPORTER_LEASE_SECONDS", derived_lease
    )

    return SupporterSettings(
        webhook_secret=secret,
        signature_header=signature_header,
        accept_test_mode=accept_test_mode,
        min_amount=min_amount,
        currency=currency,
        username_prefix=username_prefix,
        public_endpoint=public_endpoint,
        smtp_host=smtp_host,
        smtp_username=smtp_username,
        smtp_password=smtp_password,
        smtp_from=smtp_from,
        smtp_mode=smtp_mode,
        notify=notify,
        **supporter_kwargs,
    )
