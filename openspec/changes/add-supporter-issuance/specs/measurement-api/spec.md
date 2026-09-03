# Spec Delta: measurement-api (add-supporter-issuance)

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
