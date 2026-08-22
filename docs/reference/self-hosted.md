---
status: current
last_reviewed: 2026-08-22
---

# Self-hosting a batch instance

Read this page *before* committing to a server. Two routes exist to the batch
API, and the cheap one may be enough: the hosted instance at
`batch.internet.nl` requires only an account (upstream: "Any activity on the
batch functionality requires a configured user"). Request one first —
self-host only when an account is refused or your volume warrants it. The CLI
is identical against both; only `INTERNETNL_ENDPOINT` changes.

## Requirements (from upstream's batch deployment guide)

| | Minimum | Recommended |
|---|---|---|
| CPU cores | 2 | 4 |
| Memory | 4 GB | 8 GB |
| Storage | 50 GB | 100 GB |

Plus: Ubuntu 22.04 LTS (or similar), root access, and — the one that
surprises people — **a fixed public IPv4 address on the primary interface,
and IPv6**.

### The addressing requirement, spelled out

The instance runs its own DNS components and performs measurements *from*
its address. This is therefore **not something you run behind NAT** on a
laptop or on a typical homelab with one shared public IP. Your options:

1. **A VPS or colocated machine** with its own public IPv4 + IPv6 — the
   straightforward route.
2. **A homelab machine only if** you can route a dedicated public IPv4
   address to that VM (e.g. a routed subnet or an extra address from your
   ISP) and provide native IPv6. Port-forwarding does not qualify.
3. **Neither?** Use the hosted batch API with an account.

## Deployment notes

Upstream documents the full procedure; follow it rather than a paraphrase:

- Deployment guide (Docker Compose):
  <https://github.com/internetstandards/Internet.nl/blob/main/documentation/Docker-deployment-batch.md>
- After the stack is up, create a batch user with upstream's
  `user_manage.sh`; that user's HTTP Basic credentials are what you put in
  `INTERNETNL_USERNAME` / `INTERNETNL_PASSWORD`.
- Point the CLI at your instance:
  `INTERNETNL_ENDPOINT=https://<your-host>/api/batch/v2`. That the CLI works
  unchanged against it — with only the endpoint variable altered — is the
  acceptance test for the client's "endpoint is configuration" rule.

## What it costs to keep running

Self-hosting means operating Internet.nl's **full stack**, including its DNS
components (the test suites depend on them). That is a maintained service,
not a script:

- OS and stack updates on a machine that is, by design, publicly addressable.
- Upstream releases change tests and scoring; an unmaintained instance
  silently drifts from what "Internet.nl" means today.
- Disk grows with result history; monitor the 50–100 GB budget.

If nobody on your team will own that, use the hosted instance.

## Batch results are not website results

Upstream lists the differences; they apply to the hosted batch instance and
to your own alike:

- the **connection test** is unavailable in batch mode;
- **DNSSEC** tests skip the registrar lookup;
- **no prechecks** run on whether a hostname has an A/AAAA record at all.

A batch verdict must never be quoted as "the internet.nl score" without
naming the endpoint that produced it — which is why the CLI stamps the
endpoint host, timestamp and API version on every result.
