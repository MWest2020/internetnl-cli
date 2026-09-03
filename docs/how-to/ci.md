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
    credential: ${{ secrets.INTERNETNL_CREDENTIAL }}
```

`credential` is a single `username:password` string, split on the
*first* colon (a password may contain one; the username charset never
does). It is the preferred way to pass a credential to the action: one
secret instead of two. `username`/`password` still work as separate
inputs if you already have the credential split that way:

```yaml
- uses: MWest2020/internetnl-cli@main
  with:
    hosts: example.org example.com
    endpoint: https://api.westerweel.work
    username: ${{ secrets.INTERNETNL_USERNAME }}
    password: ${{ secrets.INTERNETNL_PASSWORD }}
```

Provide exactly one of the two forms — `credential`, or both `username`
and `password` — never a mix; the action's "Validate inputs" step fails
closed otherwise.

Pin `@main` to a tag or commit SHA once one exists, the same way this
repo pins its own third-party actions (see `.github/workflows/ci.yml`).
Never put the password (or the combined credential) directly in the
workflow file — always a secret.

Note the trade-off while there is no tag/SHA to pin yet: the action
resolves its own install source from `github.action_repository`/
`github.action_ref`, which are only populated when consumed as `uses:
owner/repo@ref` from another repo. A local `uses: ./` (dogfooding this
action from its own repo, e.g. in the smoke workflow) falls back to
installing from this repo's `main` branch, so that path always tracks
`main` regardless of what you pin `uses:` to elsewhere. Pinning to a
tag or SHA fixes what code the *step definition* runs, but does not
change this fallback's behaviour when it triggers.

Inputs:

| Input | Required | Default | Meaning |
|---|---|---|---|
| `hosts` | one of `hosts`/`file` | — | space-separated hostnames |
| `file` | one of `hosts`/`file` | — | path to a file, one hostname per line |
| `type` | no | `web` | `web` or `mail` |
| `fail-on-scored` | no | `true` | gate on scored subtest failures (exit 3); must be exactly `true` or `false`, anything else fails the step |
| `name` | no | — | free-form label for the request |
| `allowlist` | no | — | workspace-relative path to an allowlist file (see (c) below); requires `actions/checkout` to have run first |
| `endpoint` | **yes** | — | `INTERNETNL_ENDPOINT` |
| `credential` | one of `credential`/(`username`+`password`) | — | `INTERNETNL_CREDENTIAL`, `username:password` (split on the first `:`), from a secret; preferred |
| `username` | one of `credential`/(`username`+`password`) | — | `INTERNETNL_USERNAME`; alternative to `credential`, pair with `password` |
| `password` | one of `credential`/(`username`+`password`) | — | `INTERNETNL_PASSWORD`, from a secret; alternative to `credential`, pair with `username` |

Output: `results-path`, the path to the raw `--json` results file
written during the run — attach it as a build artifact if you want it
kept:

```yaml
- uses: actions/upload-artifact@v4
  if: always() && steps.<step-id>.outputs.results-path != ''
  with:
    name: internetnl-results
    path: ${{ steps.<step-id>.outputs.results-path }}
```

Give the action step an `id:` to reference its outputs, and use
`if: always()` on the upload step so a result still gets uploaded when
the gate trips the job (that is the point of the gate: it is supposed
to fail the job, not hide the evidence). The `&&
steps.<step-id>.outputs.results-path != ''` guard is explained below —
it is not optional decoration.

Three things to know about that artifact recipe.

`results-path` can point at a **0-byte file** on exit codes 1, 2, or 4
— the CLI redirects its own stdout straight to that path, so a run
that fails before producing JSON (a config error, a transport/API
error, or the poll timeout) still creates the file, just empty; do not
assume a present file means usable JSON.

The `path:` given to `upload-artifact` above is workspace-relative
like everything else `actions/upload-artifact` handles — `${{
steps.<step-id>.outputs.results-path }}` itself resolves to an
absolute path under `$RUNNER_TEMP` (set by the action, not the
workspace), so this works regardless of whether `actions/checkout`
ran. `actions/checkout` is still required, though: the `allowlist` and
`file` inputs above are both workspace-relative paths, so either one
needs the workspace populated by `actions/checkout` first to resolve.

And `results-path` is only ever set by the action's "Run internetnl
submit" step, so it stays **unset** (empty string) if an earlier step
— "Validate inputs" (e.g. a `hosts`/`file` conflict or an invalid
`fail-on-scored`) or "Install uv"/the install step — fails first. A
bare `if: always()` upload step given that empty `path:` errors ("no
files were found"/invalid path) instead of silently skipping, which is
why the recipe above guards on `steps.<step-id>.outputs.results-path
!= ''` as well.

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
/ `INTERNETNL_USERNAME` / `INTERNETNL_PASSWORD`, or the single
`INTERNETNL_CREDENTIAL` in `username:password` form — never both at
once) — never command-line arguments. Mark the username/password (or
the combined credential) as masked (and protected, if your CI supports
it) CI/CD variables, the same way you would for any other credential.

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

Exit code 4 is the one most likely to bite a large run: the default
`INTERNETNL_POLL_MAX` is 3600 seconds (one hour), and a batch of many
hosts can legitimately take longer than that to finish scoring. The
composite action's job-level (or workflow-level) `env:` block passes
through unchanged to its `run:` steps — GitHub Actions does not
sandbox a composite action's environment from the calling job's — so
setting `INTERNETNL_POLL_MAX` (raise the timeout), `INTERNETNL_POLL_INTERVAL`
(poll less often), or `INTERNETNL_BATCH_SIZE` (cap how many hosts one
request may contain, forcing you to split a large host list across
multiple `submit` calls/jobs) on the job that calls `uses:
MWest2020/internetnl-cli@main` reaches the action's own `internetnl`
invocation:

```yaml
jobs:
  scan:
    runs-on: ubuntu-latest
    env:
      INTERNETNL_POLL_MAX: "7200"
    steps:
      - uses: MWest2020/internetnl-cli@main
        with: { ... }
```

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

Every route needs `INTERNETNL_ENDPOINT` and a credential — either
`INTERNETNL_USERNAME` / `INTERNETNL_PASSWORD`, or the single
`INTERNETNL_CREDENTIAL` (`username:password`, split on the first `:`).
Options, cheapest first:

- An account on the hosted `batch.internet.nl` API (ask upstream).
- A credential on the `netnl` facade at `https://api.westerweel.work`:
  see [Running the netnl private beta](beta.md) for how the beta
  works today, and [Supporter keys](supporter-key.md) for the
  lifetime-key-for-a-donation route.
- [Self-hosting](../reference/self-hosted.md) your own batch instance,
  if your volume or requirements warrant it.
