# batch-measurement Specification

## Purpose

What the CLI is for: submitting a whole fleet of domains to an Internet.nl batch
endpoint and getting an answer you can act on.

The public site tests one domain in a browser. This capability is the other
shape of the same question — many domains, unattended, in a pipeline. Everything
here follows from that: the endpoint is configurable because not everyone
measures against the same instance; a run can be resumed because a batch outlives
a terminal; the output is machine-readable because a human is not the only
reader; and a result can fail a build, because a measurement nobody acts on is a
report.

"Honest results" is the load-bearing requirement. A gate that quietly passes when
the measurement did not actually happen is worse than no gate, so the difference
between "everything is fine" and "we could not tell" stays visible all the way to
the exit code.

## Requirements

### Requirement: Configurable endpoint

The CLI SHALL take the batch API base URL from configuration and SHALL NOT
contain a default pointing at any specific instance. Credentials SHALL be read
from the environment and SHALL NOT be accepted as command-line arguments.

Credentials SHALL be configurable either as the pair `INTERNETNL_USERNAME` /
`INTERNETNL_PASSWORD`, or as a single `INTERNETNL_CREDENTIAL` environment
variable in `username:password` form, split on the first `:` (a password MAY
contain a `:`; a username used over HTTP Basic never can, per RFC 7617). The
CLI SHALL NOT accept both forms at once, and SHALL NOT accept a
`INTERNETNL_CREDENTIAL` value that lacks a `:`, or whose split yields an empty
username or an empty password — in every one of those cases it SHALL exit
non-zero with a `ConfigError` that names the problem without echoing the
offending value.

#### Scenario: No endpoint configured

- WHEN the CLI runs without an endpoint in the environment or config file
- THEN it exits non-zero with a message naming the variable to set, and makes
  no network call

#### Scenario: Credentials never surface

- WHEN a request fails and the CLI reports the error
- THEN the message contains the HTTP status and the endpoint host, and never
  the credential — including in `--debug` output

#### Scenario: Switching instances changes nothing but configuration

- WHEN the endpoint is changed from a hosted instance to a self-hosted one
- THEN the same command produces the same shape of result, with no code change

#### Scenario: Single-credential form is split on the first colon

- WHEN `INTERNETNL_CREDENTIAL` is set to `alice:se:cret`
- THEN the CLI authenticates as username `alice` with password `se:cret`

#### Scenario: Both forms set at once is a config error

- WHEN `INTERNETNL_CREDENTIAL` is set together with `INTERNETNL_USERNAME` or
  `INTERNETNL_PASSWORD`
- THEN the CLI exits non-zero with a message naming both variables, and makes
  no network call

#### Scenario: A colon-less credential is a config error

- WHEN `INTERNETNL_CREDENTIAL` is set to a value containing no `:`
- THEN the CLI exits non-zero with a message stating the required
  `username:password` format, and the value itself never appears in that
  message

#### Scenario: A degenerate split is a config error, not a silent anonymous request

- WHEN `INTERNETNL_CREDENTIAL` is set to `:secret`, `alice:`, or `:`
- THEN the CLI exits non-zero with a message stating that both the username
  and the password must be non-empty, and makes no network call — an empty
  username SHALL NOT be allowed to reach the request layer, where it would
  cause the `Authorization` header to be omitted entirely

### Requirement: Submit, poll, resume

The CLI SHALL submit a set of hostnames as one batch request, SHALL print the
request identifier before polling, and SHALL accept that identifier later to
resume polling a run it did not start.

#### Scenario: Long run survives an interrupted client

- WHEN a submit is interrupted after the request id is printed
- THEN `internetnl poll <id>` retrieves the same run's results

#### Scenario: Run still in progress

- WHEN results are requested for a run that has not finished
- THEN the CLI reports the run's status and exits zero without inventing
  partial verdicts

### Requirement: Honest results

Every rendered result SHALL record the endpoint it came from, the time of the
run, and the API version. The CLI SHALL NOT compute, approximate, or fill in a
verdict locally.

#### Scenario: Endpoint appears in output

- WHEN results are rendered as a table or as JSON
- THEN each result carries the endpoint host, the run timestamp and the API
  version

#### Scenario: API unreachable

- WHEN the endpoint cannot be reached
- THEN the CLI exits non-zero with the transport error, and emits no rows

#### Scenario: Subtest missing from the response

- WHEN the API omits a subtest the CLI knows about
- THEN that subtest renders as unknown, never as passing

### Requirement: Usable as a gate

The CLI SHALL support failing on scored subtests, with an allowlist of accepted
exceptions, so it can run unattended in a pipeline.

#### Scenario: Regression fails the pipeline

- WHEN `--fail-on-scored` is set and a scored subtest fails for any host
- THEN the CLI exits non-zero and lists host and subtest, one per line

