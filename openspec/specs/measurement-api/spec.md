# measurement-api Specification

## Purpose
TBD - created by archiving change add-measurement-api. Update Purpose after archive.
## Requirements
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

A separate, opt-in `/demo/*` route family (see "Anonymous demo runs are
strictly bounded" below) is anonymous by design and is not a violation of
"no measurement route SHALL be anonymous" above: it is not part of the v2
measurement subset, it is disabled by default, it never authenticates as
any tenant, and every one of its own bounds (rate, concurrency, per-IP,
per-domain, origin) is a hard requirement of that separate requirement, not
an exception carved out of this one. A `/demo/*` route SHALL NOT exist at
all unless the operator has explicitly enabled it, on the same
"acts like it does not exist" terms as `/health` and `security.txt` above
when their own preconditions are unmet.

A separate, opt-in `POST /webhooks/bmc` route (see the `supporter-issuance`
capability's own requirements) is likewise anonymous by design and not a
violation of "no measurement route SHALL be anonymous" above: it is not
part of the v2 measurement subset, it is disabled unless the operator has
set `NETNL_BMC_WEBHOOK_SECRET`, it never authenticates as any tenant, it
never reaches the upstream instance, and every request to it is verified
against that secret before anything else happens. A request to this path
SHALL NOT be able to influence tenant data beyond, on success, the creation
of exactly one new tenant credential row through the same primitives
`netnl-admin user add` already uses. When the operator has not set
`NETNL_BMC_WEBHOOK_SECRET`, this path SHALL NOT exist as a route at all, on
the same "acts like it does not exist" terms as `/health`, `security.txt`
and `/demo/*` above.

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

#### Scenario: The demo family does not count as an anonymous measurement route

- WHEN the operator has not set `NETNL_DEMO_ENABLED=1`
- THEN every `/demo/*` path answers the ordinary 501 not-implemented
  catch-all, identically to any other unrecognised path, and this is not a
  violation of "no measurement route SHALL be anonymous" — the demo family
  is a separate surface, governed entirely by "Anonymous demo runs are
  strictly bounded" below

#### Scenario: The webhook bridge does not count as an anonymous measurement route

- WHEN the operator has not set `NETNL_BMC_WEBHOOK_SECRET`
- THEN `POST /webhooks/bmc` answers the ordinary 501 not-implemented
  catch-all, identically to any other unrecognised path, and this is not a
  violation of "no measurement route SHALL be anonymous" — the webhook
  bridge is a separate surface, governed entirely by the
  `supporter-issuance` capability's own requirements, and it never contacts
  the upstream instance or reaches the v2 measurement subset itself

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
by an operator as verified tenant attribution. The cap SHALL reserve
additional, separately bounded room for usernames that belong to an
existing credential (revoked or not): once the general cap is reached, a
new pair for an unknown username SHALL collapse into the overflow record,
while a new pair for a known tenant's username SHALL still be tracked and
recorded under that username until the reserved room is itself exhausted —
so that a burst of failures aimed at a real tenant cannot be hidden inside
the overflow record by first exhausting the general cap with throwaway
usernames.

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

#### Scenario: A known tenant's failures stay attributed once the general cap is reached

- WHEN the general cap on distinct username-and-route pairs has been
  reached within a window by unknown usernames, and then authentication
  fails for a username that belongs to an existing credential (wrong
  password, or a revoked credential) on the same route
- THEN that window's audit contains a failed-authentication record under
  that tenant's sanitised username, separate from the overflow record, and
  a further failure for yet another unknown username in the same window
  still collapses into the overflow record

#### Scenario: The reserved room for known tenants is itself bounded

- WHEN failures for more distinct known-tenant usernames than the reserved
  room allows occur on one route within one window after the general cap
  is reached
- THEN the additional known-tenant pairs collapse into the overflow record
  and the total number of tracked pairs never exceeds the general cap plus
  the reserved room plus one overflow record per route

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
Internetstandaarden. Toward the upstream instance, the facade SHALL identify
itself on every request with a `User-Agent` whose first product token names
the facade and its version (`netnl/<version>`), followed by the client
library's own token, so that facade traffic is distinguishable from a
directly-run CLI in the instance's logs; the request is otherwise identical
to the one the unmodified client would send.

#### Scenario: Results are passthrough

- WHEN results are retrieved
- THEN the domains object is structurally identical to the upstream reply's
  (equal under canonical JSON serialisation — no key added, removed,
  reordered or rewritten), and the response carries a header naming the
  facade instance

#### Scenario: Upstream sees the facade, not a bare CLI

- WHEN the facade makes any call to the upstream instance
- THEN the request's `User-Agent` starts with `netnl/` and also contains
  `internetnl-cli/`, and its `Authorization`, `Content-Type` and `Accept`
  headers are exactly those the unmodified client would have sent

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
or have permission to test) SHALL be a documented precondition of
issuance. The facade SHALL also support re-keying an existing credential
row in place — a fresh password and immediate un-revocation — without
requiring that row's username to be free of any prior row, so that an
operator can turn a previously revoked credential back on without a
restart or a configuration change.

#### Scenario: Revoked credential

- WHEN a revoked credential makes any call
- THEN the facade answers 401 immediately, with no grace period

#### Scenario: Reissuing a credential turns it back on

- WHEN an operator reissues an existing credential row, whether it is
  currently revoked or not
- THEN the facade generates a fresh password for that row, clears any
  revocation, and prints the new password exactly once; a subsequent call
  authenticating with the new password succeeds, and issuing a reissue for
  a username with no existing row at all is refused rather than silently
  creating one

### Requirement: Deployment keeps the instance private

The deployment recipe SHALL expose only the facade publicly; the batch
instance SHALL be reachable solely from the internal network the facade
shares with it.

#### Scenario: Direct approach of the instance

- WHEN the batch instance's API is addressed from outside the deployment
- THEN the connection is refused or unroutable; only the facade answers
  publicly

### Requirement: Anonymous demo runs are strictly bounded

The facade SHALL, only when the operator sets `NETNL_DEMO_ENABLED=1` (off
by default), expose an anonymous, single-domain demo route family
(`POST /demo/requests`, `GET /demo/requests/{id}`, `GET
/demo/requests/{id}/results`, plus `OPTIONS` on each). When enabled, the
facade SHALL run every demo submission under one operator-issued credential row
named by `NETNL_DEMO_TENANT` rather than any real tenant's identity, SHALL
accept a request body shaped as exactly one field (`{"domain": string}`,
rejecting any additional field or a differently-typed `domain`), SHALL
reuse the measurement subset's own domain shape and anti-SSRF checks rather
than reimplementing them, SHALL enforce the demo tenant's own rate and
concurrency limits through the same atomic reservation mechanism the
measurement subset uses, SHALL additionally bound submissions by client IP
and by a per-domain cooldown, SHALL restrict cross-origin access to exactly
one configured origin, SHALL NOT read or validate an `Authorization` header
or invoke any password-hashing computation on this path, and SHALL record
in its audit trail nothing more than an ordinary tenant-shaped submission
record — no visitor IP, `Origin`, or submitted domain, on any path,
accepted or rejected.

#### Scenario: Demo disabled by default

- WHEN `NETNL_DEMO_ENABLED` is unset (the default)
- THEN every `/demo/*` path answers 501 not-implemented, indistinguishable
  from any other unmapped path, and no demo-specific configuration variable
  is required to start the facade

#### Scenario: The request body accepts exactly one field

- WHEN `POST /demo/requests` is called with a body containing an
  additional field, a `type` field, or a `domain` that is a list rather
  than a string
- THEN the facade rejects the request before any limit, credential or
  upstream check runs, with the same single literal message a rejected
  domain's own shape/anti-SSRF failure uses below — never a field name or
  a JSON-path reflected back from the request body

#### Scenario: A rejected domain shows one plain, showable message

- WHEN a submitted domain fails the shape check or the anti-SSRF check
  (an IP-address literal, a single-label name, a reserved/internal-use
  suffix, or a malformed token such as a URL)
- THEN the facade answers 400 with the single literal message
  `"enter a bare domain like example.nl, not a URL"`, worded for a person
  reading a form rather than an API consumer, and nothing is submitted
  upstream

#### Scenario: The demo tenant's own limit is the single aggregate cap

- WHEN accepted demo submissions in the trailing hour reach
  `NETNL_DEMO_MAX_PER_HOUR`, or non-terminal demo runs reach
  `NETNL_DEMO_MAX_CONCURRENT`
- THEN the next demo submission answers 429, enforced by the same atomic
  reservation transaction the measurement subset's own rate/concurrency
  limits use, with no second, independently-maintained counter, and with a
  visitor-facing literal that never names the configured numbers (unlike
  the equivalent tenant-facing rejection on the authenticated surface); a
  non-terminal demo run whose upstream status has since become terminal
  without ever being polled is refreshed before this check runs, so it no
  longer counts toward `NETNL_DEMO_MAX_CONCURRENT`

#### Scenario: A per-IP bucket bounds repeat submissions from one address

- WHEN a client IP (read from the configured header, generalised to
  `/32` for IPv4 or `/64` for IPv6) has already had
  `NETNL_DEMO_PER_IP_PER_HOUR` accepted submissions in the trailing hour,
  checked and recorded atomically so that concurrent submissions from the
  same address can never all observe "not yet at the limit"
- THEN a further submission from that same address answers 429, while a
  missing or unparseable client-IP header always falls into one shared
  bucket rather than bypassing this limit or being given its own identity;
  a submission whose per-IP claim succeeded but whose reservation or
  upstream call then failed does not count against this bucket

#### Scenario: A domain cooldown blocks a repeat run without leaking an id

- WHEN a domain was accepted for a demo run less than
  `NETNL_DEMO_DOMAIN_COOLDOWN_SECONDS` ago, checked and recorded atomically
  so that concurrent submissions for the same domain can never all observe
  "not on cooldown"
- THEN a further submission for that same (normalised) domain answers 429
  and never returns the request id of the run already in progress or
  finished for it; a submission whose cooldown claim succeeded but whose
  reservation or upstream call then failed does not leave the domain on
  cooldown

#### Scenario: The per-IP cap and the domain cooldown are indistinguishable to a prober

- WHEN a demo submission is rejected either because its client IP is at
  its per-IP cap or because its domain is on cooldown
- THEN both outcomes answer 429 with the identical literal message, and
  the per-IP check is evaluated strictly before the domain-cooldown check,
  so that a submission already rejected for being over its per-IP cap
  never touches (and so never affects, or reveals anything about) any
  domain's cooldown state

#### Scenario: A per-IP poll budget bounds status and results requests

- WHEN a client IP (keyed the same way as the per-IP submission bucket)
  has already made `NETNL_DEMO_POLLS_PER_IP_PER_HOUR` status or results
  requests in the trailing hour
- THEN a further status or results request from that address answers 429
  with its own literal message, independently of the per-submission bucket

#### Scenario: A terminal status poll never re-contacts the upstream instance

- WHEN `GET /demo/requests/{id}` is called for an id whose stored status is
  already terminal (`done`, `error` or `cancelled`)
- THEN the facade answers from its own store, without making any call to
  the upstream instance; `GET /demo/requests/{id}/results` is unaffected by
  this and always fetches from the upstream instance, since the facade
  does not retain a copy of the results payload

#### Scenario: An upstream failure on a demo route never reveals the upstream hostname

- WHEN a call to the upstream instance made on behalf of a demo request
  fails, for any reason (unreachable, an error status, or a malformed
  reply)
- THEN the facade answers with one of two fixed, host-free outcomes — a
  503 reusing the same `demo-unavailable` message the kill switch already
  uses when the upstream instance cannot be reached at all, or a 502 with a
  single fixed literal for every other upstream failure — and the
  upstream instance's own hostname never appears in the reply

#### Scenario: A missing or revoked demo credential is the kill switch

- WHEN the credential row named by `NETNL_DEMO_TENANT` does not exist, or
  has been revoked
- THEN every demo submission answers 503 `demo-unavailable`, and this is
  the sole mechanism an operator needs to take the demo offline without a
  restart or a configuration change; the operator SHALL be able to take
  the demo back online again, equally without a restart or a configuration
  change, by re-keying the same credential row in place (see "Credential
  lifecycle" below) rather than only being able to issue a brand new one

#### Scenario: CORS is scoped to exactly one origin

- WHEN any `/demo/*` request carries an `Origin` header
- THEN the facade answers with `Access-Control-Allow-Origin` set to the
  literal configured `NETNL_DEMO_ALLOWED_ORIGIN` value only when that
  header is absent or matches it exactly (never echoing a different
  value), never paired with `Access-Control-Allow-Credentials`; an actual
  (non-`OPTIONS`) demo request whose `Origin` is present and does not match
  answers 403 `forbidden-origin`, while the equivalent `OPTIONS` preflight
  still answers 204, simply without the CORS headers that would let a
  browser proceed

#### Scenario: The preflight actually grants the browser permission to proceed

- WHEN an `OPTIONS` preflight request to any `/demo/*` path carries an
  `Origin` header that is absent or matches the configured one
- THEN the 204 reply additionally carries `Access-Control-Allow-Methods`
  covering `POST`, `GET` and `OPTIONS`, `Access-Control-Allow-Headers`
  covering `content-type`, and an `Access-Control-Max-Age`, so a browser's
  own preflight enforcement actually permits the cross-origin `POST` (with
  a JSON `Content-Type`) the demo page needs to make; a mismatched `Origin`
  gets none of these three, on top of already lacking the CORS headers
  above

#### Scenario: The demo path never touches authentication

- WHEN a demo request carries any `Authorization` header, a malformed one,
  or none at all, and separately, when the facade's own password-hashing
  function is made to raise on any call
- THEN the demo request's outcome is unaffected by either — no
  `Authorization` header is read, and no password-hashing computation is
  ever invoked on this path

#### Scenario: Demo submissions are audited like a tenant submission, and reveal nothing else

- WHEN a demo submission is accepted
- THEN exactly one audit record is written, identical in shape to an
  ordinary tenant submission (`event = submit`, `credential =
  NETNL_DEMO_TENANT`, `domain_count = 1`), and no visitor IP, `Origin`, or
  the submitted domain itself appears in that record, anywhere else in the
  database, or in any log line, on this or any rejected path; a rejected
  demo submission writes no audit record at all, and a demo-owned request
  row past `NETNL_DEMO_RETENTION_HOURS` is pruned on the facade's existing
  retention pass, counted separately from the tenant retention counters

