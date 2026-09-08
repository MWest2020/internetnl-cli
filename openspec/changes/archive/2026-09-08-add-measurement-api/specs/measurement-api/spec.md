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

#### Scenario: Isolation holds under concurrent requests

- WHEN many requests from different credentials are served concurrently
- THEN no request ever receives a row belonging to another credential, and a
  status or results lookup for a credential's own id returns only that
  credential's data

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

#### Scenario: Concurrent submits cannot exceed the limit

- WHEN a credential fires more simultaneous submissions than its rate or
  concurrency limit allows
- THEN no more than the limit reach the upstream instance, and every accepted
  submission has an audit record written before upstream was contacted

#### Scenario: Internal targets are refused

- WHEN a submission contains an IP-address literal, a single-label name, a
  name under a reserved or internal-use suffix (`.localhost`, `.local`,
  `.internal`, `.corp`, `.home`, `.lan`, …), or a known cloud-metadata
  hostname
- THEN the facade answers 400 and submits nothing upstream, so it cannot be
  used to probe the internal network by literal address or by a
  convention-internal name

#### Scenario: A stranded reservation frees its slot

- WHEN a submission reserves a slot but its upstream call never completes
- THEN after the reserving grace the prune job clears the stale reservation,
  and the credential's concurrency slot is available again

#### Scenario: Persistently failing upstream refresh caps concurrency, not availability

- WHEN every one of a credential's non-terminal runs can never be refreshed
  from upstream (a sustained upstream outage or persistent upstream error)
- THEN each further submission from that credential answers 429 rather than
  crashing, and the credential is blocked for at most as long as those rows
  remain non-terminal — bounded by the result-retention window and the
  deployment's prune schedule, not indefinitely

### Requirement: Authenticated surface

The facade SHALL require valid HTTP Basic credentials on every route in the
v2 measurement subset, `GET /metadata/report` included; no measurement route
SHALL be anonymous. A single operational liveness endpoint (`GET /health`) MAY be
anonymous, but SHALL reveal nothing beyond a static ok status — no API
version, no upstream host, no credential, and it SHALL NOT contact the
upstream instance or read tenant data. An optional, operator-opted-in
`GET /.well-known/security.txt` (RFC 9116) MAY also be anonymous, on the
same terms: it SHALL reveal nothing beyond the operator-configured contact
value, and it SHALL NOT contact the upstream instance or read tenant data.
When the operator has not configured a contact value, this path SHALL NOT
exist as a route at all — it SHALL fall through to the same 501
not-implemented catch-all as any other unrecognised path.

#### Scenario: Anonymous metadata request

- WHEN `GET /metadata/report` is called without valid credentials
- THEN the facade answers 401 and does not contact the upstream instance

#### Scenario: Liveness probe needs no credentials and leaks nothing

- WHEN `GET /health` is called without credentials
- THEN the facade answers 200 with a static status, contacts neither the
  upstream instance nor tenant data, and discloses no API version, upstream
  host or credential

#### Scenario: security.txt served when a contact is configured

- WHEN `GET /.well-known/security.txt` is called without credentials and
  the operator has configured a contact value
- THEN the facade answers 200 with a `text/plain` body containing a
  `Contact:` line carrying exactly that configured value and an `Expires:`
  line, and contacts neither the upstream instance nor tenant data

#### Scenario: security.txt absent when no contact is configured

- WHEN `GET /.well-known/security.txt` is called and the operator has not
  configured a contact value
- THEN the facade answers 501, identically to any other unrecognised path

### Requirement: Authentication cost is bounded

The facade SHALL reject a request whose `Authorization` header is missing or
does not parse as `Basic base64(username:password)` (the `Basic` scheme
token matched case-insensitively, per RFC 7617) without performing a
password-hash computation, since no username is present to protect against
enumeration in that case. The facade SHALL cap the number of password-hash
verifications it performs concurrently to a small limit, and SHALL answer a
request that cannot obtain a verification slot within a short, bounded wait
with a 503 v2-shaped error carrying a `Retry-After` header, rather than
performing the computation regardless of the limit or queueing it
unboundedly.

