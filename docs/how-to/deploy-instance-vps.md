---
status: current
last_reviewed: 2026-08-23
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

## 1. Provision the VPS and deploy the instance

Follow [reference/self-hosted.md](../reference/self-hosted.md) for the
machine sizing (2-4 CPU, 4-8 GB RAM, 50-100 GB disk) and the upstream
Docker Compose deployment guide:
<https://github.com/internetstandards/Internet.nl/blob/main/documentation/Docker-deployment-batch.md>.
Nothing about that procedure changes here — the VPS just happens to also
run Tailscale alongside it. Create a batch user with upstream's
`user_manage.sh` as usual; that credential becomes
`NETNL_UPSTREAM_USERNAME` / `NETNL_UPSTREAM_PASSWORD` for the facade.

## 2. Join the VPS to the tailnet

Install Tailscale on the VPS and bring it up:

```sh
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
```

Note the tailnet IPv4 address Tailscale assigns the machine (or give it a
stable [MagicDNS](https://tailscale.com/kb/1081/magicdns) name instead —
either works as the endpoint host below):

```sh
tailscale ip -4
```

The facade's Kubernetes node(s) (or, at minimum, the node the facade pod
runs on) must be on the same tailnet — join them the same way, or run
Tailscale as a sidecar/subnet-router per your cluster's networking setup;
that part is cluster-specific and out of scope here.

## 3. Do not publish the batch API publicly

The instance's own Compose stack must **not** expose the batch API's port
on the VPS's public interface. Bind it to `127.0.0.1` or to the
Tailscale interface only (check the upstream compose file's `ports:`
mapping), so the only way to reach `/api/batch/v2` on this VPS is over
the tailnet. Confirm from a host **outside** the tailnet that the batch
API port is unreachable — the same check topology 2's runbook
([deploy-facade.md](deploy-facade.md)) asks for with its internal docker
network, applied here to the tailnet boundary instead.

## 4. Point the homelab facade at the tailnet address

In the facade's configuration (the homelab ArgoCD app / K8s manifests
that carry `NETNL_UPSTREAM_ENDPOINT` and the credential, per
`openspec/changes/add-measurement-api/design.md`, "Configuration"), set:

```
NETNL_UPSTREAM_ENDPOINT=https://<vps-tailnet-address-or-magicdns-name>/api/batch/v2
NETNL_UPSTREAM_USERNAME=<batch credential username>
NETNL_UPSTREAM_PASSWORD=<batch credential password>
```

using the tailnet address (or MagicDNS name) from step 2 — never the
VPS's public IP. If the instance serves plain HTTP over the tailnet
(no TLS terminated on that internal hop), also set
`NETNL_ALLOW_HTTP=1`, matching the same internal-hop allowance the
co-located compose recipe documents.

## 5. Expose the facade, not the instance

The facade itself is what tenants reach publicly. In the K8s topology it
is exposed via **Tailscale Funnel** on the homelab cluster (Tailscale
terminates TLS and provides an `*.ts.net` hostname), not via the compose
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

## Not an SLA

As with the co-located recipe, this is a homelab-grade arrangement: one
VPS running the upstream stack, one tailnet hop, no managed-service
guarantee. See [deploy-facade.md](deploy-facade.md#not-an-sla) for the
same caveat as it applies to the facade side.
