---
status: current
last_reviewed: 2026-08-23
---

# Deploying the netnl facade

The facade (`netnl-serve`, package `src/netnl/`) is a small public gateway
that fronts a **private** batch instance: it validates, rate-limits and
audits requests, then submits them upstream with a server-side credential
that tenants never see. This page deploys `deploy/compose.yaml` next to
(not inside) that instance's own stack. It does not replace
[Self-hosting a batch instance](../reference/self-hosted.md) — read that
first if the instance itself does not exist yet.

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

## 3. Bring the stack up

```sh
docker compose -f deploy/compose.yaml up -d
```

This starts `netnl` (the facade, no published port) and `edge` (Caddy,
publishing 80/443 and terminating TLS for `NETNL_PUBLIC_HOST`). The
facade's SQLite database lives on the named volume `netnl-data`, created
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
