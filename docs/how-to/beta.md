---
status: current
last_reviewed: 2026-09-03
---

# Running the netnl private beta

This page covers task 4.3 of `openspec/changes/add-measurement-api`: a
short, private beta of the `netnl` facade with a handful of known
users, before the endpoint, terms and limits get a public docs page
(task 5.1) and a self-serve credential-request runbook (task 5.2).

Do not start this until [`scripts/acceptance.sh`](../../scripts/acceptance.sh)
(task 4.2) passes green against the live facade — see "Go/no-go" below.

## Issuing, revoking and listing credentials

Credentials are issued with `netnl-admin`, run inside the facade
container/pod. In the co-located compose topology
([deploy-facade.md](deploy-facade.md)):

```sh
docker compose -f deploy/compose.yaml exec netnl netnl-admin user add <naam>
```

In the K8s topology (per `design.md`'s "Two supported topologies" —
the facade is now fronted publicly by a Cloudflare Tunnel as the
primary path, with the Tailscale Funnel hostname kept up in parallel
as a fallback), the equivalent is:

```sh
kubectl -n netnl exec deploy/netnl -- netnl-admin user add <naam>
```

(The namespace `netnl`, deployment `netnl` and `netnl-config` ConfigMap
named above match the homelab manifests introduced in
[MWest2020/homelab#13](https://github.com/MWest2020/homelab/pull/13), so
these names are traceable back to their source.)

Both print the generated password **once**, to stdout. It is hashed
(`scrypt`, per-credential salt) before it ever touches the database, so
it cannot be recovered later — only rotated (`user revoke` followed by
a fresh `user add`). Hand the printed username/password pair to the
beta user out of band (not over the same channel you'd use to discuss
the beta publicly, and never committed anywhere in this repo).

Revoke a credential (effective immediately, no grace period — see the
spec's "Credential lifecycle" requirement):

```sh
kubectl -n netnl exec deploy/netnl -- netnl-admin user revoke <naam>
```

List issued credentials and their state (`active` or the revocation
timestamp):

```sh
kubectl -n netnl exec deploy/netnl -- netnl-admin user list
```

`netnl-admin` never prints a password after issuance — `user list`
shows only username, `created_at` and state.

## Onboarding a beta user

For each of the handful of known beta users, hand over:

- The endpoint: **`https://api.westerweel.work`** — the facade's
  primary, branded hostname, hand this out by default. The Funnel hostname
  (`https://netnl.<tailnet>.ts.net` — fill in your actual tailnet name)
  keeps working in parallel as a fallback if the primary name is ever
  unreachable; both currently front the same facade.

  **Provenance of the primary path:** a Cloudflare Tunnel named `netnl-api`
  (remotely-managed; its ingress config lives at Cloudflare, not in this
  repo), served by the `cloudflared` Deployment `netnl-tunnel` in the
  homelab's `netnl` namespace. The tunnel's run-token lives only in the
  out-of-band Secret `netnl-tunnel` — never in this repo. DNS is a proxied
  CNAME, `api.westerweel.work` → `<tunnel-id>.cfargotunnel.com`. A CLI build
  from before the `User-Agent` fix (see CHANGELOG) sends `urllib`'s default
  `Python-urllib/x.y` string and gets a 403 from Cloudflare before it ever
  reaches the facade — make sure beta users are on a current build.
- `INTERNETNL_ENDPOINT` must be the **bare base URL** (e.g.
  `https://api.westerweel.work`, no trailing path) — the facade serves
  the batch-v2 routes (`/requests`, `/requests/{id}`,
  `/requests/{id}/results`, `/metadata/report`) directly on the root, with
  no `/api/batch/v2` prefix. `/health` is served at the root too, but it
  is a separate, **unauthenticated** liveness route, not part of the
  batch-v2 subset (see design.md, "Facade image and liveness"). Any path
  the facade doesn't proxy replies `501 not-implemented` by design; that
  is not a misconfiguration to chase down.
- Their own `INTERNETNL_USERNAME` / `INTERNETNL_PASSWORD` pair from
  `user add` above.
- A pointer to the `internetnl` CLI quickstart in the top-level
  [README](../../README.md#quickstart) — the only change from the
  hosted Internet.nl instance or a self-hosted one is which
  `INTERNETNL_ENDPOINT`/`INTERNETNL_USERNAME`/`INTERNETNL_PASSWORD` they
  export; nothing else about the CLI changes (this is also what
  `scripts/acceptance.sh` verifies before the beta opens).

### Terms (state these explicitly, every time)

- **Only measure hosts you operate or have explicit permission to
  test.** This is the same rule the CLI's own README states and the
  facade's "Internal targets are refused" check partially backstops
  (it blocks obviously-internal names and IP literals, not "hosts you
  don't own") — the terms are a human agreement, not something the
  software fully enforces.
- **This service is not affiliated with, endorsed by, or run by
  internet.nl or Platform Internetstandaarden.** It is an independent
  instance of the same open-source batch software. Every facade reply
  already carries an `X-Netnl-Notice` header saying so; say it out
  loud to the user too.
- **Homelab-grade, no SLA** — see "Not an SLA" below.

## What to observe during the beta, and how to act on it

The beta's purpose is to find out whether the *default* limits
(`design.md`'s configuration table) fit real usage, before opening
things up further:

| Default | Meaning | Watch for |
|---|---|---|
| `NETNL_RATE_LIMIT=10` | submissions/credential/hour | beta users hitting 429 `rate-limited` routinely for legitimate use (not misuse) |
| `NETNL_MAX_DOMAINS=500` | domains/request | beta users splitting requests to work around 400 `bad-request` |
| `NETNL_MAX_CONCURRENT=2` | non-terminal runs/credential | beta users queuing behind their own earlier runs that haven't finished |
| in-process scrypt-concurrency cap (`max(4, min(8, cpu_count))`, not env-tunable) | concurrent password-hash verifications this process will run at once | a 503 `overloaded` reaching a beta user under ordinary (not attack) traffic — that means legitimate concurrent logins are queuing past the cap's short bounded wait, worth escalating rather than a symptom of a credential-guessing campaign |

A 503 `overloaded` carries a `Retry-After: 1` header; a well-behaved
client should back off briefly and retry. It is distinct from 429
`rate-limited` (a per-credential quota) — 503 can hit a caller who has
never made a request before, because it reflects this process's own
authentication-verification capacity, not anything the caller did wrong.
See design.md's "Authentication cost is bounded" and
[deploy-facade.md](deploy-facade.md#brute-force--rate-limiting-at-the-edge)
for what actually causes it and what backstops it at the edge.

Also watch the **upstream instance's** capacity directly — CPU, memory
and how long a batch of N domains actually takes on the VPS sizing
from [reference/self-hosted.md](../reference/self-hosted.md) — since
the facade's limits only ration access to a instance that has to do
the real DNS/TLS/IPv6 work per domain regardless of who asked.

If a default no longer fits, adjust it in the homelab `netnl-config`
ConfigMap (the K8s topology's equivalent of `deploy/.env`'s tunables
section — see `deploy/.env.example` and `design.md`'s configuration
table for what each variable does) and roll the facade deployment.
Record *why* a default changed (which observation drove it) — that
record is exactly the "capacity observations fed back into the default
limits" task 4.3 asks for, and it is what task 5.1's public limits
table will eventually restate.

Retention is not a beta-only concern but is worth restating here since
it shapes what a beta user can still retrieve later: finished requests
stay retrievable for `NETNL_RESULT_RETENTION_DAYS` (default **7
days**), audit records for `NETNL_AUDIT_RETENTION_DAYS` (default **90
days**) — both applied only when `netnl-admin prune` runs, so the
prune cron's cadence (see [deploy-facade.md](deploy-facade.md#6-schedule-prune),
"hourly is a reasonable starting cadence") is what actually bounds the
grace window in practice, not the configured day counts alone. If beta
observations suggest the prune cadence itself is too coarse (e.g. a
revoked credential's requests staying queryable far longer than
expected because prune ran late), that is exactly the same kind of
observation to fold back into the deploy schedule.

## Go/no-go

Run [`scripts/acceptance.sh`](../../scripts/acceptance.sh) against **both**
public URLs — the primary `https://api.westerweel.work` and the Funnel
fallback — before handing out the first beta credential, and again after
any limit/config change you make in response to what the beta turns up.
Both must pass: a beta user might be handed either URL (the fallback
matters only if it actually works), and a tunnel-specific misconfiguration
on one path would otherwise go unnoticed while the other stays green. It
exercises the unmodified `internetnl` CLI end to end (submit, results, and
the instance-not-public check) and is the go/no-go signal for task 4.2.
Read the script's own header comment for the environment variables it
needs (`NETNL_FACADE_URL`, `INTERNETNL_USERNAME`, `INTERNETNL_PASSWORD`,
`TEST_DOMAIN`, and the optional `NETNL_INSTANCE_PROBE_URL`).

## After the beta

Once the beta's observations have settled the defaults (or confirmed
they were already right), the next step is task 5 (community opening):
a public docs page stating the endpoint, terms, limits and retention
(task 5.1), a published credential-request runbook (task 5.2), and a
handover package complete enough to run without the original operators
(task 5.3). This page is deliberately narrower — a short-lived runbook
for a handful of known users, not the public-facing story.

Task 5.2's self-serve runbook is [Supporter keys](supporter-key.md): a
lifetime tenant credential issued after a small donation, still
subject to the same per-tenant rate limits described above as its
fair-use mechanism. The donation link is live, and — once an operator
has enabled the webhook bridge described in
[Automatic supporter-key issuance](supporter-webhook.md) — issuance
itself is automatic: a qualifying donation mints and mails the
credential without an operator in the loop. The manual, out-of-band
`netnl-admin user add` process described above remains the fallback
when the bridge is disabled or unavailable.

## Not an SLA

Same caveat as the deployment recipes
([deploy-facade.md](deploy-facade.md#not-an-sla),
[deploy-instance-vps.md](deploy-instance-vps.md#not-an-sla)): one
facade, one VPS instance, cron-scheduled retention, no managed-service
guarantee. Beta users should be told this plainly, not discover it when
something breaks.
