---
status: current
last_reviewed: 2026-08-22
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
  in front of a private batch instance.

## Reference

- [Self-hosting a batch instance](reference/self-hosted.md) — requirements,
  the fixed-public-IP caveat, deployment notes, running costs, and how batch
  results differ from the website's.
