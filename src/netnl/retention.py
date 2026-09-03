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
    # openspec/changes/add-demo-run, D11: the demo family's own, much
    # shorter retention window. `None` when the demo is not configured —
    # nothing below ever runs in that case, so an operator who never opted
    # in gets byte-identical `prune` behaviour to before this change.
    demo_cutoff = (
        store.utcnow_iso(lambda: now - timedelta(hours=settings.demo.retention_hours))
        if settings.demo is not None
        else None
    )

    conn.execute("BEGIN IMMEDIATE")
    try:
        # Round-3 fix (security-L3): scoped to `upstream_id IS NOT NULL` —
        # without this, a `reserving` row stranded *longer* than the
        # (much longer) result-retention window would be deleted here,
        # before the stranded-reservation audit below ever runs, silently
        # losing the one thing that could reconstruct it. Rows still
        # `reserving` are only ever removed by the dedicated stranded-
        # reservation delete further down, always preceded by its audit.
        cur = conn.execute(
            "DELETE FROM requests WHERE submitted_at < ? AND upstream_id IS NOT NULL",
            (result_cutoff,),
        )
        requests_deleted = cur.rowcount if cur.rowcount >= 0 else 0

        # Round-2 fix (finding 5, accepted restrisico — see design.md,
        # "Concurrency and storage"): a row still `reserving` here does not
        # prove the upstream submit never happened — it proves only that
        # this facade never recorded the `upstream_id` (the process could
        # have crashed *after* upstream accepted the submission but
        # *before* `finalize_reservation` ran). Once this row is deleted,
        # that upstream run — if it exists — becomes unreachable through
        # the facade forever: no `upstream_id` was ever stored, so there is
        # nothing left to poll or cancel it by. This cannot be fixed here
        # (the upstream id was never known), so the minimal mitigation is
        # to make it *reconstructable*: one audit row per pruned row,
        # naming its facade id, owning credential and domain count, with
        # its original `submitted_at` in `detail` — enough for an operator
        # who spots an unexplained run on the upstream instance's own
        # dashboard to correlate it back to a tenant and a submission time.
        #
        # Round-3 fix (reviewer-m12): `LEFT JOIN` + `COALESCE`, not an
        # inner `JOIN` — a missing `credentials` row (should never happen,
        # but `ON DELETE` semantics for `credential_id`'s foreign key are
        # not enforced by a cascade here) must not silently drop that row
        # from this audit trail; it is named `<unknown>` instead of
        # vanishing.
        stranded = conn.execute(
            "SELECT r.facade_id, r.domain_count, r.submitted_at, "
            "COALESCE(c.username, '<unknown>') AS username "
            "FROM requests r LEFT JOIN credentials c ON c.id = r.credential_id "
            "WHERE r.upstream_id IS NULL AND r.submitted_at < ?",
            (reserving_cutoff,),
        ).fetchall()
        for row in stranded:
            store.record_audit(
                conn,
                at=store.utcnow_iso(lambda: now),
                credential=row["username"],
                event="reserving-pruned",
                facade_id=row["facade_id"],
                domain_count=row["domain_count"],
                detail=row["submitted_at"],
            )

        cur = conn.execute(
            "DELETE FROM requests WHERE upstream_id IS NULL AND submitted_at < ?",
            (reserving_cutoff,),
        )
        reserving_deleted = cur.rowcount if cur.rowcount >= 0 else 0

        # openspec/changes/add-demo-run, D11: placed after the reserving-
        # audit step above (so a stranded demo reservation is still
        # individually audited by that pre-existing path before this
        # newer, demo-scoped delete ever runs), before the audit-retention
        # delete below. Scoped to `upstream_id IS NOT NULL` like the main
        # result-retention delete — a still-`reserving` demo row is left
        # entirely to the general reserving-grace mechanism above, not
        # this one. No per-row audit here: a demo run reaching its own,
        # much shorter retention window is the routine case (mirroring the
        # main result-retention delete just above, which also writes none)
        # — it is not the crash/stranded-reservation scenario the
        # reserving-audit step exists to reconstruct.
        demo_deleted = 0
        if settings.demo is not None:
            demo_credential = store.find_credential(conn, settings.demo.tenant)
            if demo_credential is not None:
                cur = conn.execute(
                    "DELETE FROM requests WHERE credential_id = ? AND upstream_id IS NOT NULL "
                    "AND submitted_at < ?",
                    (demo_credential["id"], demo_cutoff),
                )
                demo_deleted = cur.rowcount if cur.rowcount >= 0 else 0

        conn.execute("DROP TRIGGER audit_no_delete")
        cur = conn.execute("DELETE FROM audit WHERE at < ?", (audit_cutoff,))
        audit_deleted = cur.rowcount if cur.rowcount >= 0 else 0
        conn.execute(_RECREATE_AUDIT_NO_DELETE)

        # openspec/changes/add-supporter-issuance: pruned unconditionally
        # (not gated on `settings.supporter`, unlike the demo-scoped delete
        # above) — the table is self-contained (keyed on a BMC transaction
        # id, not a credential row) and always exists once `migrate` has
        # run, so there is nothing operator-specific to check first. Reuses
        # the existing audit-retention cutoff rather than introducing a new
        # retention variable — an issuance row carries no more sensitivity
        # than an audit row and is not a request result subject to the
        # (usually much shorter) result-retention window.
        cur = conn.execute(
            "DELETE FROM supporter_issuance WHERE created_at < ?", (audit_cutoff,)
        )
        issuance_deleted = cur.rowcount if cur.rowcount >= 0 else 0

        store.record_audit(
            conn,
            at=store.utcnow_iso(lambda: now),
            credential=None,
            event="prune",
            facade_id=None,
            domain_count=(
                requests_deleted
                + reserving_deleted
                + audit_deleted
                + demo_deleted
                + issuance_deleted
            ),
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
        # openspec/changes/add-demo-run, D11: always present (0 when the
        # demo is not configured) — counted separately from the tenant
        # retention counters above, never folded into them.
        "demo_deleted": demo_deleted,
        # openspec/changes/add-supporter-issuance: always present (0 if the
        # bridge was never used, or nothing has aged out yet).
        "issuance_deleted": issuance_deleted,
    }
