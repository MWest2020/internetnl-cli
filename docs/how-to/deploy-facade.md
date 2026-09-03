---
status: current
last_reviewed: 2026-09-03
---

# Deploying the netnl facade

The facade (`netnl-serve`, package `src/netnl/`) is a small public gateway
that fronts a **private** batch instance: it validates, rate-limits and
audits requests, then submits them upstream with a server-side credential
that tenants never see. It does not replace
[Self-hosting a batch instance](../reference/self-hosted.md) — read that
first if the instance itself does not exist yet.

## Two supported topologies

`openspec/changes/add-measurement-api/design.md` ("Two supported
topologies") pins two ways to run the facade:

1. **Instance on a VPS, facade in a Kubernetes cluster (the one we run).**
   The batch instance runs on a VPS with a fixed public IPv4+IPv6, reached
   over a Tailscale tailnet — see
   [Deploying the upstream instance on a VPS, reached over a tailnet](deploy-instance-vps.md).
   The facade runs in Kubernetes, exposed publicly via a branded hostname
   over a Cloudflare Tunnel as the primary path (ours:
   `https://api.westerweel.work` — the hostname itself is configuration,
   like `NETNL_PUBLIC_HOST` for topology 2 below), with a Tailscale Funnel
   hostname (`*.ts.net`) kept up in parallel as a fallback — no Caddy edge
   needed there either way; the K8s manifests for that deployment live in
   a separate homelab repo — a link will replace this note once they are
   public.
2. **Co-located** (the recipe on the rest of this page): facade + instance
   on one host with a public IP, `deploy/compose.yaml` plus Caddy at the
   edge. Simpler, but needs a public-IP host that also runs the full
   instance stack.

The rest of this page covers topology 2. This page deploys
`deploy/compose.yaml` next to (not inside) the instance's own stack.

## Brute-force / rate limiting at the edge

The facade itself only offers a last-resort backstop against
credential-guessing traffic: `netnl.auth` fast-fails a request with no (or
an unparseable) `Authorization` header without touching the password
hasher, and caps how many password verifications may run *concurrently*
(a small, fixed number) — see
`openspec/changes/add-measurement-api/design.md`, "Tenancy and identity"
("Authentication cost is bounded on two axes"). That bound protects this
process's own CPU and memory from a burst of concurrent bad-credential
requests; it is **not** a rate limit, and it does nothing to slow down a
sustained, low-concurrency credential-guessing campaign spread out over
time. That is the edge's job, and it differs by topology:

- **Topology 1 (Cloudflare Tunnel in front of K8s).** Add a Cloudflare
  [rate limiting rule](https://developers.cloudflare.com/waf/rate-limiting-rules/)
  scoped to the auth-bearing paths (`POST /requests`, `GET
  /requests/*`, `GET /metadata/report` — everything except `GET /health`
  and the opt-in `security.txt`), keyed on the client IP, with a
  low-enough threshold that a credential-guessing script gets throttled or
  challenged long before it can run through any meaningful password space.
  This is configured in the Cloudflare dashboard/API for the tunnel's
  hostname, not in this repo.

  **Topology 1 has a second public ingress this rule does not cover
  (reviewer-M7).** Per `design.md`, "Two supported topologies", the same
  facade is also reachable via the **Tailscale Funnel** `*.ts.net`
  hostname kept up in parallel as a fallback (see `docs/how-to/beta.md`).
  A Cloudflare rate-limiting rule is scoped to the Cloudflare Tunnel's own
  hostname — it does nothing at all for traffic that goes straight to the
  Funnel hostname instead, which serves the exact same facade over a
  completely separate public path. Two honest options, pick one
  deliberately rather than assuming the Cloudflare rule alone covers this
  topology:
  - **Turn the Funnel off** except when actually needed for a specific
    fallback test or incident, so there is only one public ingress to rate
    limit at any given time; or
  - **Accept it as a known gap**: while the Funnel stays up, the facade's
    own in-process scrypt-concurrency cap (`netnl.auth`, bounded wait +
    503 `overloaded` on sustained saturation — see "Authentication cost is
    bounded" in design.md) is the *only* backstop against credential
    guessing over that path, since nothing at the edge is rate-limiting
    it. That backstop protects this process's own CPU/memory; it is not a
    rate limit and does not slow down a sustained, low-concurrency
    guessing campaign the way the Cloudflare rule does for the primary
    hostname.
- **Topology 2 (Caddy at the edge, the compose recipe on this page).**
  Vanilla Caddy, as shipped in `deploy/Caddyfile`, has **no built-in rate
  limiting** — being honest about that rather than implying protection
  that is not actually there. Two realistic options, neither wired up by
  default in `deploy/compose.yaml`:
  - The third-party
    [`caddy-ratelimit`](https://github.com/mholt/caddy-ratelimit) plugin,
    built into a custom Caddy image (`xcaddy build --with
    github.com/mholt/caddy-ratelimit`) and configured with a `rate_limit`
    directive on the auth-bearing paths, keyed on the client IP — the
    closest equivalent to the Cloudflare rule above.
  - A **fail2ban-on-logs** approach: point `fail2ban` at Caddy's JSON
    access log (`deploy/Caddyfile` already logs to a file the compose unit
    can mount into a `fail2ban` sidecar or the host), with a filter that
    matches repeated 401 responses from the same client IP against the
    facade's auth-bearing paths, and a ban action (e.g. a host firewall
    rule) once a threshold is crossed. Coarser than a proper rate-limiter
    (it reacts after the fact, per IP) but needs no custom Caddy build.

  Either way, this is a deployment-time addition an operator running
  topology 2 needs to make deliberately; it is not part of the
  `deploy/compose.yaml` recipe as shipped.

## The `/demo/*` family at the edge

`openspec/changes/add-demo-run` adds an opt-in, anonymous route family
(`/demo/*`, see [how-to/demo-run.md](demo-run.md)) with no credential in
front of it at all. Everything above about the *auth-bearing* paths does
not apply to it the same way — there is no `Authorization` header to
guess, so a Cloudflare rule or `caddy-ratelimit`/`fail2ban` config scoped to
"the auth-bearing paths" (as written above) simply does not cover
`/demo/*`, and should not be extended to it naively either: the demo's own
in-process bounds (the demo tenant's hourly/concurrency cap, a per-IP
hourly cap, and a per-domain cooldown — see `design.md`, D3–D5) are already
the load-bearing limits for this surface, not a backstop behind an
edge-level one the way `netnl.auth`'s scrypt cap is for the authenticated
paths. Two things worth doing at the edge anyway, both optional:

- **A separate, generous rate-limiting rule scoped to `/demo/*` only**, if
  the edge already has the machinery from the section above — high enough
  not to fight the demo's own per-IP cap (`NETNL_DEMO_PER_IP_PER_HOUR`) for
  a legitimate visitor, low enough to blunt a volumetric flood before it
  reaches the process at all. Not required: the demo's own bounds hold
  without it.
- **A CDN/edge cache rule that never caches `/demo/*` responses.** Every
  demo reply already carries `Cache-Control: no-store` (design.md, D7), so
  a compliant cache will not store them regardless — this is belt-and-
  braces for an edge that might not honour that header for every response
  shape.

**The Tailscale Funnel gap (see above) applies to `/demo/*` too, and in one
way more sharply.** If the demo is enabled and the facade is reachable via
both the Cloudflare Tunnel and the Funnel fallback (topology 1), a
`/demo/*`-scoped Cloudflare rule (if added per the bullet above) covers only
the Cloudflare Tunnel hostname — the exact same gap the authenticated
surface has. Unlike the authenticated surface, there is no credential at
all standing between an anonymous caller and an upstream submission on this
path, so while the Funnel stays up, the demo's own in-process bounds are
the *only* thing standing between the Funnel hostname and the shared
upstream instance's capacity. Read `NETNL_DEMO_ALLOWED_ORIGIN` in this
light too: it only rejects a **browser** carrying a mismatched `Origin`
header (D6) — it does nothing against a non-browser caller hitting the
Funnel hostname directly with no `Origin` header at all, since an absent
`Origin` is allowed through by design (see
[reference/demo-api.md](../reference/demo-api.md)). The same two options
from the section above apply: turn the Funnel off outside of an active
fallback need, or accept the gap and rely on the demo's own bounds while it
stays up.

## Prerequisites

- A batch instance already running, on a machine with a **fixed public
  IPv4 address and IPv6** — see
  [reference/self-hosted.md](../reference/self-hosted.md) for why that
  requirement exists and cannot be worked around with NAT or a
  hosted-behind-CGNAT homelab box.
- That instance publishes **no public API port**. Only the facade in this
  compose unit is public; the instance is reachable solely on an internal
  docker network.
- A batch credential issued on that instance
  (`INTERNETNL_USERNAME`/`INTERNETNL_PASSWORD` in the CLI's terms — here
  they become `NETNL_UPSTREAM_USERNAME`/`NETNL_UPSTREAM_PASSWORD`).
- A DNS name pointed at this facade host (`NETNL_PUBLIC_HOST`), and ports
  80/443 free for Caddy.
- Docker and the Compose plugin.

## 1. Join the upstream network

The facade and the instance must share a docker network so the facade can
reach the instance's API without it ever being public. On the instance
host, find the network its compose stack created:

```sh
docker network ls
```

It is typically `<instance-compose-project>_default` or similarly named
by the instance's own `docker-compose.yml`. Note that name — you will put
it in `.env` as `NETNL_UPSTREAM_NETWORK`. If the facade runs on the same
host as the instance, no further action is needed: `deploy/compose.yaml`
declares it as an `external` network and joins it directly. If the facade
runs on a *different* host, put both hosts on a shared overlay network
first (out of scope here — a single-host deployment is the common case
this recipe targets).

## 2. Configure

```sh
cp deploy/.env.example deploy/.env
```

Edit `deploy/.env` and fill in at least:

- `NETNL_PUBLIC_HOST` — the public hostname for this facade.
- `NETNL_UPSTREAM_NETWORK` — the network name from step 1.
- `NETNL_UPSTREAM_ENDPOINT` — the instance's batch v2 URL as reachable
  *on that internal network* (not its public form — it has none).
- `NETNL_UPSTREAM_USERNAME` / `NETNL_UPSTREAM_PASSWORD` — the batch
  credential from the instance.

Leave the tunables (rate limit, max domains, retention, ...) at their
commented-out defaults to start; see `deploy/.env.example` and
`openspec/changes/add-measurement-api/design.md`'s configuration table for
what each one does. `deploy/.env` is gitignored — never commit it.

Optionally, uncomment `NETNL_SECURITY_CONTACT` and set it to a `mailto:`
or `https:` contact value to publish `GET /.well-known/security.txt`
(RFC 9116); leave it unset and the path answers the ordinary 501
not-implemented, same as any other unrecognised path.

## 3. Bring the stack up

```sh
docker compose -f deploy/compose.yaml up -d
```

This starts `netnl` (the facade, no published port) and `edge` (Caddy,
publishing 80/443 and terminating TLS for `NETNL_PUBLIC_HOST`). Because
Caddy is the hop that terminates TLS in this topology, `deploy/Caddyfile`
sets `Strict-Transport-Security` there, not in the facade process behind
it. The facade's SQLite database lives on the named volume `netnl-data`, created
with owner-only permissions by the app itself.

## 4. Issue a tenant credential

```sh
docker compose -f deploy/compose.yaml exec netnl netnl-admin user add <naam>
```

This prints a generated password **once**, to stdout — it is not stored
in plain text and cannot be recovered later, only rotated (`user revoke`
followed by a new `user add`). Give the printed username/password pair to
the tenant out of band.

## 5. Acceptance check

Point the `internetnl` CLI at the facade's public address and confirm it
behaves identically to pointing it at a batch instance directly — this is
the acceptance test for task 4.2 (`openspec/changes/add-measurement-api`):

```sh
INTERNETNL_ENDPOINT=https://$NETNL_PUBLIC_HOST \
INTERNETNL_USERNAME=<naam> \
INTERNETNL_PASSWORD=<generated password> \
internetnl submit example.org
```

Also confirm from outside the facade host that the instance's own API
port is *not* reachable — the facade must be the only public path in.

## 6. Schedule `prune`

Retention (expired results, stale `reserving` rows, old audit records) is
applied by `netnl-admin prune`, run on a schedule — never as an
in-process thread (see design.md, "Tenancy and identity"). The compose
file defines it as a `prune` profile so it does not start with `up`:

```sh
docker compose -f deploy/compose.yaml --profile prune run --rm prune
```

Add that line to host cron or a systemd timer. The cadence you pick
**bounds the grace window**: an expired request stays queryable by its
owner, and a stale `reserving` row keeps pinning a concurrency slot,
until `prune` next runs. Hourly is a reasonable starting cadence; nothing
in the facade enforces a maximum.

## Hardening: the upstream credential via Docker/Compose secrets

Step 2 above puts `NETNL_UPSTREAM_USERNAME`/`NETNL_UPSTREAM_PASSWORD` in
`deploy/.env`, loaded into the `netnl` container via `env_file: .env`. For
a homelab-grade deployment that is acceptable — the file itself is
gitignored and readable only by whoever can already read the compose
directory. But once the container is running, that credential sits in
the container's *environment*, which is visible to anyone who already has
host-level access to the container: `docker inspect <container>` prints
it (Compose promotes `env_file` entries into the container's `Config.Env`
the same as `environment:` does), and so does reading
`/proc/<pid>/environ` for the container's process. If you consider
host/daemon access itself a boundary worth hardening against, move the
credential to a Docker/Compose **secret** instead — a file, not an
environment variable, mounted read-only at `/run/secrets/<name>` inside
the container and never written into `Config.Env` or the process
environment as delivered.

Note what this does *not* do: `netnl` (`src/netnl/settings.py`) only
reads plain environment variables — there is no `NETNL_UPSTREAM_PASSWORD_FILE`
(or similar `*_FILE`) convention in the current code, so a secret file by
itself is not enough. The credential still has to end up as an
environment variable *inside the container* before `netnl-serve` starts;
secrets just change how it gets there (from a root-owned, non-inspectable
file, at container start) instead of storing it directly in the
container's declared environment.

The simplest change that works with the image as built (no code or
`Dockerfile` change required) is to declare the secret and override the
`netnl` service's `command:` with a small shell wrapper that reads the
secret file into the environment and then execs `netnl-serve`. In
`deploy/compose.yaml` (or a `docker-compose.override.yaml` next to it, to
keep the default `env_file` path working for anyone who does not opt in):

```yaml
services:
  netnl:
    secrets:
      - netnl_upstream_password
    environment:
      # No longer set NETNL_UPSTREAM_PASSWORD here or in .env — it now
      # comes from the secret file at container start.
      NETNL_UPSTREAM_PASSWORD_FILE: /run/secrets/netnl_upstream_password
    command:
      - sh
      - -c
      - >-
        export NETNL_UPSTREAM_PASSWORD="$(cat "$NETNL_UPSTREAM_PASSWORD_FILE")" &&
        exec netnl-serve

secrets:
  netnl_upstream_password:
    file: ./secrets/netnl_upstream_password.txt   # gitignored, 0600, not the real password shown here
```

`NETNL_UPSTREAM_USERNAME` is not normally sensitive enough to need the
same treatment, but the identical pattern applies if you want it too
(a second secret, a second `_FILE` var, appended to the same `export`
line).

Assumptions this relies on:

- Local (non-Swarm) Compose file-based secrets are used, so no Swarm
  cluster is required — `file:` secrets work with plain `docker compose
  up`.
- The secret file itself (`./secrets/netnl_upstream_password.txt` above)
  is created by you on the host, kept out of git, and readable only by
  the user running `docker compose`; Compose mounts it read-only into the
  container at `/run/secrets/netnl_upstream_password`.
- The `netnl` image (`deploy/Dockerfile`, `python:3.12-slim`) has `/bin/sh`
  available, so the wrapper command runs without any image changes.
- This hardens against *inspecting the running container's declared
  environment* (`docker inspect`, `env_file`-sourced vars). It does not
  protect against someone with a shell inside the container or root on
  the host reading the process's live environment or `/run/secrets/*`
  directly — that level of isolation is out of scope for this recipe.

## Batch vs. website results

The facade passes through whatever the underlying batch instance
produces; the batch-vs-website differences documented in
[reference/self-hosted.md](../reference/self-hosted.md#batch-results-are-not-website-results)
apply unchanged (no connection test, no DNSSEC registrar lookup, no
hostname prechecks).

## Not an SLA

This is a homelab-grade recipe: one facade container, one edge proxy, one
SQLite file, cron-scheduled retention. It is not a managed service and
carries no uptime or support guarantee. It is also **not affiliated with,
endorsed by, or run by internet.nl or the Internet.nl project** — every
facade reply carries an `X-Netnl-Notice` header stating that
independence, and tenants should treat results accordingly.
