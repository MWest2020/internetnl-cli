# Tasks: add-demo-run

## Owner inputs — not this build's call

- [ ] O1 When to flip the dark-launched demo page from private preview to
      publicly linked (README, demo-repo landing) — capacity headroom
      observation needed first
- [ ] O2 Final `NETNL_DEMO_MAX_PER_HOUR` / `NETNL_DEMO_MAX_CONCURRENT` /
      `NETNL_DEMO_PER_IP_PER_HOUR` values once real demo traffic exists —
      the defaults in `design.md` are a starting point, not a measurement
- [ ] O3 Whether the demo needs its own upstream capacity reservation
      (a slice of `NETNL_MAX_CONCURRENT` on the batch instance) once it is
      publicly linked, or shares headroom with tenants unmanaged
- [ ] O4 Timing/scope of the BMC-bridge (supporter-key issuance from a demo
      visitor) — explicitly not designed or stubbed in this change
- [ ] O5 Abuse-response runbook specific to an anonymous surface (who
      revokes `NETNL_DEMO_TENANT`, and under what observed condition)
- [ ] O6 Whether `NETNL_DEMO_ALLOWED_ORIGIN` ever needs to be a list rather
      than one origin (e.g. a staging + production demo page) — the current
      design deliberately supports exactly one

## T1. OpenSpec change

- [x] 1.1 `proposal.md` — why, what changes, non-goals, impact
- [x] 1.2 `design.md` — pinned decisions D1–D15, configuration table, HTTP
      surface, reused-not-reimplemented, privacy invariant, testing
      constraints, exit criteria
- [x] 1.3 `tasks.md` — this file
- [x] 1.4 Spec delta `specs/measurement-api/spec.md`: MODIFIED
      "Authenticated surface" (full current text plus the demo-family
      paragraph, not numbered against anything not yet built), ADDED
      "Anonymous demo runs are strictly bounded" (10 scenarios)
- Verify: `openspec validate add-demo-run --strict`

## T2. Settings

- [x] 2.1 `DemoSettings` dataclass in `netnl/settings.py`: `allowed_origin`,
      `tenant`, `max_per_hour`, `max_concurrent`, `per_ip_per_hour`,
      `client_ip_header`, `domain_cooldown_seconds`, `retention_hours`,
      `polls_per_ip_per_hour` (builder-review fix, M2)
- [x] 2.2 `Settings.demo: DemoSettings | None`; `None` when
      `NETNL_DEMO_ENABLED` is unset/not `"1"`
- [x] 2.3 Fail-closed: `NETNL_DEMO_ALLOWED_ORIGIN`/`NETNL_DEMO_TENANT`
      required once enabled, `SettingsError` naming the missing variable
- [x] 2.4 Origin validated against `^https://[A-Za-z0-9.-]+(:[0-9]{1,5})?$`,
      `http://localhost[:port]` accepted only under `NETNL_ALLOW_HTTP=1`
- [x] 2.5 Numeric defaults (6/2/2/900/24/120) via the existing
      `_resolve_numeric` helper; negative/non-integer rejected the same way
      as every other numeric setting
- [x] 2.6 Tests: defaults, each required-var-missing case names itself,
      origin-shape rejection (bad scheme, bad port, embedded path), the
      `NETNL_ALLOW_HTTP` localhost carve-out, disabled-by-default (`demo is
      None` with no other demo var set)
- Verify: `uv run pytest tests/netnl/test_netnl_demo_settings.py -q`

## T3. Demo submit route

- [x] 3.1 `src/netnl/demo.py`: `DemoRequest` (pydantic, `extra="forbid"`,
      one field `domain: str`), domain normalisation (D14), origin check,
      demo-credential availability check (503 `demo-unavailable`), per-domain
      cooldown, per-IP bucket, `limits.check_domains` reuse with the D14
      literal message, `limits.reserve_submission` via
      `dataclasses.replace`, upstream submit with `name="netnl-demo"`
- [x] 3.2 `POST /demo/requests` registered in `create_app`, only when
      `settings.demo` is not `None`
- [x] 3.3 `netnl.replies.LABEL_STATUS` gains `demo-unavailable` (503),
      `forbidden-origin` (403)
- [x] 3.4 `tests/netnl/conftest.py`: `demo_env`, `demo_app`, `demo_client`
      fixtures; autouse reset of `demo.py`'s module-level per-IP/cooldown
      state between tests
