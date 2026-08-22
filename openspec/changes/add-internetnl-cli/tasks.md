# Tasks: add-internetnl-cli

## 0. Access — do this first, it may change everything

- [x] 0.1 ~~Request a batch account on the hosted instance~~ — decided
      2026-08-22 (Mark): accounts are a bottleneck; we go self-hosted and
      expose our own API, hosted-account route dropped
- [ ] 0.2 Read the v2 spec (`https://batch.internet.nl/api/batch/openapi.yaml`)
      and write down the four calls we need: submit, status, results, delete
- [ ] 0.3 Skim `poorting/internet.nl_batch_scripts` for the request/response
      shapes before writing a line — cheaper than discovering them from 400s

If 0.1 is granted, section 4 becomes optional rather than blocking.

## 1. Skeleton

- [x] 1.1 `uv` project, `pyproject.toml`, console entry point `internetnl`
- [x] 1.2 Config resolution: environment first, then config file, then error —
      endpoint and credentials, no defaults pointing anywhere
- [x] 1.3 `pytest` with an autouse fixture that repoints `$HOME` at a tmp dir
- [ ] 1.4 CI that runs the suite and the HOME-isolation check

## 2. Client

- [ ] 2.1 `submit` — hosts from `--file`/arguments, prints the request id first,
      then polls
- [ ] 2.2 `poll <id>` — resumes any run, including one this machine did not
      start
- [ ] 2.3 `results <id>` — renders finished results
- [ ] 2.4 Poll loop with tunable interval and maximum duration; reports status
      instead of guessing when a run is unfinished
- [x] 2.5 Errors carry status and endpoint host, never the credential; verify
      with a test that greps the captured output for the secret

## 3. Output and gating

- [ ] 3.1 Table renderer: one row per host, plain text, no colour
- [ ] 3.2 `--json`: one document on stdout, progress on stderr
- [ ] 3.3 Endpoint host, run timestamp and API version on every result
- [ ] 3.4 `--fail-on-scored` with an allowlist file; exit codes documented
- [ ] 3.5 Unknown or missing subtest renders as unknown, never as passing —
      with a test that feeds a response with a subtest removed

## 4. Self-hosted instance

- [x] 4.1 Deployment page: upstream requirements (2/4/50 minimum, 4/8/100
      recommended, Ubuntu 22.04, root, fixed public IPv4 on the primary
      interface, IPv6), and what it costs to keep running
      (`docs/reference/self-hosted.md`)
- [ ] 4.2 Compose/deploy notes following upstream's batch deployment guide,
      including creating a batch user with their `user_manage.sh`
- [x] 4.3 Document the batch-vs-website differences on the same page: no
      connection test, DNSSEC without registrar lookup, no A/AAAA prechecks
- [ ] 4.4 Run the CLI against the own instance unchanged, with only the
      endpoint variable altered — this is the acceptance test for section 1.2

## 5. Repo hygiene

- [x] 5.1 README: what it is, what it is not, the "only measure hosts you
      operate" rule, and a five-line quickstart
- [x] 5.2 CHANGELOG from the first commit
- [x] 5.3 Licence (MIT), and a note that this is a client — the tests belong
      to Internet.nl and are theirs
