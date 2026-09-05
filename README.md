# internetnl-cli

A command-line client for the [Internet.nl](https://internet.nl) **batch
API**, plus a documented recipe for running your own batch instance.

Internet.nl tests a domain for IPv6, DNSSEC, RPKI, TLS and a set of
informational checks. The public site tests one domain at a time in a
browser; this tool submits a whole fleet to a batch API v2 endpoint, polls
until the run finishes, and renders the result as a diffable table or as JSON
for pipelines.

> **Status:** implemented. The user-visible surface stays pinned in
> [`openspec/changes/add-internetnl-cli/design.md`](openspec/changes/add-internetnl-cli/design.md);
> the commands below match the current behaviour.

**New here?** See the illustrated
[**how-it-works page**](https://claude.ai/code/artifact/82279ff8-2a68-43ee-a7b5-2d8fa0ebfa12)
(the whole flow, the commands, the deploy topology), or the runnable
[**internetnl-cli-demo**](https://github.com/MWest2020/internetnl-cli-demo)
(quickstart + example hosts + CI-gate example).

**Try it live, no account needed:**
[**https://mwest2020.github.io/internetnl-cli-demo/**](https://mwest2020.github.io/internetnl-cli-demo/)
— type a domain, get a real result, one run at a time (strictly
rate-limited; see [docs/reference/demo-api.md](docs/reference/demo-api.md)
for the contract that page relies on and
[docs/how-to/demo-run.md](docs/how-to/demo-run.md) for how it is run).

## The rule

**Only measure hosts you operate or have explicit permission to test.**
The tool does not enforce this; you do.

## Quickstart

```sh
export INTERNETNL_ENDPOINT=https://batch.internet.nl/api/batch/v2
export INTERNETNL_CREDENTIAL=you:…                     # hosted API needs an account; user:pass, split on the first ':'
internetnl submit --file hosts.txt                     # prints request-id, then polls
internetnl poll <request-id>                           # resume after a closed laptop
internetnl results <request-id> --json > results.json  # machine-readable
```

There is **no default endpoint**: you configure where you measure — the
hosted instance (account required) or [your own](docs/reference/self-hosted.md).
Switching between them is a change of `INTERNETNL_ENDPOINT`, nothing else.

## Getting a credential

Every endpoint needs one, and there are three routes:

1. **A supporter key for `api.westerweel.work`** — the netnl facade in front
   of an instance run by this project's author. A one-off donation via
   [Buy Me a Coffee](https://buymeacoffee.com/mark.westerweel) of $2 or more
   mints a lifetime credential and mails it to you automatically, usually
   within a minute. It is a beta service on a best-effort, no-SLA basis, with
   the same fair-use rate limit for everyone — read
   [docs/how-to/supporter-key.md](docs/how-to/supporter-key.md) for exactly
   what that promises and what it does not.
2. **An account on the hosted batch API** at
   [Internet.nl](https://batch.internet.nl), if your organisation qualifies
   for one.
3. **Your own instance** — see [Self-hosting](#self-hosting) below.

No credential at all is needed to try the tool: the
[live demo](https://mwest2020.github.io/internetnl-cli-demo/) runs one domain
at a time against the same facade, anonymously.

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
config path). `INTERNETNL_CREDENTIAL` is a single `username:password`
alternative to the username/password pair, split on the first `:`; set
either form, never both. Credentials come from the environment only —
never from command-line arguments, and they never appear in output or
logs.

## Use in CI

`internetnl` is meant to run in someone else's pipeline: `--json`,
stable exit codes (`0` ok, `1` config, `2` transport/API, `3`
`--fail-on-scored` gate, `4` poll timeout) and a bundled GitHub Action
([`action.yml`](action.yml)):

```yaml
- uses: MWest2020/internetnl-cli@main
  with:
    hosts: example.org
    endpoint: https://api.westerweel.work
    credential: ${{ secrets.INTERNETNL_CREDENTIAL }}
```

See [docs/how-to/ci.md](docs/how-to/ci.md) for the full input
reference, a plain-CLI recipe for other CI systems (GitLab CI et al.),
and the gate's exit-code semantics.

## Self-hosting

If you cannot get an account on the hosted batch instance, and a
[supporter key](#getting-a-credential) does not fit, you can run your own.
Read [docs/reference/self-hosted.md](docs/reference/self-hosted.md)
first: it needs a server with a **fixed public IPv4 address and IPv6** — not
something you run behind NAT — and it is a maintained service, not a script.
[docs/how-to/deploy-instance-vps.md](docs/how-to/deploy-instance-vps.md) walks
the whole deployment, including the two upstream traps that cost this project
days: a documented setting that silently kills all container egress, and a
broken certbot in the webserver image.

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