- [x] 3.5 Tests (the full list from `design.md`'s "Testing constraints" and
      privacy invariant, submit-side): disabled → 501; enabled + valid body
      → 200 with facade id; extra field / `type` field / list `domain` →
      422/400 (pydantic rejects, never reaches the handler); trim+lowercase
      normalisation; unfriendly-shape/SSRF domain → 400 with the literal
      D14 message; missing/revoked demo credential → 503
      `demo-unavailable`; per-tenant-hour cap → 429; per-IP cap → 429 (IPv4
      `/32` bucketing, IPv6 `/64` bucketing, unparseable/missing header →
      shared `unattributed` bucket); domain cooldown → 429, never returning
      an existing `request_id`; `Authorization` header fully ignored (a
      monkeypatch of `auth.hash_password` to raise still lets a demo
      request through); upstream body sent is exactly `{"type": "web",
      "domains": [<domain>], "name": "netnl-demo"}`; audit row shape
      (`event=submit`, `credential=<demo tenant>`, `domain_count=1`)
- Verify: `uv run pytest tests/netnl/test_netnl_demo.py -q`

## T4. Demo status/results routes

- [x] 4.1 `store.owned_request_or_404` — lifted out of `api.py`'s
      `_owned_request_or_404`, parameterised on `credential_id`; both
      tenant call sites (`GET /requests/{id}`, `GET
      /requests/{id}/results`) updated with no behaviour change
- [x] 4.2 `GET /demo/requests/{id}` / `GET /demo/requests/{id}/results`,
      owner-scoped to the demo credential via the same shared helper
- [x] 4.3 Tests: a demo-issued id is retrievable via the demo routes; a
      tenant's own id is a 404 via the demo routes and vice versa
      (isolation in both directions); results passthrough is structurally
      unchanged (same "no key added/removed/reordered" contract as the
      tenant surface)
- Verify: `uv run pytest tests/netnl/test_netnl_demo.py -q`

## T5. CORS, preflight, no-store

