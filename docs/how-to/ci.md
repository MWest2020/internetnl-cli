---
status: current
last_reviewed: 2026-09-03
---

# Use in CI

`internetnl` was built for pipelines from the start: `--json` for
machine-readable output, `--fail-on-scored` for a gate, and stable exit
codes. This page covers running it from someone else's CI — a GitHub
Actions workflow via the bundled composite action, or any other CI
system by installing the CLI directly.

## a) GitHub Actions

The repository root ships [`action.yml`](../../action.yml), a composite
action. Minimal use:

```yaml
- uses: MWest2020/internetnl-cli@main
  with:
    hosts: example.org example.com
    endpoint: https://api.westerweel.work
    username: ${{ secrets.INTERNETNL_USERNAME }}
    password: ${{ secrets.INTERNETNL_PASSWORD }}
```

Pin `@main` to a tag or commit SHA once one exists, the same way this
repo pins its own third-party actions (see `.github/workflows/ci.yml`).
Never put the password directly in the workflow file — always a secret.

Inputs:

| Input | Required | Default | Meaning |
|---|---|---|---|
| `hosts` | one of `hosts`/`file` | — | space-separated hostnames |
| `file` | one of `hosts`/`file` | — | path to a file, one hostname per line |
| `type` | no | `web` | `web` or `mail` |
| `fail-on-scored` | no | `true` | gate on scored subtest failures (exit 3) |
| `name` | no | — | free-form label for the request |
| `endpoint` | **yes** | — | `INTERNETNL_ENDPOINT` |
| `username` | **yes** | — | `INTERNETNL_USERNAME` |
| `password` | **yes** | — | `INTERNETNL_PASSWORD`, from a secret |

Output: `results-path`, the path to the raw `--json` results file
written during the run — attach it as a build artifact if you want it
kept:

```yaml
- uses: actions/upload-artifact@v4
  if: always()
  with:
    name: internetnl-results
    path: ${{ steps.<step-id>.outputs.results-path }}
```

Give the action step an `id:` to reference its outputs, and use
`if: always()` on the upload step so a result still gets uploaded when
the gate trips the job (that is the point of the gate: it is supposed
to fail the job, not hide the evidence).

## b) Any other CI (plain CLI)

The action is a thin wrapper; anything that can run `uv` (or install a
Python package and set environment variables) can do the same thing
directly. GitLab CI example:

```yaml
scan:
  image: python:3.12-slim
  variables:
    INTERNETNL_ENDPOINT: https://api.westerweel.work
    INTERNETNL_USERNAME: $INTERNETNL_USERNAME   # CI/CD variable, masked
    INTERNETNL_PASSWORD: $INTERNETNL_PASSWORD   # CI/CD variable, masked+protected
  script:
    - pip install "git+https://github.com/MWest2020/internetnl-cli@main"
    - internetnl submit --file hosts.txt --fail-on-scored --json > results.json
  artifacts:
    when: always
    paths: [results.json]
```

Credentials are still environment variables only (`INTERNETNL_ENDPOINT`
/ `INTERNETNL_USERNAME` / `INTERNETNL_PASSWORD`) — never command-line
arguments. Mark the username/password as masked (and protected, if your
CI supports it) CI/CD variables, the same way you would for any other
credential.

## c) The gate: exit codes and what "scored" means

`internetnl submit`/`poll`/`results` return one of these exit codes
(the full table is in
[`openspec/changes/add-internetnl-cli/design.md`](../../openspec/changes/add-internetnl-cli/design.md#exit-codes)):

| Code | Meaning |
|---|---|
| 0 | Success (including `results` on a run that has not finished yet) |
| 1 | Configuration error (missing endpoint, unreadable file, bad allowlist) |
| 2 | Usage error, transport failure, or an API error |
| 3 | `--fail-on-scored` gate tripped |
| 4 | `INTERNETNL_POLL_MAX` exceeded while the run was still unfinished |

"Scored" means a subtest whose Internet.nl API status is `failed` —
Internet.nl reserves `failed` for problems that count toward the
published score; purely informational findings surface as `warning` or
`info` and are shown in the output but never change the exit code.

An accepted, tracked exception is an **allowlist file**, not a
disabled gate: `--allowlist path/to/file` takes `host testname` pairs
(whitespace-separated, `#` comments allowed). A listed failure still
shows up in the output as "accepted" — it just does not trip exit code
3. This is for a specific, named, tracked exception (e.g. "we know
`example.org` fails `web_tls_...` until the certificate is renewed
next week"), not a way to silence a whole class of findings.

A subtest the API omitted for a host renders as **unknown**, never as
passing — a gap in the response is not silent success.

## d) The rule

**Only measure hosts you operate or have explicit permission to
test.** This applies in CI exactly as it does on a laptop: a workflow
that runs on every pull request from outside contributors, or that
lets a PR body or branch name influence which hosts get submitted, can
turn "scan my own site" into "scan whatever a stranger asks for." The
tool does not enforce this — you do, by keeping the host list under
your own control (a file committed to the repo, not something derived
from untrusted input).

## e) Getting a credential

Every route needs `INTERNETNL_ENDPOINT` / `INTERNETNL_USERNAME` /
`INTERNETNL_PASSWORD`. Options, cheapest first:

- An account on the hosted `batch.internet.nl` API (ask upstream).
- A credential on the `netnl` facade at `https://api.westerweel.work`:
  see [Running the netnl private beta](beta.md) for how the beta
  works today, and [Supporter keys](supporter-key.md) for the planned
  lifetime-key-for-a-donation route once it opens beyond the beta's
  known users.
- [Self-hosting](../reference/self-hosted.md) your own batch instance,
  if your volume or requirements warrant it.
