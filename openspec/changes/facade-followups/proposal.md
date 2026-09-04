# Change: facade-followups

## Why

Two small items the review rounds of `add-measurement-api` left on the
table, both noted in that change's `design.md` and neither worth its own
change:

1. **Facade traffic is indistinguishable from direct CLI traffic in the
   instance's logs.** The facade reuses `internetnl_cli.client.BatchClient`
   unchanged, so every call it makes upstream carries the CLI's own
   `User-Agent: internetnl-cli/<version>`. An operator reading the upstream
   nginx log cannot tell "the facade, on behalf of some tenant" from "a
   person running the CLI directly against the instance with the `facade`
   batch user" — which matters exactly when something looks wrong.
2. **A targeted brute-force can hide inside the overflow record.** The
   failed-authentication aggregator caps distinct username-and-route pairs
   at 512 per window; anything beyond collapses into a per-route `<other>`
   record. That bounds memory and audit growth (its purpose), but it also
   means an attacker who first burns 512 throwaway usernames in a minute
   can then brute-force a *real* tenant's username on the same route and
   have every one of those attempts land in `<other>`, unattributed.
   `add-measurement-api` accepted this as residual risk N2 ("documented,
   not built around") and asked for a follow-up.

## What Changes

**A. A facade User-Agent on every upstream call.** `netnl.upstream.
build_client` wraps whichever opener it is given (the real `urllib_opener`
or a test's fake) so that the request headers carry
`User-Agent: netnl/<version> internetnl-cli/<version>` — the facade's own
product token first, the client library's second, RFC 9110 product-token
style. The version is the installed distribution's (both live in the same
`internetnl-cli` distribution), with the same "unknown" fallback the CLI
already uses. Nothing else about the request changes: same body, same
`Authorization`, same redirect refusal, same error discipline.

**B. Known tenants keep their own failure bucket past the cap.** The
aggregator learns one bit per failure: whether the attempted username
belongs to an existing credential row (found by the lookup `authenticate`
already performs — no extra query). When the 512-pair cap is reached, a
*new* pair for an unknown username still folds into `<other>` exactly as
today; a new pair for a **known** tenant gets its own bucket, up to a
second, separate cap (`_MAX_TENANT_BUCKETS = 256`) on top of the first.
Because the set of known tenants is operator-controlled and small, this
reservation is bounded by construction, and a burst aimed at a real tenant
stays attributed to that tenant's username in the audit trail. A revoked
credential's username is still "known" (the row exists) and is attributed
the same way.

## Non-goals

- **No change to the CLI's own User-Agent** or to `internetnl_cli.client`
  at all. The wrapper lives in `netnl.upstream`; the CLI stays a plain,
  unmodified client.
- **No lifting of the existing 512 cap** for unknown usernames, and no
  change to the flush/sweep mechanics, the one-transaction batch write, or
  the "a failing aggregator write never fails the request" guarantee.
- **No new configuration.** Both caps stay module constants; the facade has
  a small, fixed route set and an operator-sized tenant set, so nothing here
  needs to be tuned per deployment.
- **No attribution promise beyond what exists.** An `auth-failure` row's
  `credential` remains attacker-supplied input, never verified tenant
  attribution — the spec's existing caveat stands unchanged. What changes is
  only that a *known* username is no longer forced into `<other>`.

## Impact

- `netnl/upstream.py`: one wrapper function around the opener; one constant.
- `netnl/auth.py`: `_record_auth_failure` gains a keyword-only
  `known_tenant: bool`; two of its three call sites pass `True`; one new
  constant; the cap check becomes a two-tier check.
- Spec deltas on `measurement-api`: "Honest provenance" (identifies itself
  upstream too) and "Append-only audit trail" (the residual-risk paragraph
  is replaced by the reservation; one scenario added).
- Tests: header assertion through the existing `FakeOpener`; a cap test
  that fills 512 unknown pairs and then proves a known tenant still gets
  its own row, and that a 513th *unknown* one still does not; a test that
  the tenant tier is itself capped.
- `CHANGELOG.md`: two entries.
