"""Per-credential limits: size, domain shape, rate, concurrency.

Round-1 fix (B2/M6/M7): rate and concurrency are enforced atomically inside
a single `BEGIN IMMEDIATE` reservation transaction (`reserve_submission`),
so parallel submits from one credential cannot each read a stale count —
see design.md, "Concurrency and storage". Size and per-domain shape are
checked before any database or upstream work, since they need no state.
"""

from __future__ import annotations

import ipaddress
import re
import sqlite3
from datetime import datetime, timedelta

from netnl import store
from netnl.errors import NetnlHTTPError
from netnl.settings import Settings

# A conservative "plausible hostname" check (M6): dot-separated labels of
# letters/digits/hyphens, no leading/trailing hyphen per label, no
# whitespace or control characters anywhere. Deliberately not a full RFC
# 1035 validator — it exists to keep URLs, paths, CRLF and raw IPs-with-
# ports out of what the private upstream instance is asked to scan, not to
# be the last word on what is a "real" hostname.
_LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
_HOSTNAME_RE = re.compile(rf"^{_LABEL}(?:\.{_LABEL})*$")

# A label made up entirely of decimal or hex digits — the shape the last
# label of a dotted "IP written as digits" literal takes (`10.0.0.5`,
# `0x7f.0.0.1`, the trailing `1` of `0300.0250.0.1`, the short form
# `127.1`). No real public TLD is all-digits or a hex number, so a
# fully-numeric/hex last label is a reliable signal of "this is an address,
# not a hostname" (security-MEDIUM, design.md "No internal targets").
_NUMERIC_LABEL_RE = re.compile(r"^(?:[0-9]+|0[xX][0-9a-fA-F]+)$")

# Reserved / internal-use suffixes (design.md, "No internal targets
# (anti-SSRF)"): any name whose last label is one of these is a
# convention-internal name, never a public FQDN — matched case-insensitively
# per label. `localhost` itself is already caught by the single-label check
# above; this catches multi-label names *under* these suffixes
# (`foo.localhost`, `db.corp`, `x.lan`, `a.localdomain`, ...).
_RESERVED_SUFFIXES = frozenset(
    {"localhost", "local", "internal", "intranet", "corp", "home", "lan", "localdomain"}
)

# Well-known cloud-metadata hostnames, matched case-insensitively as the
# full (normalised) domain. Names ending in `.internal` are already caught
# by `_RESERVED_SUFFIXES` above; this covers the bare `metadata` form (a
# single label, also already caught) and the fully-qualified GCP name,
# listed explicitly per design.md.
_METADATA_HOSTNAMES = frozenset({"metadata", "metadata.google.internal"})


def check_size(domains: list[str], settings: Settings) -> None:
    if len(domains) > settings.max_domains:
        raise NetnlHTTPError(
            400,
            "bad-request",
            f"too many domains: {len(domains)} exceeds the limit of {settings.max_domains} "
            "per request",
        )


def _looks_like_hostname(domain: str, max_length: int) -> bool:
    if not domain or len(domain) > max_length:
        return False
    if any(ch.isspace() or ord(ch) < 0x20 or ord(ch) == 0x7F for ch in domain):
        return False
    return bool(_HOSTNAME_RE.fullmatch(domain))


def _is_internal_target(domain: str) -> bool:
    """Round-2 fix (security-MEDIUM, anti-SSRF): the facade fronts a scanner
    that resolves and connects to whatever target it is given, so only a
    public, multi-label FQDN token is accepted here — never an address. See
    design.md, "No internal targets (anti-SSRF)".

    Rejects:

    - anything `ipaddress` recognises directly: dotted IPv4 (`10.0.0.5`,
      `127.0.0.1`, `169.254.169.254`, ...) and every IPv6 form (`::1`, and
      any form that would even reach here — `_HOSTNAME_RE` already excludes
      `:`);
    - single-label names (`localhost` — no public name is a single label);
    - dotted decimal/octal/hex integer notations `ipaddress` does *not*
      parse on its own (`2130706433` has no dot so is already caught as
      single-label; `0300.0250.0.1`, `0x7f.0.0.1`, the short form `127.1`)
      — caught by the "TLD is all-digits/hex" check, since no real public
      TLD is;
    - names under a reserved or internal-use suffix (`.localhost`, `.local`,
      `.internal`, `.intranet`, `.corp`, `.home`, `.lan`, `.localdomain`),
      matched case-insensitively on the last label — a *named* internal
      target is just as much a pivot into the internal network as a literal
      address (round-3 fix, security re-check);
    - the well-known cloud-metadata hostnames (`metadata`,
      `metadata.google.internal`), matched case-insensitively.

    A trailing dot (`foo.localhost.`) is stripped before the suffix/label
    checks so it cannot be used to slip past them.

    DNS rebinding (a name that *resolves* to an internal address at request
    time) is explicitly out of scope: this facade never resolves the
    hostname it is handed — the upstream scanner does its own resolution
    and connects itself.
    """
    try:
        ipaddress.ip_address(domain)
    except ValueError:
        pass
    else:
        return True

    normalised = domain[:-1] if domain.endswith(".") else domain
    if normalised.lower() in _METADATA_HOSTNAMES:
        return True

    labels = normalised.split(".")
    if len(labels) < 2:
        return True
    if labels[-1].lower() in _RESERVED_SUFFIXES:
        return True
    return bool(_NUMERIC_LABEL_RE.fullmatch(labels[-1]))


