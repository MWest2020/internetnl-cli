---
status: current
last_reviewed: 2026-08-31
---

# Deploying the upstream instance on a VPS, reached over a tailnet

This page covers **topology 1** from
`openspec/changes/add-measurement-api/design.md`, "Two supported
topologies": the upstream batch instance runs on a VPS with a fixed
public IPv4 + IPv6, and the facade runs elsewhere (a homelab Kubernetes
cluster, in our case) and reaches it privately over a
[Tailscale](https://tailscale.com/) tailnet — never over the public
internet. Use this page together with
[reference/self-hosted.md](../reference/self-hosted.md), which covers the
instance's requirements and links the upstream Docker Compose deployment
guide; this page does not repeat that material, only the parts specific
to the VPS + tailnet arrangement.

## Why a VPS, and why a tailnet

The batch instance measures *from its own address* and runs its own DNS
components, so it needs a fixed public IPv4 **and** IPv6 on the host
itself (see self-hosted.md, "The addressing requirement, spelled out") —
something a NAT'd homelab cannot offer. A small VPS with a public
IPv4/IPv6 pair satisfies that requirement cheaply, without needing the
facade to live on the same public host. The facade still needs to reach
the instance's batch v2 API, but that API must **never** be public — the
facade is the only path a tenant gets. A tailnet gives a private,
authenticated, encrypted address between the two hosts without opening
any port on the VPS's public interface for the batch API itself.

## 1. Provisioning on Hetzner

This step creates the VPS host itself — Ubuntu, Docker CE + the compose
plugin, Tailscale, a hardened non-root user, a host firewall — using
`deploy/vps/cloud-init.yaml` and `deploy/vps/create-vps.sh`. It does
**not** deploy the Internet.nl batch stack; that stays upstream's guide,
covered in step 2 below.

### Prerequisites

- The [`hcloud` CLI](https://github.com/hetznercloud/cli), logged in
  against a Hetzner Cloud project of your own (`hcloud context create` /
  `HCLOUD_TOKEN` — this token is *your* project credential, never
  committed anywhere in this repo).
- An SSH key of yours registered in that Hetzner project (`hcloud
  ssh-key list`, or add one with `hcloud ssh-key create`), or the path
  to a local public key file — either works as `SSH_KEY_NAME` below.
- A Tailscale [pre-auth
  key](https://tailscale.com/kb/1085/auth-keys), created **ephemeral**,
  **tagged**, **single-use** (`reusable: false`) and with the
  **shortest TTL** the admin console allows (Tailscale admin console →
  Settings → Keys): ephemeral so the device is removed automatically if
  it's ever redeployed rather than piling up stale entries, tagged so
  it's governed by your tailnet ACL policy rather than a personal user
  identity, single-use + short-TTL because of the at-rest exposure
  described below. This becomes `TS_AUTHKEY` below — never commit it,
  and treat it like any other credential (it authorises a device to join
  your tailnet).

  **Tailscale auth key at-rest on the host.** `create-vps.sh` never
  writes the rendered cloud-init (with the key filled in) anywhere but a
  throwaway temp file on *your* machine, deleted immediately after the
  server is created. However, the *server itself* receives that same
  rendered cloud-init as its user-data, and cloud-init caches it,
  plaintext, on the host — at minimum
  `/var/lib/cloud/instance/user-data.txt`, and in cloud-init's own
  per-instance script/log copies under `/var/lib/cloud/instance/`. That
  cache is root-only (`0600`/`0640` depending on the file), but the
  `deploy` user has `NOPASSWD:ALL` sudo, so in practice anyone who can
  SSH in as `deploy` can read it.

  We deliberately do **not** try to have cloud-init scrub or shred its
  own cached user-data as a `runcmd` step: cloud-init keeps *more than
  one* copy of the same data during a run (for example, `runcmd` itself
  is first extracted into its own script file under
  `/var/lib/cloud/instance/scripts/`, separate from
  `user-data.txt`), so a single `rm`/`shred` step would not reliably get
  all of them, would need to run as the very last thing cloud-init does
  (or risk breaking later modules that still expect to read the cached
  data), and touching cloud-init's own bookkeeping files from inside a
  `runcmd` step is exactly the kind of fragile, easy-to-get-subtly-wrong
  change that turns "cleans up a secret" into "silently breaks
  provisioning or locks out the next `cloud-init` run." That trade-off
  isn't worth it here.

  The mitigation instead: make the key **single-use and short-TTL**, so
  by the time anyone reads the leftover plaintext copy, Tailscale has
  already consumed it and it cannot register another device. If you want
  the at-rest copy gone too, wipe it yourself once you've confirmed the
  host joined the tailnet (for example `ssh deploy@<host> sudo shred -u
  /var/lib/cloud/instance/user-data.txt`) — that is a manual, deliberate
  step outside cloud-init's own execution, not automated by this recipe.

### Create the VPS

```sh
HCLOUD_TOKEN=<your Hetzner project token> \
TS_AUTHKEY=<ephemeral, tagged, single-use, short-TTL Tailscale pre-auth key> \
SSH_KEY_NAME=<hcloud SSH key name, or a path to a local .pub file> \
deploy/vps/create-vps.sh --yes
```

Drop `--yes` to get an interactive confirmation prompt instead — this
creates a **billable** Hetzner resource (a `cx22` by default, roughly
€3–5/month; see `VPS_TYPE`/`VPS_LOCATION`/`VPS_IMAGE`/`VPS_NAME` in the
script's header if you want a different size or region). The script
never provisions anything without either `--yes` or that confirmation.
It prints the server's public IPv4, public IPv6 and its tailnet
hostname (`VPS_NAME`, `netnl-instance` by default — the same value also
becomes the host's OS hostname and its `tailscale up --hostname=`, so
all three are guaranteed to match) once done; wait for cloud-init to
finish on the host (a minute or two) before continuing — `cloud-init
status --wait` over SSH as the `deploy` user confirms it.

What this buys you, concretely: Docker CE + the compose plugin and
Tailscale installed from their official apt repositories (no `curl |
sh`), a non-root `deploy` user with your SSH key and no root login left
open, `PasswordAuthentication no` and `KbdInteractiveAuthentication no`
(only your SSH key gets in), a `ufw` firewall that default-denies
inbound and only opens SSH (rate-limited) and the Tailscale UDP port,
and the host already joined to your tailnet. See
`deploy/vps/cloud-init.yaml` for the exact detail, and
`openspec/changes/add-measurement-api/design.md`, "VPS provisioning
(Hetzner) — for topology 1", for why each piece is there.

The instance's batch API is **not** opened publicly by any of this —
only ports needed for the batch stack itself (see step 2) go in the
clearly-marked "Internet.nl public ports" block in
`deploy/vps/cloud-init.yaml`'s firewall rules, and even those are left
commented out until you confirm the exact set against upstream's guide.

## 2. Deploy the Internet.nl batch stack

The VPS from step 1 is a provisioned, Tailscale-joined host — nothing
Internet.nl-specific runs on it yet. Follow
[reference/self-hosted.md](../reference/self-hosted.md) for the sizing
context and the upstream Docker Compose deployment guide:
<https://github.com/internetstandards/Internet.nl/blob/main/documentation/Docker-deployment-batch.md>.
This part is entirely upstream's guide, not reproduced here — it covers
the instance's own `.env`, DNS delegation for the domains it will
measure, and bringing up its compose stack. A `/opt/internetnl-batch/`
directory with a pointer back to that guide is already there, left by
cloud-init, as a landing spot (not a reproduction of the stack itself).
Create a batch user with upstream's `user_manage.sh` as usual; that
credential becomes `NETNL_UPSTREAM_USERNAME` / `NETNL_UPSTREAM_PASSWORD`
for the facade in step 5.

## 3. Confirm the tailnet address

The VPS already joined your tailnet during step 1 (`tailscale up` runs
as part of cloud-init). Note its tailnet IPv4 address, or give it a
stable [MagicDNS](https://tailscale.com/kb/1081/magicdns) name instead —
either works as the endpoint host in step 5:

```sh
ssh deploy@<public-ipv4-from-step-1> -- tailscale ip -4
```

The facade's Kubernetes node(s) (or, at minimum, the node the facade pod
runs on) must be on the same tailnet — join them the same way, or run
Tailscale as a sidecar/subnet-router per your cluster's networking setup;
that part is cluster-specific and out of scope here.

## 4. Do not publish the batch API publicly

The instance's own Compose stack must **not** expose the batch API's port
on the VPS's public interface. Bind it to `127.0.0.1` or to the
Tailscale interface only (check the upstream compose file's `ports:`
mapping), so the only way to reach `/api/batch/v2` on this VPS is over
the tailnet. Confirm from a host **outside** the tailnet that the batch
API port is unreachable — the same check topology 2's runbook
([deploy-facade.md](deploy-facade.md)) asks for with its internal docker
network, applied here to the tailnet boundary instead.

## 5. Wire the facade to the tailnet address

This is the only step that touches the facade side (homelab Kubernetes),
not the VPS. It has two parts: a Secret carrying the batch credential
from step 2, and a ConfigMap entry pointing at the VPS's tailnet
address from step 3.

Create (or update) the `netnl-upstream` Secret from the batch user you
created in step 2 — the same credential pair, never the facade's own
tenant credentials:

```sh
kubectl -n netnl create secret generic netnl-upstream \
  --from-literal=NETNL_UPSTREAM_USERNAME=<batch credential username> \
  --from-literal=NETNL_UPSTREAM_PASSWORD=<batch credential password> \
  --dry-run=client -o yaml | kubectl apply -f -
```

(`--dry-run=client -o yaml | kubectl apply -f -` so re-running this
after a credential rotation updates the Secret in place, instead of
`kubectl create` failing because it already exists.)

Then set `NETNL_UPSTREAM_ENDPOINT` in the homelab `netnl-config`
ConfigMap (the same one referenced in
[beta.md](beta.md#what-to-observe-during-the-beta-and-how-to-act-on-it),
from
[MWest2020/homelab#13](https://github.com/MWest2020/homelab/pull/13)) to
the VPS's tailnet address or MagicDNS name from step 3 — **never** its
public IP:

```
NETNL_UPSTREAM_ENDPOINT=https://<vps-tailnet-address-or-magicdns-name>/api/batch/v2
```

If the instance serves plain HTTP over the tailnet (no TLS terminated on
that internal hop), also set `NETNL_ALLOW_HTTP=1` in the same
ConfigMap, matching the same internal-hop allowance the co-located
compose recipe documents. Roll the facade deployment (or let ArgoCD
sync) to pick up both changes.

## 6. Expose the facade, not the instance

The facade itself is what tenants reach publicly. In the K8s topology it
is exposed via **two parallel public paths**: a **Cloudflare Tunnel** on a
branded hostname (`https://api.westerweel.work`, primary — decided
2026-08-31, owner Mark) and the **Tailscale Funnel** `*.ts.net` hostname
(fallback, kept up in parallel). Neither uses the compose
`Caddyfile`/`edge` service — that belongs only to the co-located topology.
The VPS in this runbook never needs a public path to its batch API at
all; only the facade, running elsewhere, needs a private path to it.

## Acceptance check

From the homelab facade (or a pod on the same cluster/tailnet), confirm
the tailnet path works before wiring up tenants:

```sh
curl -u "$NETNL_UPSTREAM_USERNAME:$NETNL_UPSTREAM_PASSWORD" \
  "$NETNL_UPSTREAM_ENDPOINT/metadata/report"
```

and, from a host outside the tailnet, confirm the same request against
the VPS's **public** address fails (connection refused/timeout) — the
batch API must have no public listener.

Finally, run [`scripts/acceptance.sh`](../../scripts/acceptance.sh)
(task 4.2) against the live facade — it exercises the unmodified
`internetnl` CLI's `submit`/`results` against the facade and, optionally
(`NETNL_INSTANCE_PROBE_URL`), confirms the instance itself is
unreachable from outside the tailnet. See the script's own header
comment for the required environment, and
[beta.md](beta.md#gono-go) for how this gates opening the beta.

## Not an SLA

As with the co-located recipe, this is a homelab-grade arrangement: one
VPS running the upstream stack, one tailnet hop, no managed-service
guarantee. See [deploy-facade.md](deploy-facade.md#not-an-sla) for the
same caveat as it applies to the facade side.
