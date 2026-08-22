# internetnl-cli

A command-line client for the [Internet.nl](https://internet.nl) **batch
API**, plus a documented recipe for running your own batch instance.

Internet.nl tests a domain for IPv6, DNSSEC, RPKI, TLS and a set of
informational checks. The public site tests one domain at a time in a
browser; this tool submits a whole fleet to a batch API v2 endpoint, polls
until the run finishes, and renders the result as a diffable table or as JSON
for pipelines.

> **Status:** the interface is fully specified
> (`openspec/changes/add-internetnl-cli/`, surface pinned in `design.md`);
> the implementation is being built through the
> [habitat](https://github.com/MWest2020/habitat) agent chain. Until the
> change lands, the commands below describe the pinned design.

## The rule

**Only measure hosts you operate or have explicit permission to test.**
The tool does not enforce this; you do.

## Quickstart

```sh
export INTERNETNL_ENDPOINT=https://batch.internet.nl/api/batch/v2
export INTERNETNL_USERNAME=you INTERNETNL_PASSWORD=…   # hosted API needs an account
internetnl submit --file hosts.txt                     # prints request-id, then polls
internetnl poll <request-id>                           # resume after a closed laptop
internetnl results <request-id> --json > results.json  # machine-readable
```

There is **no default endpoint**: you configure where you measure — the
hosted instance (account required) or [your own](docs/reference/self-hosted.md).
Switching between them is a change of `INTERNETNL_ENDPOINT`, nothing else.

## What it is, and is not

- **A client.** The tests, the scoring and the verdicts belong to
  Internet.nl and run on the endpoint you point at. Nothing is measured,
  approximated or filled in locally: if the API is unreachable, the answer is
  an error, not a guess.
- **A pipeline gate.** `--fail-on-scored` exits non-zero when a subtest that
  counts toward the score fails, with an allowlist file for accepted
  exceptions. Informational findings are shown but never gate.
- **Honest.** Every result carries the endpoint host, the run timestamp and
  the API version. A subtest missing from a response renders as *unknown*,
  never as passing.
- **Not a dashboard** (upstream has
  [Internet.nl-dashboard](https://github.com/internetstandards/Internet.nl-dashboard)),
  **not a scraper** of the website UI, and **not a reimplementation** of any
  test.

## Batch results are not website results

Upstream documents the differences: the connection test is unavailable,
DNSSEC tests skip the registrar lookup, and no prechecks run on whether a
hostname resolves at all. Never quote a batch verdict as "the internet.nl
score" without naming the endpoint — which is why every result is labelled.

## Configuration

Everything is environment-tunable; see
[`openspec/changes/add-internetnl-cli/design.md`](openspec/changes/add-internetnl-cli/design.md)
for the full table (`INTERNETNL_ENDPOINT`, `INTERNETNL_USERNAME`,
`INTERNETNL_PASSWORD`, timeouts, poll interval and maximum, batch size,
config path). Credentials come from the environment only — never from
command-line arguments, and they never appear in output or logs.

## Self-hosting

If you cannot get an account on the hosted batch instance, you can run your
own. Read [docs/reference/self-hosted.md](docs/reference/self-hosted.md)
first: it needs a server with a **fixed public IPv4 address and IPv6** — not
something you run behind NAT — and it is a maintained service, not a script.

## Development

- Python via [`uv`](https://docs.astral.sh/uv/), standard library only;
  `uv run pytest` runs the suite (no test touches the network or your real
  `$HOME`).
- The repo is built through the habitat agent chain: role definitions live in
  `.claude/agents/`, and `scripts/verify.sh` is the non-negotiable gate a
  builder run must pass.

## License

[MIT](LICENSE). This licence covers the client only: the tests, the scoring
and the Internet.nl name belong to the
[Internet.nl / Platform Internetstandaarden](https://github.com/internetstandards/Internet.nl)
project and are theirs.
