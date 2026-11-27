"""Facade configuration: environment only, prefix `NETNL_`.

See design.md's configuration table. No default ever points at a host;
missing required variables refuse to start, naming the variable.
"""

from __future__ import annotations

import base64
import binascii
import math
from datetime import datetime, timezone
import os
import re
from dataclasses import dataclass, field
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
    `openspec/changes/archive/2026-09-08-add-demo-run/design.md`, pinned decisions D1-D15).
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
    bazibo: "BaziboSettings | None" = None


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
        supporter=(supporter := _load_supporter(env)),
        bazibo=_load_bazibo(env, supporter=supporter),
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


# --- Bazibo unlock codes (openspec/changes/add-bazibo-unlock) ---------------

_BAZIBO_DEFAULT_ORIGINS = ("https://bazibo.nl", "https://mwest2020.github.io")

# var -> (attribute, default)
_BAZIBO_NUMERIC_DEFAULTS = {
    "NETNL_BAZIBO_DEVICE_LIMIT": ("device_limit", 10),
    "NETNL_BAZIBO_AUTO_REVOKE_AT": ("auto_revoke_at", 100),
    "NETNL_BAZIBO_ACTIVATE_PER_MINUTE": ("activate_per_minute", 10),
    "NETNL_BMC_POLL_LOOKBACK_HOURS": ("poll_lookback_hours", 168),
    "NETNL_BMC_POLL_MAX_PAGES": ("poll_max_pages", 10),
}

# A salt shorter than this is an obviously-wrong value (a placeholder, a
# typo), not a strength target — the same floor `NETNL_BMC_WEBHOOK_SECRET`
# uses, for the same reason.
_MIN_DEVICE_SALT_LEN = 32


@dataclass(frozen=True)
class BaziboSettings:
    """Bazibo web unlock codes: issuance from a BMC Extra purchase and the
    device-count activation check (openspec/changes/add-bazibo-unlock).

    `None` on `Settings.bazibo` means `NETNL_BAZIBO_SIGNING_KEY` is unset:
    neither `POST /bazibo/activate` nor the Extra routing exists. With the
    key set but no Extra configured the subsystem is *dormant*: the
    activation route answers 503 `{"error": "off"}` and an Extra purchase
    is ignored like any other unknown event. `enabled` is the one switch:
    true with an Extra configured, or with `NETNL_BAZIBO_FROM_DONATIONS=1`
    (a plain donation of at least `NETNL_BAZIBO_DONATION_MIN` mints a code).
    """

    # The raw 32-byte Ed25519 seed. `repr=False`: a settings object that
    # ends up in a traceback or a debug print must never carry it.
    signing_seed: bytes = field(repr=False)
    public_key: bytes
    enabled: bool
    extra_id: str | None
    extra_title: str | None
    min_amount: Decimal
    device_salt: bytes | None = field(repr=False)
    device_limit: int
    auto_revoke_at: int
    activate_per_minute: int
    allowed_origins: tuple[str, ...]
    client_ip_header: str
    # API poller (`netnl-admin bazibo poll`, openspec/changes/
    # add-bazibo-support-created): off while the token is unset.
    bmc_api_token: str | None = field(default=None, repr=False)
    bmc_api_base: str = "https://developers.buymeacoffee.com/api/v1"
    poll_lookback_hours: int = 168
    poll_max_pages: int = 10
    source: str = "webhook"
    poll_not_before: "datetime | None" = None
    # A plain BMC donation ("support created", `donation.created`) of at
    # least `donation_min` also mints a code (Mark, 27 Nov 2026: BMC sends
    # this account no webhook for Extra purchases, only for donations).
    # `NETNL_BAZIBO_FROM_DONATIONS=1` switches it on; off by default.
    from_donations: bool = False
    donation_min: Decimal = Decimal("1")


def _decode_seed(raw: str) -> bytes:
    """`NETNL_BAZIBO_SIGNING_KEY`: base64 (standard or url-safe, padding
    optional) of exactly 32 raw bytes. The error never echoes the value.
    """
    value = raw.strip()
    padded = value + "=" * (-len(value) % 4)
    for decoder in (base64.b64decode, base64.urlsafe_b64decode):
        try:
            seed = decoder(padded)
        except (binascii.Error, ValueError):
            continue
        if len(seed) == 32:
            return seed
    raise SettingsError(
        "NETNL_BAZIBO_SIGNING_KEY must be the base64 encoding of a raw 32-byte "
        "Ed25519 seed (value not shown)"
    )


