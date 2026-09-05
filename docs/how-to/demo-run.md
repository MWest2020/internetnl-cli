---
status: current
last_reviewed: 2026-09-03
---

# Running the netnl demo

The demo is an anonymous, single-domain front door onto the `netnl` facade
(`/demo/*`, opt-in, see `openspec/changes/add-demo-run/design.md`): a
first-time visitor types a bare domain into the dark-launched demo page and
sees a real result, without an account. It is the smallest possible slice —
one domain, strictly bounded — not a way to issue tenant credentials; that
flow (turning a demo visitor into a supporter/tenant) is a separate, later
change and does not exist yet (see `openspec/changes/add-demo-run/tasks.md`,
owner input O4).

## Enabling it

Add these to the facade's environment (`deploy/.env` in the co-located
topology, the homelab `netnl-config`/`netnl-secrets` equivalent in the K8s
topology — see [deploy-facade.md](deploy-facade.md)):

```sh
NETNL_DEMO_ENABLED=1
NETNL_DEMO_ALLOWED_ORIGIN=https://demo.example.org   # the demo page's own origin, exactly
NETNL_DEMO_TENANT=netnl-demo
```

`NETNL_DEMO_ALLOWED_ORIGIN` must be the bare `https://host[:port]` origin
the demo page is actually served from — no path, no trailing slash (see
`design.md`'s D6 for the exact shape validated at startup). Everything else
(`NETNL_DEMO_MAX_PER_HOUR`, `NETNL_DEMO_MAX_CONCURRENT`,
`NETNL_DEMO_PER_IP_PER_HOUR`, `NETNL_DEMO_CLIENT_IP_HEADER`,
`NETNL_DEMO_DOMAIN_COOLDOWN_SECONDS`, `NETNL_DEMO_RETENTION_HOURS`,
`NETNL_DEMO_POLLS_PER_IP_PER_HOUR`) has a sensible default from
`design.md`'s configuration table; leave them unset to start.
`NETNL_DEMO_POLLS_PER_IP_PER_HOUR` (default 120) bounds anonymous *polling*
(`GET` status/results) per client-IP bucket, separately from
`NETNL_DEMO_PER_IP_PER_HOUR`'s bound on *accepted submissions* — see
[reference/demo-api.md](../reference/demo-api.md) for the poll cadence it
is sized against.

Leaving `NETNL_DEMO_ENABLED` unset (the default) means the three `/demo/*`
paths (six routes, counting each path's own `OPTIONS` preflight) do not
exist as far as any client can tell — the ordinary 501 `not-implemented`
catch-all, same as any other unmapped path.

## Issuing the borrowed credential

The demo runs under one ordinary credential row — issued exactly like a
tenant's, then never used to authenticate as anyone:

```sh
netnl-admin user add netnl-demo
```

This prints a generated password once, to stdout. **Throw it away** — write
it down nowhere, do not put it in a secret store. Nobody ever authenticates
as `netnl-demo`; the demo routes never read an `Authorization` header at
all (design.md, D9). The row's only job is to exist, unrevoked, as the
identity every accepted demo submission is recorded under in the audit
trail (`credential = netnl-demo`, `event = submit`, `domain_count = 1`) —
indistinguishable in shape from an ordinary tenant submission.

**Use a dedicated, never-issued-to-anyone username** for `NETNL_DEMO_TENANT`
(`netnl-demo` above is the convention; anything not already, or ever,
handed out to a real tenant works). If this username were ever also a real
tenant's own credential, the demo would become an anonymous read path into
*that tenant's* runs: `GET /demo/requests/{id}` and `.../{id}/results` are
scoped to whichever credential row `NETNL_DEMO_TENANT` names, with no
further check — the same row, the same rows in `requests`.

## Header trust and the client IP

The per-IP bucket (`NETNL_DEMO_PER_IP_PER_HOUR`, and the poll budget
`NETNL_DEMO_POLLS_PER_IP_PER_HOUR`) trusts whatever value arrives in
`NETNL_DEMO_CLIENT_IP_HEADER` (`CF-Connecting-IP` by default) — a value the
*edge* sets, not something this facade can itself verify. Reachable
**only** through Cloudflare (topology 1's primary path, or any edge that
always overwrites this header before proxying), that is a safe assumption:
Cloudflare sets `CF-Connecting-IP` itself and strips any client-supplied
copy. Reachable **also** through a bypass that does not set (or does not
overwrite) this header — most concretely, topology 1's Tailscale Funnel
fallback (see [deploy-facade.md](deploy-facade.md#the-demo-family-at-the-edge)) —
a caller going in through that path can put *anything* in
`CF-Connecting-IP`, spoofing its way into a fresh per-IP bucket on every
request.

That per-IP bucket is a **dampener against casual repeat use from the same
visitor**, not the hard rate limit: the demo tenant's own aggregate
rate/concurrency cap (`NETNL_DEMO_MAX_PER_HOUR`/`NETNL_DEMO_MAX_CONCURRENT`,
via the same atomic reservation transaction the tenant surface uses) is the
one bound that holds regardless of what any header claims, because it does
not key on the header at all. Three options if the Funnel-bypass gap
matters for a given deployment: add a Cloudflare rule scoped to `/demo/*`
that only forwards traffic that actually came through Cloudflare (defeats
the header-spoof from the Funnel path, since a request that skipped
Cloudflare never gets that rule's stamp); turn the Funnel off outright
outside of an active fallback need; or accept the gap and rely on the
aggregate cap while the Funnel stays up. See deploy-facade.md's own
`/demo/*` section for the same trade-off from the edge-configuration side.

One more reviewer-noted limit worth stating plainly: the per-IP bucket, the
per-domain cooldown and the poll budget are **all in-process, per running
`netnl-serve` instance** — running more than one replica behind a load
balancer means each replica keeps its own independent counters, so the
*effective* per-IP/per-domain/poll limits multiply by the number of
replicas. The demo tenant's own aggregate cap (`limits.reserve_submission`,
backed by the shared SQLite file) is the only one of these bounds that
still holds cluster-wide regardless of replica count.

## The kill switch

Revoke the row and every demo request answers 503 `demo-unavailable` from
that moment on — no restart, no configuration change:

```sh
netnl-admin user revoke netnl-demo
```

Re-enable it with:

```sh
netnl-admin user reissue netnl-demo
```

`reissue` (not `add`) is the correct lever here: it works on the *existing*
row whether it is currently revoked or not — a fresh password/salt, with
`revoked_at` cleared — where `netnl-admin user add netnl-demo` would refuse
with "user already exists" the moment the row exists at all, revoked or
active. This is the kill switch's other half: `revoke` turns the demo off,
`reissue` turns it back on, both without a restart or a configuration
change. `reissue` also prints a generated password once, to stdout — throw
it away exactly like `user add`'s: the demo never authenticates as this
credential either way (design.md, D9).

This is the operator's entire abuse-response lever for the demo surface.
When to pull it is below.

## Abuse response: who pulls it, and on what signal

Decided 2026-09-05 (owner Mark): **one human revoker, one written
threshold.** No automatic revocation — a false positive would take the
public demo offline unattended, and this surface is not load-bearing enough
to justify that. Mark revokes; nobody else has `netnl-admin` on the pod.

Pull the switch when any of these holds:

- **Sustained saturation.** The demo hits `NETNL_DEMO_MAX_PER_HOUR` for
  three consecutive hours *and* tenant submissions are visibly queueing
  behind it. One busy hour is the cap doing its job; three in a row with
  tenant impact is the demo crowding out the people it was meant to
  advertise to.
- **Single-source flooding.** One IP burns the per-IP cap every hour for
  three consecutive hours — the per-IP bound is holding, but somebody is
  clearly automating against it rather than trying the demo.
- **Targeting.** Submitted domains are plainly not the submitter's own
  (a run of unrelated third-party domains, or anything on a well-known
  target list). The terms permit measuring hosts you operate; the demo
  cannot verify that, so this is the one signal that is judged by eye.

What to look at, on the pod:

```sh
netnl-admin user list          # is the demo row still active?

# demo submissions per hour over the last day
sqlite3 "$NETNL_DB" \
  "SELECT substr(at,1,13) AS hour, count(*), sum(domain_count)
     FROM audit WHERE credential='netnl-demo' AND event='submit'
       AND at > datetime('now','-1 day')
   GROUP BY hour ORDER BY hour;"
```

The audit table is append-only and `submit` rows carry the credential and
the domain count, so this is the whole picture for the demo surface — there
is no separate metrics stack to consult.

After revoking: say so on the demo page (it already renders
`demo-unavailable` as a plain message), and note the reason and the hour
range in the change's `tasks.md` or a handover note, so the eventual
decision to raise the caps has evidence under it rather than a memory.

## Smoke check

The demo page itself probes a fixed, all-zero id on load to tell whether the
demo is live, without needing a real submission first:

```sh
curl -s -o /dev/null -w '%{http_code}\n' \
  https://your-facade-host/demo/requests/00000000000000000000000000000000
```

- **404** — the demo is enabled and reachable; an all-zero id is simply not
  a request that exists (the same "foreign or unknown id" 404 the
  authenticated surface gives).
- **501** — the demo is not enabled at all.
- **503 `demo-unavailable`** — enabled, but the borrowed credential is
  missing or revoked (the kill switch is engaged).

A full end-to-end check submits a real domain and polls it through to
`done` — see [reference/demo-api.md](../reference/demo-api.md) for the
exact contract the demo page itself relies on (poll cadence, error shapes,
CORS).

## CORS

`NETNL_DEMO_ALLOWED_ORIGIN` is the *only* origin the demo answers to —
never echoed, never combined with `Access-Control-Allow-Credentials`. If
the demo page moves to a new hostname (or gains a staging copy), update
this variable to match — the facade will otherwise answer real requests
from the old/other origin with 403 `forbidden-origin` and preflight requests
with a CORS-header-free 204 a browser will not let through. See
`design.md`'s D6/D8 and `openspec/changes/add-demo-run/tasks.md`'s owner
input O6 (a single origin is a deliberate constraint, not yet revisited).

## Retention

A demo-owned request stays retrievable for `NETNL_DEMO_RETENTION_HOURS`
(default **24 hours**) — much shorter than the tenant surface's
`NETNL_RESULT_RETENTION_DAYS` (default 7 days), applied on the same
`netnl-admin prune` pass (see [deploy-facade.md](deploy-facade.md#6-schedule-prune)
for the cron cadence that actually bounds this in practice). `netnl-admin
prune`'s output gains a `demo requests pruned: N` line once the demo is
configured; without `NETNL_DEMO_ENABLED`, the output is unchanged.

## Not an SLA

Same caveat as the rest of the deployment docs
([deploy-facade.md](deploy-facade.md#not-an-sla)): homelab-grade, no managed-
service guarantee. The demo's own bounds (per-tenant hourly cap, per-IP
cap, per-domain cooldown) exist to keep one anonymous surface from
overwhelming the same finite upstream instance every tenant shares — not to
promise availability.
