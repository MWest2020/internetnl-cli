# Spec Delta: measurement-api (add-measurement-api)

## ADDED Requirements

### Requirement: Batch API v2 compatible surface

The facade SHALL implement the batch API v2 subset — register
(`POST /requests`), status (`GET /requests/{id}`), results
(`GET /requests/{id}/results`) and `GET /metadata/report` — with reply
shapes unchanged from the upstream OpenAPI, authenticated with HTTP Basic,
and SHALL issue request ids matching `^[a-f0-9]{32}$`.

#### Scenario: The internetnl CLI works unchanged

- WHEN the `internetnl` CLI is pointed at the facade with only
  `INTERNETNL_ENDPOINT`, `INTERNETNL_USERNAME` and `INTERNETNL_PASSWORD`
  changed
- THEN submit, poll and results behave identically to a run against the
  upstream instance, with no CLI code change

#### Scenario: Unimplemented v2 endpoint

- WHEN a client calls a v2 path the facade does not proxy
- THEN it receives a v2-shaped error body with a clear label, not an empty
  or framework-default error page

### Requirement: Tenant isolation

The facade SHALL bind every request to the credential that created it and
SHALL NOT reveal to any other credential that the request exists. Facade ids
SHALL be facade-issued; upstream ids SHALL never reach a client.

#### Scenario: Another tenant's request id

- WHEN credential B retrieves status or results for a request created by
  credential A
- THEN the facade answers 404 — indistinguishable from a nonexistent id

#### Scenario: Upstream id stays internal

- WHEN any facade reply or error is rendered
- THEN it contains the facade-issued id only, never the upstream instance's
  request id

### Requirement: Upstream credential never leaves the server

The facade SHALL keep its upstream batch credential exclusively in
server-side configuration and SHALL NOT include it in any reply, error,
log line or client-visible header.

#### Scenario: Error path with upstream authentication failure

- WHEN the upstream instance rejects the facade's credential
- THEN the client sees a v2-shaped upstream-error reply naming neither the
  credential nor its base64 form, and the facade log carries status and
  host only

### Requirement: Limits protect the instance

The facade SHALL enforce a per-credential rate limit, a maximum number of
domains per request and a maximum number of concurrent runs, each tunable
via the environment, answering violations with v2-shaped error bodies and
appropriate status codes (429 for rate, 400 for size).

#### Scenario: Rate limit exceeded

- WHEN a credential exceeds its request rate
- THEN the facade answers 429 with a v2-shaped error body and does not
  contact the upstream instance

#### Scenario: Oversized domain list

- WHEN a submission exceeds the configured maximum domains per request
- THEN the facade answers 400 naming the limit, and nothing is submitted
  upstream

### Requirement: Append-only audit trail

The facade SHALL record every submission and credential-lifecycle event in
an append-only audit store (credential, timestamp, domain count, facade and
upstream ids) with no update or delete path, and the documentation SHALL
state the retention period for audit records and result bodies.

#### Scenario: Submission is audited

- WHEN a submission is accepted
- THEN an audit record exists before the reply is sent, and no code path
  can modify or remove it

#### Scenario: Reader checks retention

- WHEN someone reads the service documentation before submitting
- THEN they find how long domain lists, results and audit records are kept

### Requirement: Honest provenance

The facade SHALL pass result bodies through unmodified and SHALL identify
itself as the measuring party in a response header and in its documentation,
which SHALL restate the documented differences between batch results and the
internet.nl website and SHALL state that the service is an independent
instance, affiliated with neither internet.nl nor Platform
Internetstandaarden.

#### Scenario: Results are passthrough

- WHEN results are retrieved
- THEN the domains object is structurally identical to the upstream reply's
  (equal under canonical JSON serialisation — no key added, removed,
  reordered or rewritten), and the response carries a header naming the
  facade instance

### Requirement: Credential lifecycle

The facade SHALL support operator-issued credentials and immediate
revocation; acceptance of the terms of use (only measure hosts you operate
or have permission to test) SHALL be a documented precondition of issuance.

#### Scenario: Revoked credential

- WHEN a revoked credential makes any call
- THEN the facade answers 401 immediately, with no grace period

### Requirement: Deployment keeps the instance private

The deployment recipe SHALL expose only the facade publicly; the batch
instance SHALL be reachable solely from the internal network the facade
shares with it.

#### Scenario: Direct approach of the instance

- WHEN the batch instance's API is addressed from outside the deployment
- THEN the connection is refused or unroutable; only the facade answers
  publicly
