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
  upstream check runs

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
  limits use, with no second, independently-maintained counter

#### Scenario: A per-IP bucket bounds repeat submissions from one address

- WHEN a client IP (read from the configured header, generalised to
  `/32` for IPv4 or `/64` for IPv6) has already had
  `NETNL_DEMO_PER_IP_PER_HOUR` accepted submissions in the trailing hour
- THEN a further submission from that same address answers 429, while a
  missing or unparseable client-IP header always falls into one shared
  bucket rather than bypassing this limit or being given its own identity

#### Scenario: A domain cooldown blocks a repeat run without leaking an id

- WHEN a domain was accepted for a demo run less than
  `NETNL_DEMO_DOMAIN_COOLDOWN_SECONDS` ago
- THEN a further submission for that same (normalised) domain answers 429
  and never returns the request id of the run already in progress or
  finished for it

#### Scenario: A missing or revoked demo credential is the kill switch

- WHEN the credential row named by `NETNL_DEMO_TENANT` does not exist, or
  has been revoked
- THEN every demo submission answers 503 `demo-unavailable`, and this is
  the sole mechanism an operator needs to take the demo offline without a
  restart or a configuration change

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
