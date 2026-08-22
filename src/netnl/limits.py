"""Per-credential limits: size, rate, concurrency.

Checked in this order in `POST /requests`, all three before any upstream
call: size, then rate, then concurrency. A rejection at size or rate never
touches upstream at all; concurrency may issue up to `NETNL_MAX_CONCURRENT`
status refresh calls before deciding.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

from netnl import store
from netnl.errors import NetnlHTTPError
from netnl.settings import Settings


def check_size(domains: list[str], settings: Settings) -> None:
    if len(domains) > settings.max_domains:
        raise NetnlHTTPError(
            400,
            "bad-request",
            f"too many domains: {len(domains)} exceeds the limit of {settings.max_domains} "
            "per request",
        )


def check_rate(
    conn: sqlite3.Connection,
    credential: str,
    settings: Settings,
    now: datetime,
) -> None:
    cutoff = store.utcnow_iso(lambda: now - timedelta(hours=1))
    count = store.count_submits_since(conn, credential, cutoff)
    if count >= settings.rate_limit:
        raise NetnlHTTPError(
            429,
            "rate-limited",
            f"rate limit of {settings.rate_limit} submissions per hour reached",
        )


def check_concurrency(
    conn: sqlite3.Connection,
    credential_id: int,
    client,
    settings: Settings,
) -> None:
    from netnl.api import call_upstream  # local import: avoids a cycle at module load

    rows = store.non_terminal_requests(conn, credential_id)
    for row in rows[: settings.max_concurrent]:
        reply = call_upstream(client, client.status, row["upstream_id"])
        upstream_request = reply["request"]
        store.update_status(
            conn, row["facade_id"], upstream_request["status"], upstream_request.get("finished_date")
        )

    rows = store.non_terminal_requests(conn, credential_id)
    if len(rows) >= settings.max_concurrent:
        raise NetnlHTTPError(
            429,
            "rate-limited",
            f"{len(rows)} runs already in progress; the limit is {settings.max_concurrent}",
        )
