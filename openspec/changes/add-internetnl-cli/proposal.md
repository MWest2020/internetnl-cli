# Change: add-internetnl-cli

## Why

Measuring a fleet against Internet.nl currently means a browser and a human.
That does not scale past a handful of hosts, produces no artefact you can diff,
and cannot gate a pipeline.

The batch API exists and is documented, so the work is a client — not a test
suite. Writing your own DNS/TLS checks instead is the tempting shortcut and the
wrong one: it approximates the scoring, drifts from it silently, and is easy to
get wrong in ways that look like data. A measurement tool that reports a
resolver timeout as if it were a result is worse than no tool.

Two access routes exist, and the same client must serve both. The hosted batch
API needs an account (upstream: "Any activity on the batch functionality
requires a configured user", authorised by HTTP Auth on the webserver in front).
Self-hosting is documented and needs a modest server. Which route you take is a
deployment decision, not a code decision.

## What Changes

**1. `internetnl` CLI.** Submits a list of hosts to a batch API v2 endpoint,
polls until the run finishes, and renders the result.

- Endpoint and credentials come from the environment or a config file.
- Input from a file or arguments; output as a table, or `--json` for pipelines.
- `--fail-on-scored` exits non-zero when a subtest that counts toward the score
  fails, with an allowlist file for accepted exceptions — so it can be a gate.
- The request id is printed at submit time and accepted by a `poll` subcommand,
  so a long run survives a closed laptop.
- Every result records which endpoint produced it, with a timestamp and the API
  version.

**2. Self-hosted batch instance recipe.** Documented, reproducible, and honest
about what it costs and where its results differ from the website.

Requirements, from upstream's batch deployment guide: minimum 2 cores, 4 GB
memory, 50 GB storage; recommended 4 cores, 8 GB, 100 GB; Ubuntu 22.04 LTS or
similar; root access; **a fixed public IPv4 address on the primary interface,
and IPv6**. The address requirement is the one that surprises people — this is
not something you run behind NAT on a laptop.

## Non-goals

- **Not reimplementing the tests.** No local approximation of the scoring. If
  the API is unreachable, the answer is an error, not a guess.
- **No dashboard or web UI.** Upstream has `Internet.nl-dashboard` for that.
- **No scraping of the website UI.** It is not an interface, and treating it as
  one breaks on their next release.
- **No bundled credentials, no default endpoint pointing at someone else's
  instance.** You configure where you measure.

## Rollout

Deliberately in this order, because the cheap route may be enough:

1. **Client first, against the hosted API.** Request an account; if granted,
   the client is the whole solution and no server is needed.
2. **Self-hosted instance second**, only when an account is refused or the
   volume warrants it.

The client must be usable before any server exists, and unchanged after one
does. That is the test of whether the endpoint is really configuration.

## Impact

- **Batch results are not identical to website results.** Upstream lists the
  differences: the connection test is unavailable, DNSSEC tests skip the
  registrar lookup, and no prechecks run on whether a hostname has an A/AAAA
  record at all. A batch verdict must therefore never be presented as "the
  internet.nl score" without naming the endpoint — hence the endpoint label on
  every result.
- **Self-hosting means running their full stack**, including its DNS
  components. That is a maintained service, not a script. The recipe says so.
- **Testing hosts you do not operate is rude at best.** The README states the
  rule; the tool does not enforce it.
- Risk: low. Read-only measurement, no production system in the path.