def _load_bazibo(env: Mapping[str, str], *, supporter: SupporterSettings | None) -> BaziboSettings | None:
    """Fail-fast on every half-configured state:

    - an Extra configured (`NETNL_BAZIBO_EXTRA_ID`/`_TITLE`) without the
      signing key, without the BMC bridge itself (`NETNL_BMC_WEBHOOK_SECRET`
      and its mail settings), or without `NETNL_BAZIBO_DEVICE_SALT`;
    - a signing key that is not a 32-byte seed, or that does not match
      `NETNL_BAZIBO_PUBLIC_KEY` when that cross-check is set.

    A key on its own (no Extra) is the documented dormant state, not an
    error: it lets the key be deployed before the Extra exists.
    """
    from netnl import bazibo  # local: keeps `cryptography` off the CLI import path

    raw_key = env.get("NETNL_BAZIBO_SIGNING_KEY") or None
    extra_id = (env.get("NETNL_BAZIBO_EXTRA_ID") or "").strip() or None
    extra_title = (env.get("NETNL_BAZIBO_EXTRA_TITLE") or "").strip() or None
    wants_extra = extra_id is not None or extra_title is not None
    raw_from_donations = env.get("NETNL_BAZIBO_FROM_DONATIONS", "0")
    if raw_from_donations not in ("0", "1"):
        raise SettingsError(
            f"NETNL_BAZIBO_FROM_DONATIONS must be '0' or '1': got {raw_from_donations!r}"
        )
    from_donations = raw_from_donations == "1"

    if raw_key is None:
        if wants_extra:
            raise SettingsError(
                "NETNL_BAZIBO_EXTRA_ID/NETNL_BAZIBO_EXTRA_TITLE is set but "
                "NETNL_BAZIBO_SIGNING_KEY is missing"
            )
        if from_donations:
            raise SettingsError(
                "NETNL_BAZIBO_FROM_DONATIONS=1 is set but NETNL_BAZIBO_SIGNING_KEY is missing"
            )
        return None

    seed = _decode_seed(raw_key)
    public_key = bazibo.public_key_from_seed(seed)

    expected_pub = (env.get("NETNL_BAZIBO_PUBLIC_KEY") or "").strip()
    if expected_pub:
        if bazibo.b64url_encode(public_key) != expected_pub.rstrip("="):
            raise SettingsError(
                "NETNL_BAZIBO_SIGNING_KEY does not match NETNL_BAZIBO_PUBLIC_KEY "
                "(wrong key in the secret?)"
            )

    if extra_id is not None and not extra_id.isdigit():
        raise SettingsError(f"NETNL_BAZIBO_EXTRA_ID must be a numeric BMC extra id: got {extra_id!r}")
    if extra_title is not None:
        _reject_crlf("NETNL_BAZIBO_EXTRA_TITLE", extra_title)

    raw_salt = env.get("NETNL_BAZIBO_DEVICE_SALT") or None
    device_salt = raw_salt.encode("utf-8") if raw_salt else None
    if device_salt is not None and len(raw_salt) < _MIN_DEVICE_SALT_LEN:
        raise SettingsError(
            f"NETNL_BAZIBO_DEVICE_SALT must be at least {_MIN_DEVICE_SALT_LEN} characters "
            f"(got {len(raw_salt)}); generate one with: openssl rand -hex 32"
        )

    if wants_extra or from_donations:
        if supporter is None:
            what = (
                "NETNL_BAZIBO_EXTRA_ID/NETNL_BAZIBO_EXTRA_TITLE" if wants_extra
                else "NETNL_BAZIBO_FROM_DONATIONS=1"
            )
            raise SettingsError(
                f"{what} requires the BMC bridge: "
                "missing required environment variable: NETNL_BMC_WEBHOOK_SECRET"
            )
        if device_salt is None:
            raise SettingsError(
                "missing required environment variable: NETNL_BAZIBO_DEVICE_SALT"
            )

    numeric: dict = {}
    for var, (attr, default) in _BAZIBO_NUMERIC_DEFAULTS.items():
        numeric[attr] = _resolve_numeric(env, var, default)
    if numeric["device_limit"] < 1:
        raise SettingsError("NETNL_BAZIBO_DEVICE_LIMIT must be at least 1")
    if numeric["auto_revoke_at"] <= numeric["device_limit"]:
        raise SettingsError(
            "NETNL_BAZIBO_AUTO_REVOKE_AT must be greater than NETNL_BAZIBO_DEVICE_LIMIT"
        )
    if numeric["activate_per_minute"] < 1:
        raise SettingsError("NETNL_BAZIBO_ACTIVATE_PER_MINUTE must be at least 1")
    if numeric["poll_max_pages"] < 1:
        raise SettingsError("NETNL_BMC_POLL_MAX_PAGES must be at least 1")

    token = (env.get("NETNL_BMC_API_TOKEN") or "").strip() or None
    if token is not None and (any(ch.isspace() for ch in token) or not token.isprintable()):
        raise SettingsError(
            "NETNL_BMC_API_TOKEN must be a single printable token without whitespace "
            "(value not shown)"
        )
    api_base = env.get("NETNL_BMC_API_BASE", "https://developers.buymeacoffee.com/api/v1").rstrip("/")
    if not api_base.startswith("https://") and not (
        env.get("NETNL_ALLOW_HTTP") == "1" and api_base.startswith("http://")
    ):
        raise SettingsError("NETNL_BMC_API_BASE must be an https:// URL (value not shown)")
    if any(ch in api_base for ch in "\r\n@") or not api_base.isprintable():
        raise SettingsError(
            "NETNL_BMC_API_BASE must be a plain https://host/path URL, no credentials "
            "(value not shown)"
        )

    # Which channel mints Bazibo codes for Extra purchases. The webhook (and
    # a hand issue) key on BMC's transaction id, the API poller on the API's
    # purchase id; they cannot recognise each other's purchases, so exactly
    # one of them is ever active (design.md D11).
    source = env.get("NETNL_BAZIBO_SOURCE", "webhook")
    if source not in ("webhook", "poll"):
        raise SettingsError(f"NETNL_BAZIBO_SOURCE must be 'webhook' or 'poll': got {source!r}")
    poll_not_before = None
    if source == "poll":
        if token is None:
            raise SettingsError(
                "NETNL_BAZIBO_SOURCE=poll requires NETNL_BMC_API_TOKEN"
            )
        raw_nb = env.get("NETNL_BMC_POLL_NOT_BEFORE") or ""
        try:
            poll_not_before = datetime.fromisoformat(raw_nb)
        except ValueError:
            raise SettingsError(
                "NETNL_BAZIBO_SOURCE=poll requires NETNL_BMC_POLL_NOT_BEFORE, an ISO "
                "timestamp (UTC unless it says otherwise) before which purchases are "
                "never mailed by the poller (no backfill of purchases another channel "
                f"already served): got {raw_nb!r}"
            ) from None
        if poll_not_before.tzinfo is None:
            poll_not_before = poll_not_before.replace(tzinfo=timezone.utc)

    raw_origins = env.get("NETNL_BAZIBO_ALLOWED_ORIGINS")
    if raw_origins is None:
        origins = _BAZIBO_DEFAULT_ORIGINS
    else:
        origins = tuple(o.strip() for o in raw_origins.split(",") if o.strip())
        if not origins:
            raise SettingsError("NETNL_BAZIBO_ALLOWED_ORIGINS must name at least one origin")
        for origin in origins:
            if not _ORIGIN_RE.fullmatch(origin):
                raise SettingsError(
                    "NETNL_BAZIBO_ALLOWED_ORIGINS entries must be bare https://host[:port] "
                    f"origins: got {origin!r}"
                )

    return BaziboSettings(
        signing_seed=seed,
        public_key=public_key,
        enabled=wants_extra or from_donations,
        extra_id=extra_id,
        extra_title=extra_title,
        min_amount=_resolve_decimal(env, "NETNL_BAZIBO_MIN_AMOUNT", Decimal("0")),
        device_salt=device_salt,
        allowed_origins=origins,
        client_ip_header=env.get("NETNL_BAZIBO_CLIENT_IP_HEADER", "CF-Connecting-IP"),
        bmc_api_token=token,
        bmc_api_base=api_base,
        source=source,
        poll_not_before=poll_not_before,
        from_donations=from_donations,
        donation_min=_resolve_decimal(env, "NETNL_BAZIBO_DONATION_MIN", Decimal("1")),
        **numeric,
    )