- [x] 5.1 `demo.demo_response_headers(request, settings)` shared helper:
      `Access-Control-Allow-Origin` (exact configured value, never echoed,
      only when the request's `Origin` is absent or matches), `Vary:
      Origin`, `Access-Control-Expose-Headers`, `Cache-Control: no-store`
      — all scoped to `/demo/*` paths only
- [x] 5.2 `demo_headers` middleware registered **last** in `create_app`
- [x] 5.3 `handle_unexpected` calls the same helper for a `/demo/*` path
- [x] 5.4 Explicit `OPTIONS` routes on all three demo paths → 204
- [x] 5.5 A present, non-matching `Origin` on an actual demo route (not
      `OPTIONS`) → 403 `forbidden-origin`; no `Origin` header → allowed
- [x] 5.6 Tests: ACAO present on 200/400/429/404/body-size-400/catch-all-500
      for a demo path; ACAO absent on every non-demo route (`/health`,
      `/requests`, ...); preflight → 204 with CORS headers on origin match,
      204 without them on mismatch; existing security headers
      (`Content-Security-Policy` et al.) still present on every demo reply
- Verify: `uv run pytest tests/netnl/test_netnl_demo.py -q`

## T6. Privacy proof

- [x] 6.1 Test: submit a demo request with a distinctive fake client IP
      (via the configured header), fake `Origin`, and a distinctive fake
      domain, across every rejection path (origin mismatch, unavailable
      credential, cooldown, per-IP cap, tenant-cap, shape/SSRF) and the
      accepted path — assert none of the three markers appears anywhere in
      the raw SQLite file's bytes
- [x] 6.2 Same assertion against `caplog` (all levels) for the same set of
      paths
- [x] 6.3 Test: a rejected demo request (any reason) writes zero rows to
      `audit`
- Verify: `uv run pytest tests/netnl/test_netnl_demo_privacy.py -q`

## T7. Retention and admin output

- [x] 7.1 `NETNL_DEMO_RETENTION_HOURS` applied in `retention.prune` as a
      demo-scoped delete, placed after the existing reserving-audit step;
      new `demo_deleted` count in `prune`'s return value
- [x] 7.2 `netnl-admin`'s `_prune` prints the demo count only when
      `settings.demo` is not `None`
- [x] 7.3 Tests: a demo row older than the retention window is pruned; one
      23 hours old is kept; a tenant row 3 days old is unaffected by the
      demo-scoped delete; with no demo configuration, `prune`'s and
      `netnl-admin prune`'s output are byte-identical to before this change
- Verify: `uv run pytest tests/netnl/test_netnl_retention.py
  tests/netnl/test_netnl_admin.py -q`

## T8. Docs

- [x] 8.1 `docs/how-to/demo-run.md` — runbook: enabling the demo, issuing
      and discarding the borrowed credential's password, the smoke check
      (probe `GET /demo/requests/000...0` expecting 404 when on / 501 when
      off), the kill switch (revoke the demo credential → 503
      `demo-unavailable`)
- [x] 8.2 `docs/reference/demo-api.md` — the page contract: `POST
      /demo/requests` body `{"domain": ...}`, `GET .../{id}` and
      `.../{id}/results`, the poll cadence (5s → 15s, giving up around
      ~10 minutes), the CORS requirements, id hygiene (facade-issued only),
      provenance (`X-Netnl-Instance`/`X-Netnl-Notice`), and the full error
      table with the literal, directly-showable messages
- [x] 8.3 `docs/index.md` links both new pages
- [x] 8.4 `docs/how-to/deploy-facade.md` — an edge section covering
      `/demo/*` (what to leave un-rate-limited-differently vs. the
      authenticated paths, if anything) and the Tailscale-Funnel note
      already present for the authenticated surface, restated for the demo
      family where it applies
- [x] 8.5 `README.md` — one "try it live" line pointing at the demo page
- [x] 8.6 `CHANGELOG.md` — `[Unreleased]` entry
- [x] 8.7 This file — tasks ticked, O1–O6 left unticked
- Verify: links resolve (`grep` for the new file paths from `docs/index.md`);
  `openspec validate add-demo-run --strict` and `openspec validate --all
  --strict`

## T9. Builder-review fixes (post-review hardening pass)

Two parallel reviews (adversarial + security) of the initial T1–T8 build
found overlapping, measured gaps. Fixed here, on top of the original build,
no scope beyond what those findings named:

- [x] 9.1 Slot leak: `POST /demo/requests` now calls `limits.
      refresh_stale_non_terminal` before reserving, mirroring the tenant
      path (`api.py`'s `submit`) — a non-polled run whose upstream status
      went terminal no longer occupies a concurrency slot for up to
      `NETNL_DEMO_RETENTION_HOURS`
- [x] 9.2 Check-then-act race: `netnl.demo._try_claim_ip_slot`/
      `_try_claim_domain` do sweep+check+insert inside one lock hold each
      (previously two separate lock acquisitions). This item originally
      also added an explicit release (`_release_ip_slot`/
      `_release_domain_claim`) when a later step failed — **superseded by
      T10/N1 below**, which removes that release entirely; see T10 for why.
      Real-server concurrency tests added
      (`tests/netnl/test_netnl_real_server.py`)
- [x] 9.3 Visitor language on the aggregate-cap 429:
      `limits.reserve_submission`'s tenant-facing wording is caught and
      rewritten to one fixed literal before reaching a demo reply
- [x] 9.4 Kill switch is two-directional: `netnl-admin user reissue <name>`
      re-keys an existing row (revoked or not) in place — fresh
      password/salt, `revoked_at` cleared, audited as `user-reissue`
- [x] 9.5 Poll budget: `NETNL_DEMO_POLLS_PER_IP_PER_HOUR` (default 120)
      bounds anonymous GET status/results calls per client-IP bucket; a
      status poll of a row whose *stored* status is already terminal
      answers from the store with no upstream call at all
- [x] 9.6 Host-free upstream errors: every upstream-originated
      `NetnlHTTPError` reaching a demo route is rewritten to one of two
      fixed, host-free outcomes (`netnl.demo._visitor_upstream_error`)
- [x] 9.7 Cooldown oracle closed: per-IP and per-domain-cooldown 429s share
      one literal; check order is origin → availability → shape/validation
      → per-IP → cooldown, so an over-quota IP never touches the domain
      cooldown at all
- [x] 9.8 Preflight completeness: `OPTIONS` replies add
      `Access-Control-Allow-Methods`/`-Allow-Headers`/`-Max-Age` on an
      origin match
- [x] 9.9 Header-trust and multi-replica notes documented (design.md,
      demo-run.md) — no behaviour change, doc-only per owner decision
- [x] 9.10 Demo-tenant hygiene note in demo-run.md
- [x] 9.11 pydantic validation failures on `/demo/*` flatten to the D14
      literal rather than reflecting field/`loc` paths
- [x] 9.12 Reviewer-minors: route-count wording, README's live-demo link,
      T2's verify command, generic-500 privacy test, `_MAX_BUCKETS`
      overflow cap added to the per-IP/poll/domain-cooldown structures
- Verify: `uv run pytest tests/netnl/test_netnl_demo.py
  tests/netnl/test_netnl_demo_cors.py tests/netnl/test_netnl_demo_privacy.py
  tests/netnl/test_netnl_demo_settings.py tests/netnl/test_netnl_admin.py
  tests/netnl/test_netnl_real_server.py -q`

## T10. Round-4 builder-review fixes (rehearsal blocker + minors)

Rehearsal approved all 12 prior findings but measured one new blocker
(N1) plus four smaller gaps. Fixed here, no scope beyond what was named:

- [x] 10.1 N1 (blocking): claims are never released any more. Measured
      before this fix: 30 POSTs from one IP against a saturated demo
      (tenant concurrency cap already at its limit) → 30 upstream `status`
      calls, per-IP bucket empty afterwards — the per-IP cap never
      actually bit, because `reserve_submission`'s own 429 (which fires
      *after* `refresh_stale_non_terminal` has already spent up to
      `max_concurrent` upstream calls) handed the claim straight back.
      `_release_ip_slot`/`_release_domain_claim` removed; a claim made by
      `_try_claim_ip_slot`/`_try_claim_domain` is now permanent for the
      rest of its window regardless of what happens afterwards — a
      reservation 429, a domain-cooldown rejection, or a genuine upstream
      failure on the `submit` call itself all now cost the same per-IP/
      per-domain budget an accepted run would (design.md's D4/D5 and
      "HTTP surface" sections amended; the release matrix they described
      no longer exists). New regression:
      `test_saturated_demo_bounds_total_upstream_calls_from_one_ip`
      (N POSTs from one IP → at most `per_ip_per_hour * (max_concurrent +
      1)` upstream calls total, per-IP bucket full afterwards). The
      pre-existing `test_upstream_failure_after_reservation_releases_ip_
      and_domain_claims` (which asserted the old release-on-upstream-
      failure behaviour) is renamed/rewritten to assert the new one
- [x] 10.2 N2 (low, follows from N1): the per-domain cooldown remains a
      residual, accepted cross-visitor oracle ("was this domain submitted
      recently") — but no longer a free one. New regression:
      `test_domain_cooldown_probing_is_bounded_by_the_per_ip_cap`
      (repeated cooldown probes from one IP are bounded to
      `per_ip_per_hour`). design.md's D5 bullet documents the residual and
      the bound explicitly
- [x] 10.3 N3 (low): `netnl-admin user reissue` now refuses to re-key a
      currently *active* (non-revoked) row without a new `--force` flag,
      with an error explaining why (re-keying it invalidates the live
      credential for anyone using it); reissuing an already-revoked row
      needs no flag, unchanged. The `user-reissue` audit row's `detail`
      now records `previous-revoked-at=<value or none>`. Tests in
      `test_netnl_admin.py`
- [x] 10.4 N4 (nit): resolved by 10.1 — with no release ever happening,
      `bucket.remove(now)`'s timestamp-identity race (a failing request's
      release freeing a concurrently-succeeding request's own entry) can
      no longer occur; there is nothing left to give a unique claim token
- [x] 10.5 N6 (nit): the real-server per-IP-cap/cooldown concurrency tests
      now assert `accepted == per_ip_cap` / `accepted == 1` rather than
      `<=`, so a vacuous zero-accepted pass can no longer hide a broken
      claim
- Verify: `uv run pytest tests/netnl/test_netnl_demo.py
  tests/netnl/test_netnl_real_server.py tests/netnl/test_netnl_admin.py -q`;
  repeat the N1 measurement (30 POSTs, one IP, saturated demo) and confirm
  the bounded call count and full bucket

## Overall verification

- `sh scripts/verify.sh` green
- `openspec validate add-demo-run --strict` green
- `openspec validate --all --strict` green
- Working tree clean; branch not merged
