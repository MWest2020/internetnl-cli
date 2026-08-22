# Design: add-internetnl-cli

Pins the user-visible surface of the CLI so the README, the tests and the
implementation cannot drift apart. The builder implements exactly this; any
deviation is a reported deviation, not a silent choice.

## Configuration

Resolution order: environment first, then config file, then a hard error that
names the variable to set. No default endpoint exists anywhere.

| Variable | Default | Meaning |
|---|---|---|
| `INTERNETNL_ENDPOINT` | — (required) | Batch API v2 base URL, e.g. `https://batch.internet.nl/api/batch/v2` |
| `INTERNETNL_USERNAME` | empty | HTTP Basic auth user (hosted instance requires it) |
| `INTERNETNL_PASSWORD` | empty | HTTP Basic auth password; environment only, never an argument |
| `INTERNETNL_CONFIG` | `$HOME/.config/internetnl/config.ini` | Config file path |
| `INTERNETNL_TIMEOUT` | `30` | HTTP timeout, seconds |
| `INTERNETNL_POLL_INTERVAL` | `30` | Seconds between status checks |
| `INTERNETNL_POLL_MAX` | `3600` | Maximum total polling time, seconds |
| `INTERNETNL_BATCH_SIZE` | `5000` | Maximum hosts per submit |

The config file (INI, section `[internetnl]`) may hold `endpoint` only.
Credentials come exclusively from the environment.

## Commands

```
internetnl [--debug] submit  [HOST ...] [--file FILE] [--type {web,mail}] [--name NAME] [--no-poll] [COMMON]
internetnl [--debug] poll    REQUEST_ID [COMMON]
internetnl [--debug] results REQUEST_ID [COMMON]

COMMON: --json --fail-on-scored --allowlist FILE
```

- `submit` reads hosts from `--file` (one per line, `#` comments) and/or
  arguments, deduplicates preserving order, POSTs one batch request, prints
  `request-id: <id>` to **stderr** before anything else, then polls unless
  `--no-poll`.
- `poll` resumes any run by id, including one another machine started, and
  renders results when the run reaches `done`.
- `results` is one-shot: a finished run renders; an unfinished run reports its
  status and exits `0` without inventing partial verdicts.
- All progress output goes to stderr. With `--json`, stdout carries exactly one
  JSON document.
- `--debug` prints each request as `> METHOD URL` to stderr. The Authorization
  header is never printed anywhere, in any mode.

## API mapping (batch API v2)

Ground truth is the vendored upstream spec,
[`reference/openapi.yaml`](reference/openapi.yaml) (v2.6.0, fetched
2026-08-22 from `https://batch.internet.nl/api/batch/openapi.yaml`).

`POST {endpoint}/requests` (body `{"type", "domains", "name"?}`),
`GET {endpoint}/requests/{id}`, `GET {endpoint}/requests/{id}/results`.
Auth via an `Authorization: Basic` header — never credentials in the URL.
Request statuses `registering|running|generating` keep polling; `done`
fetches results; `error|cancelled` is a hard error. Domain statuses are
`ok|error`; an errored domain has no results and renders as such. Test and
category statuses are `passed|failed|info|warning|not_tested|error` (tests)
and `passed|failed|info|warning|error` (categories); upstream defines
`failed` as "failure on a **required** test", which is what makes it the
gate. Every API reply carries a required top-level `api_version` field —
that is the version stamped on output (fallback `unknown` only for a
non-conforming server).

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Success, including `results` on an unfinished run |
| 1 | Configuration error (missing endpoint, unreadable file, bad allowlist) |
| 2 | Usage error, transport failure, or API error |
| 3 | `--fail-on-scored` gate tripped |
| 4 | `INTERNETNL_POLL_MAX` exceeded while the run was still unfinished |

## Gating semantics

- A subtest **gates** when its API status is `failed` (Internet.nl uses
  `failed` only for score-relevant problems; informational issues surface as
  `warning`/`info` and never affect the exit code, though they are shown).
- The allowlist file holds `host testname` pairs, whitespace-separated, `#`
  comments allowed. An allowlisted failure is reported as accepted and does
  not gate.
- The union of test names across all hosts in the response is the reference
  set: a test missing for a host renders as `unknown` — shown, never gating,
  never passing.

## Output

Every rendering (table and JSON) carries: endpoint **host** (not the full URL,
never credentials), run timestamp (`finished_date` from the API, else
retrieval time, UTC ISO 8601), API version as reported by the server (else
`unknown`), and the request id. The JSON document is
`{"endpoint", "timestamp", "api_version", "request_id", "request", "domains",
"checks": {"failed": [], "accepted": [], "unknown": []}}` where `domains` is
the API response passed through unmodified.

## Testing constraints

- No test performs network I/O: the HTTP opener is injectable and tests use a
  fake.
- An autouse fixture points `$HOME` at a scratch directory, clears all
  `INTERNETNL_*` variables, and asserts the scratch HOME is still empty at
  teardown.
- One test captures an error path with a credential set and asserts the secret
  appears nowhere in the output.
