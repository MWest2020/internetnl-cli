---
status: current
last_reviewed: 2026-09-03
---

# internetnl-cli documentation

A command-line client for the Internet.nl batch API, plus a recipe for
running your own batch instance. What the tool is (and is not) and the
quickstart live in the [README](../README.md); the pinned CLI surface —
commands, environment variables, exit codes — lives in
[`openspec/changes/add-internetnl-cli/design.md`](../openspec/changes/add-internetnl-cli/design.md)
until the implementation lands.

## How-to

- [Deploying the netnl facade](how-to/deploy-facade.md) — compose unit,
  edge TLS, credential issuance and the prune cron for the public facade
  in front of a private batch instance; also covers the two supported
  topologies (co-located, and facade-in-K8s over a tailnet).
- [Running the netnl demo](how-to/demo-run.md) — enabling the anonymous,
  single-domain `/demo/*` route family, issuing (and discarding the
  password of) its borrowed credential, the smoke check, and the kill
  switch.
- [Deploying the upstream instance on a VPS, reached over a tailnet](how-to/deploy-instance-vps.md) —
  the batch instance on a fixed-public-IP VPS, joined to a Tailscale
  tailnet so a homelab facade can reach it privately.
- [Self-hosting: four traps](how-to/self-hosting-pitfalls.md) — the places
  where following the upstream batch documentation literally still leaves you
  with a broken instance: the public-IP setting that kills all container
  egress, the certbot that cannot start so certificates never renew, the
  compose wrapper that needs a TTY, and the stranded-network race.
- [Running the netnl private beta](how-to/beta.md) — issuing and
  revoking tenant credentials, onboarding a handful of known beta
  users, what to observe against the default limits, and the
  acceptance script that gates going live.
- [Supporter keys](how-to/supporter-key.md) — the lifetime credential
  issued for a small donation: beta, best-effort, no SLA, with the
  per-tenant rate limit as the fair-use mechanism.
- [Automatic supporter-key issuance](how-to/supporter-webhook.md) — the
  Buy Me a Coffee webhook bridge that mints and mails a supporter key
  without an operator in the loop: enabling it, testing it, and
  troubleshooting a delivery that did not arrive.
- [Use in CI](how-to/ci.md) — the bundled GitHub Action, a plain-CLI
  recipe for other CI systems, and the gate's exit-code semantics.

## Reference

- [Self-hosting a batch instance](reference/self-hosted.md) — requirements,
  the fixed-public-IP caveat, deployment notes, running costs, and how batch
  results differ from the website's.
- [The demo API](reference/demo-api.md) — the page contract for `/demo/*`:
  endpoints, poll cadence, CORS requirements, id hygiene and the full error
  table with the literal, directly-showable visitor-facing messages.
