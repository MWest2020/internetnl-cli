# Spec Delta: measurement-api (add-demo-run)

## MODIFIED Requirements

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

## ADDED Requirements

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