#### Scenario: Missing or malformed credentials fail fast

- WHEN a request has no `Authorization` header, or one that is not valid
  `Basic base64(username:password)` (wrong scheme, invalid base64, or no
  colon after decoding)
- THEN the facade answers 401 without computing a password hash

#### Scenario: The Basic scheme token is case-insensitive

- WHEN a request's `Authorization` header uses any casing of the `Basic`
  scheme token (e.g. `basic`, `BASIC`, `Basic`) with otherwise valid
  credentials
- THEN the facade authenticates it exactly as it would `Basic`

#### Scenario: Concurrent authentication is bounded

- WHEN more authentication attempts arrive concurrently, and stay
  concurrent for longer than the facade's short bounded wait, than the
  facade's verification-concurrency limit
- THEN attempts that cannot obtain a slot within that wait receive a 503
  v2-shaped error with a `Retry-After` header, and at no point does the
  number of password-hash computations running at once exceed that limit

### Requirement: Append-only audit trail

The facade SHALL record every submission and credential-lifecycle event in
an append-only audit store (credential, timestamp, domain count, facade and
upstream ids) with no update or delete path, and the documentation SHALL
state the retention period for audit records and result bodies. The facade
SHALL also record failed authentication attempts (a sanitised username or
its absence, and the route) in the same append-only store, aggregated over
a bounded time window per distinct username-and-route pair rather than one
record per attempt; the total number of distinct username-and-route pairs
tracked at once SHALL itself be capped, with any pair beyond that cap
collapsed into a per-route overflow record, so that neither the number of
distinct pairs an attacker can generate nor the volume of failed attempts
can grow the audit store without bound; the password SHALL NOT appear in
any audit record under any circumstance. A credential value recorded on a
failed-authentication record is attacker-supplied and SHALL NOT be treated
by an operator as verified tenant attribution. Accepted residual risk
(round-4, N2, Low, see design.md): once the per-window cap on distinct
username-and-route pairs is reached, further distinct usernames — including
one an attacker deliberately aims at a real tenant's username to hide a
targeted brute-force inside the shared overflow record — collapse into that
same overflow record; total failure volume per route per window remains
correct and auditable, but a targeted burst folded into the overflow record
is no longer individually attributable to the username it targeted. This is
documented, not built around, in this change.

#### Scenario: Submission is audited

- WHEN a submission is accepted
- THEN an audit record exists before the reply is sent, and no code path
  can modify or remove it

#### Scenario: Failed authentication is audited without unbounded growth

- WHEN a number of authentication attempts for the same username (or the
  same absence of one) against the same route fail within the same bounded
  time window
- THEN at most one audit record summarising that window's failure count
  exists for that username-and-route pair, not one record per attempt, and
  none of those records contains the attempted password

#### Scenario: The failed-authentication aggregator stays bounded under an unbounded-username attack

- WHEN a large number of distinct, never-repeated usernames fail
  authentication against the same route within the same bounded time window
- THEN the number of failed-authentication audit records produced for that
  window is bounded by the facade's fixed cap plus one overflow record, not
  by how many distinct usernames were attempted

#### Scenario: A failing aggregator write never fails the request that triggered it

- WHEN the facade attempts to persist an aggregated batch of
  failed-authentication records and that write itself fails
- THEN the request that triggered the flush completes according to its own
  outcome (success or its own rejection reason), the failure is logged, and
  the unwritten window's tally is not silently retried as if it had
  succeeded

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

#### Scenario: Security headers are pinned on every reply

- WHEN any request is made to the facade, whether it succeeds or is
  rejected (validation failure, oversized body, unauthorised, unknown
  path, or an unexpected server error)
- THEN the reply carries a fixed `Content-Security-Policy`,
  `X-Content-Type-Options`, `Referrer-Policy` and `X-Frame-Options` header,
  identical in value across success and error replies

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