def check_domains(domains: list[str], settings: Settings) -> None:
    """Round-1 fix (M6): per-domain length cap and a plausible-hostname
    check, so a tenant cannot push whitespace/control characters, URLs or
    megabyte-strings through to the private scanner. Round-2 fix
    (security-MEDIUM): reject IP-literal/internal-looking targets — see
    `_is_internal_target`.
    """
    for domain in domains:
        if not _looks_like_hostname(domain, settings.max_domain_length):
            raise NetnlHTTPError(
                400,
                "bad-request",
                f"invalid domain {domain!r}: expected a plausible hostname of at most "
                f"{settings.max_domain_length} characters, with no whitespace or control "
                "characters",
            )
        if _is_internal_target(domain):
            raise NetnlHTTPError(
                400,
                "bad-request",
                f"invalid domain {domain!r}: IP-address literals and single-label or "
                "numeric-TLD names are refused — the facade only accepts a public, "
                "multi-label hostname (anti-SSRF)",
            )


def refresh_stale_non_terminal(
    conn: sqlite3.Connection,
    credential_id: int,
    client,
    settings: Settings,
) -> None:
    """Refresh up to `max_concurrent` non-terminal rows against upstream.

    Kept out of the reservation transaction (design.md, "Limits": "a
    separate concern kept out of the write transaction") — a slow upstream
    status call must never hold the write lock that serialises other
    tenants' submits. A row still `reserving` (no `upstream_id` yet) has
    nothing to refresh and is skipped.
    """
    from netnl.api import call_upstream  # local import: avoids a cycle at module load

    rows = store.non_terminal_requests(conn, credential_id)
    for row in rows[: settings.max_concurrent]:
        if row["upstream_id"] is None:
            continue
        reply = call_upstream(client, client.status, row["upstream_id"])
        upstream_request = reply["request"]
        store.update_status(
            conn, row["facade_id"], upstream_request["status"], upstream_request.get("finished_date")
        )


def reserve_submission(
    conn: sqlite3.Connection,
    *,
    credential,
    settings: Settings,
    now: datetime,
    facade_id: str,
    request_type: str,
    domain_count: int,
    submitted_at: str,
) -> None:
    """Round-1 fix (B2/M7): reserve-then-submit, atomically.

    Inside a single `BEGIN IMMEDIATE` transaction: count submits in the
    rate window and non-terminal runs (a `reserving` row counts as
    in-progress) for this credential; reject with 429 if at or over either
    limit; otherwise insert the audit `submit` row and a `requests` row in
    state `reserving` (upstream untouched so far), then commit. The write
    lock serialises concurrent submits from the *same* credential so the
    counts this reads can never be stale — no parallel submit can slip
    through on a count read before another's row was written. Callers call
    upstream only after this returns successfully, then finalize the row
    with `store.finalize_reservation`.
    """
    cutoff = store.utcnow_iso(lambda: now - timedelta(hours=1))
    conn.execute("BEGIN IMMEDIATE")
    try:
        submit_count = store.count_submits_since(conn, credential["username"], cutoff)
        if submit_count >= settings.rate_limit:
            raise NetnlHTTPError(
                429,
                "rate-limited",
                f"rate limit of {settings.rate_limit} submissions per hour reached",
            )

        non_terminal = store.non_terminal_requests(conn, credential["id"])
        if len(non_terminal) >= settings.max_concurrent:
            raise NetnlHTTPError(
                429,
                "rate-limited",
                f"{len(non_terminal)} runs already in progress; the limit is "
                f"{settings.max_concurrent}",
            )

        store.insert_reserving_request(
            conn,
            facade_id=facade_id,
            credential_id=credential["id"],
            request_type=request_type,
            domain_count=domain_count,
            submitted_at=submitted_at,
        )
        store.record_audit(
            conn,
            at=submitted_at,
            credential=credential["username"],
            event="submit",
            facade_id=facade_id,
            domain_count=domain_count,
        )
    except Exception:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")
