# Spec Delta: batch-measurement (add-internetnl-cli)

## ADDED Requirements

### Requirement: Configurable endpoint

The CLI SHALL take the batch API base URL from configuration and SHALL NOT
contain a default pointing at any specific instance. Credentials SHALL be read
from the environment and SHALL NOT be accepted as command-line arguments.

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

Timeouts, poll interval, maximum poll duration, batch size, config path and
endpoint SHALL be settable from the environment.

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
