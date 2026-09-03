# Change: add-demo-run

## Why

The demo page for `netnl` already exists, dark-launched in the
`internetnl-cli-demo` repo, and it needs exactly one thing this facade does
not yet offer: a way for a first-time visitor to type a bare domain and see
a real result without an account. Today the only way in is an
operator-issued credential (`netnl-admin user add`) — fine for a tenant, a
wall for someone deciding whether to become one.

This is deliberately the smallest possible slice: one bare domain, one run,
strictly bounded, never touching the authenticated tenant surface's
correctness or its credential-hashing cost. The BMC-bridge/supporter-key
issuance flow that would turn a demo visitor into a real tenant is a
separate, later change — it is not built here, and nothing in this change
numbers or names it, since it does not exist yet.

## What Changes

**An anonymous, single-domain demo endpoint**, opt-in via
`NETNL_DEMO_ENABLED=1`, fronted by the facade's existing limits machinery
rather than a parallel one:

1. **One route family, one shape.** `POST /demo/requests` accepts exactly
   `{"domain": "example.nl"}` (pydantic `extra="forbid"` — a list or a
   `type` field is structurally impossible, not just rejected at runtime);
   `GET /demo/requests/{id}` and `GET /demo/requests/{id}/results` mirror the
   authenticated shape, owner-scoped to the demo credential. Explicit
   `OPTIONS` routes exist so a browser preflight gets 204, not the 501
   catch-all every other unmapped path gets.
2. **A borrowed identity, not a new one.** The demo runs under one ordinary
   credential row, issued once with `netnl-admin user add` with its password
   thrown away — nobody ever authenticates as it, so there is no password to
   protect or leak. Revoking that row (or it simply not existing) is the
   operator's kill switch: every demo request answers 503 `demo-unavailable`
   until a fresh one is issued. Demo submissions reuse
   `limits.reserve_submission` verbatim, with `max_domains` forced to 1 and
   the rate/concurrency limits swapped for the demo's own — the global
   per-hour ceiling on that swapped-in limit **is** the demo's aggregate
   rate limit; there is no second bookkeeping mechanism to keep in sync with
   the first.
3. **Two more layers, because "anonymous" needs more than a tenant limit.**
   A per-IP-bucket hourly cap (client IP from a configurable header, first
   comma-token, `/32`/`/64`-generalised, one shared "unattributed" bucket
   for anything unparseable) and a per-domain cooldown (an accepted run on a
   domain blocks a repeat for a configured window) sit in front of the
   tenant-style limit, both in-memory, both bounded by construction because
   only accepted runs ever add an entry and acceptance is itself capped by
   the global per-hour limit.
4. **Never touches auth.** No `Authorization` header is read, `Depends
   (auth.authenticate)` never runs on this path, and scrypt is never
   invoked for a demo request — the whole point of the demo is that it costs
   nothing to try and can never become a second path into brute-forcing a
   real credential.
5. **CORS scoped to exactly one origin.** `NETNL_DEMO_ALLOWED_ORIGIN` is the
   only origin the demo answers to on the wire — never echoed, never
   combined with credentials — with a fixed, minimal
   `Access-Control-Expose-Headers` and `Cache-Control: no-store` on every
   demo reply, added by a shared header helper so even the generic 500
   handler (which sits outside the middleware stack) carries them on a
   `/demo/*` path.
6. **Privacy by construction, not by promise.** The audit trail records a
   demo submission exactly like a tenant submission (event `submit`,
   credential = the demo tenant, `domain_count = 1`) and nothing else —
   no visitor IP, no `Origin`, no submitted domain, anywhere on disk or in a
   log line, ever, including on a rejection.
7. **Its own retention clock.** `NETNL_DEMO_RETENTION_HOURS` (default 24)
   prunes demo-owned rows on the existing `retention.prune` pass, counted
   and reported separately from the tenant retention counters.

## Non-goals

- **No self-service credential issuance.** Turning a demo visitor into a
  tenant (the BMC-bridge / supporter-key flow) is a separate, later change,
  not built or stubbed here.
- **No demo-specific database, queue or worker.** The demo is a thin,
  bounded front door onto the exact same `requests`/`audit` tables and the
  exact same upstream call path every tenant submission already uses.
- **No raising or reshaping the authenticated v2 surface's own limits,
  labels or behaviour.** Every change here is additive: a new opt-in route
  family, a new opt-in settings block, and a handful of shared helpers
  lifted out of `api.py` without changing what they do for the surface that
  already calls them.

## Impact

- **A second, much smaller trust boundary.** Unlike the tenant surface,
  this one has no credential at all standing between the internet and an
  upstream submission — every bound (per-IP, per-domain, per-tenant-hour,
  SSRF/shape checks reused verbatim from `limits.check_domains`) is load-
  bearing, not defence in depth.
- **Off by default.** `NETNL_DEMO_ENABLED` unset means the three `/demo/*`
  paths (six routes, counting each path's own `OPTIONS` preflight) do not
  exist as far as any client can tell — the same 501 `not-implemented`
  catch-all as any other unmapped path, not a 404 or a "coming soon" reply
  that would leak that the feature exists.
- **Operator load: one more credential to keep alive.** The kill switch
  (revoke the demo credential) is the same primitive operators already use
  for a misbehaving tenant, not a new mechanism to learn.
