# Project Context

## Purpose

A command-line client for the [Internet.nl](https://internet.nl) **batch API**,
plus a documented recipe for running your own batch instance.

Internet.nl tests a domain for IPv6, DNSSEC, RPKI, TLS and a set of
informational checks. The public site tests one domain at a time in a browser.
That is fine for a single site and useless for a fleet: there is no way to
measure fifty or two hundred hosts, diff today against last week, or fail a
pipeline when something regresses.

The batch API solves that, and it already exists. This project does not
reimplement any test — it is a client, and a deployment recipe for people who
cannot get an account on the hosted batch instance.

## Problem being solved

Three concrete gaps:

1. **No fleet view.** Checking many hosts means opening a browser many times,
   and the result lives in a screenshot instead of a file you can diff.
2. **Ad-hoc scripts drift from the real scoring.** A hand-rolled DNS/TLS check
   approximates the verdict and slowly diverges from what Internet.nl actually
   measures. It is also easy to get subtly wrong: a resolver failure that gets
   captured as a measurement value will happily report nonsense.
3. **Access.** The hosted batch API requires an account. Self-hosting is
   documented upstream but the requirements and the caveats are scattered.

## Scope

- CLI that talks to any Internet.nl batch API v2 endpoint — the hosted one or
  your own instance. The endpoint is configuration, never a constant.
- Deployment recipe for a self-hosted batch instance, with the requirements
  and the known differences from the website's results written down.

Out of scope: reimplementing the tests, building a dashboard (upstream has
`Internet.nl-dashboard`), and scraping the website UI.

## Tech stack

- Python via `uv` (never `pip` directly)
- `argparse`, standard library HTTP; no framework
- `pytest`, with an autouse fixture that isolates `$HOME`
- Deployment recipe targets Docker Compose, as upstream documents it

## Conventions

- Every limit, timeout, path and endpoint is environment-tunable. Nothing that
  a test or another deployment might need to change is hardcoded.
- Output is plain text by default and machine-readable with `--json`. No
  colours or emoji: this runs in CI.
- Secrets come from the environment. No credential is ever written to a file in
  this repo, printed, or logged.
- Measure only hosts you operate or have permission to test.

## Upstream sources

- Code and deployment docs: <https://github.com/internetstandards/Internet.nl>
- Batch API v2 spec: <https://batch.internet.nl/api/batch/openapi.yaml>
- Existing consumers, useful as reference:
  `internetstandards/Internet.nl-dashboard`,
  `poorting/internet.nl_batch_scripts`