#### Scenario: Accepted exception does not fail

- WHEN that same host and subtest appear in the allowlist file
- THEN the run exits zero and the exception is listed as accepted in the output

#### Scenario: Informational findings never gate

- WHEN a subtest that does not count toward the score fails
- THEN the exit code is unaffected, and the finding is still shown

### Requirement: Machine-readable output

The CLI SHALL emit plain text by default and structured JSON with `--json`,
without colours or terminal escapes in either mode.

#### Scenario: Piped into another tool

- WHEN output is piped and `--json` is set
- THEN stdout is a single valid JSON document and all progress output goes to
  stderr

### Requirement: Everything tunable

The CLI SHALL take timeouts, poll interval, maximum poll duration, batch
size, config path and endpoint from the environment, tunable without a code
change.

#### Scenario: Slow instance

- WHEN a self-hosted instance takes longer than the default maximum
- THEN raising the environment variable is sufficient, with no code change

### Requirement: Tests write nowhere but their own temp directory

The suite SHALL run with `$HOME` pointed at a throwaway directory and SHALL
leave it empty, enforced by an autouse fixture rather than per-test discipline.

#### Scenario: Suite run against a scratch HOME

- WHEN the suite runs with `$HOME` set to an empty temporary directory
- THEN nothing is created there, and the suite passes

### Requirement: Self-hosted deployment recipe

The repository SHALL document running an own batch instance, including the
upstream hardware requirements, the fixed public IPv4 and IPv6 addressing
requirement, and the documented differences between batch results and the
website's results.

#### Scenario: Reader decides whether to self-host

- WHEN someone reads the deployment page before committing to a server
- THEN they find the minimum and recommended sizing, the addressing
  requirement, and the maintenance implication stated plainly

#### Scenario: Reader is warned about result differences

- WHEN someone plans to quote a batch verdict as an Internet.nl score
- THEN the documentation names the differences — no connection test, DNSSEC
  without registrar lookup, no A/AAAA prechecks

### Requirement: A findings export another tool can read

The CLI SHALL offer an additional output format, `--format findings`,
that writes a versioned document (`"schema": "netnl-findings/v1"`)
carrying every subtest of a completed batch verbatim: the API's
`status` (`passed` / `failed` / `warning` / `info` / `not_tested` /
`error`) and its `verdict` word, per domain, with the domain's
`report_url` and `score_percent`.

The export SHALL NOT aggregate, re-weight, or omit subtests. It SHALL
NOT compute a score of its own — the batch API already publishes one,
and a second number that disagrees with the public report destroys
trust in both.

Category names SHALL be carried verbatim from the API
(`web_dnssec`, `mail_starttls`, …), derived for each test by the
longest category key that prefixes the test name. A test matching no
category SHALL be exported with a null category rather than dropped.

The `detail` field SHALL be null in v1. Per-variant results are not
published by the batch API; the export SHALL NOT obtain them by
scraping the HTML report.

#### Scenario: Every subtest survives the export

- **GIVEN** a completed web batch whose results contain 38 subtests
  across five categories
- **WHEN** the run is exported with `--format findings`
- **THEN** the document contains 38 result entries, each with the
  API's own status and verdict, and each category name carries its
  `web_` prefix

#### Scenario: A broken subtest is not a bad score

- **GIVEN** a completed mail batch in which
  `mail_starttls_tls_available` came back with status `error`
- **WHEN** the run is exported
- **THEN** that entry appears with status `error`, distinguishable
  from `failed`

---

### Requirement: An unfinished batch exports nothing

Exporting a batch that is not `done` SHALL exit non-zero and SHALL
NOT write an output file. Where `--findings-out` names an existing
file, that file SHALL be left untouched.

Partial *content* is fine — `not_tested` subtests and domains whose
own status is not `ok` belong in the document. A partially *written*
or empty document does not: a consumer cannot tell it apart from a
measurement that found nothing, and a CI step that trusts the exit
code would archive it as a success.

#### Scenario: Running batch

- **GIVEN** a batch whose request status is `running`
- **WHEN** `--format findings` is requested
- **THEN** the command exits non-zero and no output file exists

#### Scenario: A domain that failed to measure still appears

- **GIVEN** a completed batch in which one domain has status `error`
- **WHEN** the run is exported
- **THEN** that domain appears with its status and an empty results
  list, so the consumer can tell "measured badly" from "not submitted"

---

### Requirement: The same batch exports the same bytes

Two exports of one completed batch SHALL produce byte-identical
files: domains and tests in a stable order, stable JSON formatting.
A consumer re-importing the same file SHALL be able to recognise it
as already seen, and the file SHALL diff cleanly when archived in
git.

#### Scenario: Re-export is byte-identical

- **GIVEN** a completed batch exported twice
- **WHEN** the two files are compared
- **THEN** they are identical byte for byte
