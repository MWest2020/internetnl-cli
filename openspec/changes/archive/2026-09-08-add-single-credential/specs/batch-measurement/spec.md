# Spec Delta: batch-measurement (add-single-credential)

## MODIFIED Requirements

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
