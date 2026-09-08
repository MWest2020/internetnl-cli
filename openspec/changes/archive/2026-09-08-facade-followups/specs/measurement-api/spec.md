## MODIFIED Requirements

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
