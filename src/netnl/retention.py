"""Retention: prune expired requests and old audit records.

Runs in a single `BEGIN IMMEDIATE` transaction. The append-only trigger on
`audit` is dropped only for the duration of the audit-pruning delete and
recreated before commit — this is the one place in the package allowed to
touch `audit`'s UPDATE/DELETE path. A failure anywhere in the transaction
rolls back (SQLite's DDL is transactional, so a rolled-back `DROP TRIGGER`
is undone too) and the trigger is recreated defensively regardless.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

from netnl import store
from netnl.settings import Settings

_RECREATE_AUDIT_NO_DELETE = (
    "CREATE TRIGGER IF NOT EXISTS audit_no_delete "
    "BEFORE DELETE ON audit "
    "BEGIN SELECT RAISE(ABORT, 'audit is append-only'); END"
)


def prune(conn: sqlite3.Connection, settings: Settings, now: datetime) -> dict:
    result_cutoff = store.utcnow_iso(
        lambda: now - timedelta(days=settings.result_retention_days)
    )
    # Round-2 fix (security-LOW, pinned): a row still `reserving` (no
    # `upstream_id` yet) whose upstream submit never completed — the
    # process crashed, or the upstream call hung past every retry — would
    # otherwise count toward `max_concurrent` forever, a self-inflicted DoS
    # on that credential. A short grace lets an in-flight submit finish
    # normally; only a *stranded* reservation older than the grace is
    # cleared. See design.md, "Audit" (reserving-prune).
    reserving_cutoff = store.utcnow_iso(
        lambda: now - timedelta(seconds=settings.reserving_grace_seconds)
    )
    audit_cutoff = store.utcnow_iso(
        lambda: now - timedelta(days=settings.audit_retention_days)
    )

    conn.execute("BEGIN IMMEDIATE")
    try:
        cur = conn.execute("DELETE FROM requests WHERE submitted_at < ?", (result_cutoff,))
        requests_deleted = cur.rowcount if cur.rowcount >= 0 else 0

        cur = conn.execute(
            "DELETE FROM requests WHERE upstream_id IS NULL AND submitted_at < ?",
            (reserving_cutoff,),
        )
        reserving_deleted = cur.rowcount if cur.rowcount >= 0 else 0

        conn.execute("DROP TRIGGER audit_no_delete")
        cur = conn.execute("DELETE FROM audit WHERE at < ?", (audit_cutoff,))
        audit_deleted = cur.rowcount if cur.rowcount >= 0 else 0
        conn.execute(_RECREATE_AUDIT_NO_DELETE)

        store.record_audit(
            conn,
            at=store.utcnow_iso(lambda: now),
            credential=None,
            event="prune",
            facade_id=None,
            domain_count=requests_deleted + reserving_deleted + audit_deleted,
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        # Defense in depth: even if the failure happened between the DROP
        # and the CREATE above (before the rollback could undo the DROP),
        # make sure the guard exists afterwards.
        conn.execute(_RECREATE_AUDIT_NO_DELETE)
        raise

    return {
        "requests_deleted": requests_deleted,
        "reserving_deleted": reserving_deleted,
        "audit_deleted": audit_deleted,
    }
