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

| `INTERNETNL_ALLOW_HTTP` | unset | Set to `1` to permit a plaintext `http://` endpoint (lab use only) |

The config file (INI, section `[internetnl]`) may hold `endpoint` only.
Credentials come exclusively from the environment.

Hardening (review round 1): the endpoint scheme must be `https`; `http` is a
`ConfigError` unless `INTERNETNL_ALLOW_HTTP=1`, because the client sends HTTP
Basic credentials on every request. A missing or empty `$HOME` means "no
default config file" — the path never degrades to a CWD-relative lookup.

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
- `results` is one-shot: a finished run renders; an **unfinished** run
  (`registering|running|generating`) reports its status and exits `0` without
  inventing partial verdicts. A run whose status is `error` or `cancelled` is
  not unfinished but failed: hard error, exit `2` — identical to the poll
  route. An unrecognised status is an API error, exit `2`.
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

Transport hardening (review round 1):

- **Redirects are never followed.** The transport refuses every 3xx: it is
  reported like any other non-200 (`ApiError`, exit 2). Following one would
  re-send the `Authorization` header to whatever host the reply names.
- **`request_id` is validated** against the upstream `RequestId` pattern
  `^[a-f0-9]{32}$` before it is placed in a URL path — both an id from argv
  (violation = usage error, exit 2) and one returned by a submit reply
  (violation = `ApiError`, exit 2). Belt and braces: the id is additionally
  `urllib.parse.quote(..., safe="")`-encoded at interpolation time.
- **Malformed 200 replies fail closed**: a reply whose `request` object, or
  whose `request.request_id` / `request.status`, is missing or not of the
  expected type is an `ApiError` (exit 2), never an uncaught traceback.

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
- The reference set — "the subtests the CLI knows about" — is the union of
  (a) the test names the instance itself declares via `GET
  {endpoint}/metadata/report` for the run's request type (items of
  `report.data` with `type: "test"` that appear in the corresponding
  `report.hierarchy.web` / `.mail` tree), fetched once per render, and
  (b) the test names observed across all hosts in the results reply. A test
  in the reference set that is missing for a host renders as `unknown` —
  shown, never gating, never passing. This keeps the knowledge on the
  instance (no local test catalogue, per the honest-results rule) while
  catching a subtest the server omitted for every host. When the metadata
  call fails, the CLI warns on stderr that unknown-detection is limited to
  tests present in the response, and continues with (b) alone — a degraded
  render, never a hard failure.

## Output

Every rendering (table and JSON) carries: endpoint **host** (not the full URL,
never credentials), run timestamp (`finished_date` from the API, else
retrieval time, UTC ISO 8601), API version as reported by the server (else
`unknown`), and the request id. The JSON document is
`{"endpoint", "timestamp", "api_version", "request_id", "request", "domains",
"checks": {"failed": [], "accepted": [], "unknown": []}}` where `domains` is
the API response passed through unmodified.

Rendering hardening (review round 1): every cell the table renderer emits —
host names, test names, statuses, verdict-derived text — passes through a
control-character filter (anything below U+0020, plus U+007F, becomes `?`),
so API- or file-supplied bytes can never smuggle terminal escapes into the
plain-text mode; JSON mode is already safe via `json.dumps`. A domain whose
`scoring` is explicitly `null` renders `-`, the same as an absent score.
Exceeding `INTERNETNL_BATCH_SIZE` on submit is a hard usage error (exit 2,
naming the variable) — the CLI never silently chunks into several requests.

## Testing constraints

- No test performs network I/O: the HTTP opener is injectable and tests use a
  fake.
- An autouse fixture points `$HOME` at a scratch directory, clears all
  `INTERNETNL_*` variables, and asserts the scratch HOME is still empty at
  teardown.
- One test captures an error path with a credential set and asserts the secret
  appears nowhere in the output.
- Review round 1 additions: the environment tunables are each proven
  end-to-end with a non-default value (poll interval, poll max, timeout —
  asserted at the opener seam — and batch size); the no-endpoint path asserts
  the opener was never called; the credential-leak property is also asserted
  through `main()` with `INTERNETNL_PASSWORD` set in the environment; a 3xx
  reply, an invalid `request_id`, a reply without `request`, and a
  `scoring: null` domain each have a test pinning the hardened behaviour.
- CI pins third-party actions to a full commit SHA
  (`astral-sh/setup-uv@20cfd1bf945f4377ade1205e4dbc17946fc9a30d # v10.0.1`).
