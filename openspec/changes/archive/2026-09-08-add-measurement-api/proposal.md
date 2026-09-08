# Change: add-measurement-api

## Why

The hosted batch API requires an account per consumer, and requesting one is
a bottleneck — the decision of 2026-08-22 is to route around it: run our own
batch instance and open an API on it for others, wordsworth-style. If that
works, hand it to the community.

Opening the instance itself is not an option. The upstream batch API has no
tenancy: one configured user sees every request made under that credential,
and creating upstream users means shell access (`user_manage.sh`) per signup.
Handing out our single credential would let every consumer read every other
consumer's measurements, and none of the capacity, abuse or audit questions
would have an owner.

So the thing to build is a thin **facade**: a public, multi-tenant API in
front of the private instance. The instance measures; the facade does
identity, isolation, limits and audit — exactly the split wordsworth makes
between its engine and its API surface.

## What Changes

**A batch-API-v2-compatible measurement API** (working name: `netnl`,
owner's pick 2026-08-22 — the name keeps pointing at what this is: an
opinion on how the internet should work, according to people in the
Netherlands) in front of the self-hosted batch instance. Because the name
nods at internet.nl, the service documentation and provenance header must
state plainly that this is an independent instance, not internet.nl and not
Platform Internetstandaarden — the tests are theirs, the operation is ours.

1. **The facade speaks batch API v2.** Same paths, same reply shapes, HTTP
   Basic auth — the subset the ecosystem needs: register, status, results,
   `/metadata/report`. Consequence: the `internetnl` CLI works against it
   **unchanged**, with only `INTERNETNL_ENDPOINT` and credentials different.
   That is the acceptance test, the same rule the CLI itself was built under.
   Facade request ids are facade-issued and match the upstream id shape
   (`^[a-f0-9]{32}$`), so v2 clients that validate ids keep working.
2. **Tenancy the upstream lacks.** Per-consumer credentials, issued by the
   operator (self-service signup is explicitly later). A request belongs to
   the credential that created it; anyone else gets 404, not 403 — no
   existence oracle. The facade maps its public ids to upstream ids
   internally; the single upstream credential never leaves the server.
3. **Limits, because the instance is finite.** Per-credential rate limit,
   max domains per request, max concurrent runs — all environment-tunable,
   all answered with v2-shaped error bodies so clients degrade cleanly.
4. **Wordsworth-style audit.** Append-only trail of who measured what and
   when (credential, domain count, timestamps, upstream id). No UPDATE, no
   DELETE. Retention documented, because submitted domain lists reveal
   infrastructure.
5. **Honest results, inherited.** Result bodies pass through unmodified; the
   facade identifies itself in a response header and in its docs, and the
   docs repeat the batch-vs-website differences. A verdict from this service
   is never "the internet.nl score".
6. **Deploy recipe.** A compose unit next to the batch instance: only the
   facade is publicly reachable; the instance binds to the internal network.
   Terms of use at the door: only measure hosts you operate or have
   permission to test; a revoked credential stops working immediately.

### Tech, deliberately different from the CLI

The CLI is stdlib-only by design. The facade is a service and follows the
wordsworth conventions instead: Python 3.12+ via `uv`, FastAPI + pydantic v2,
SQLite for the tenancy/id-map/audit store (single instance, single writer —
boring wins; PostgreSQL only if reality demands it). It lives in this repo as
a separate package with its own dependency group, so client and facade stay
one story until a community handover argues for a split.

## Non-goals

- **No dashboard, no result history service.** Results are fetched and gone
  after the documented retention; upstream has `Internet.nl-dashboard`.
- **No self-service signup in v1.** Credential issuance is an operator
  runbook, not a feature.
- **No proxying of upstream endpoints the ecosystem does not need** (
  `results_technical`, cancel and list may follow later; absence answers
  with a v2-shaped error, not a surprise).
- **No re-scoring, no verdict edits.** Passthrough or error, never opinion.
- **Not an SLA.** A homelab-grade service, stated plainly in the docs.

## Rollout

1. **Prerequisite:** the self-hosted batch instance runs and the CLI passes
   against it unchanged (tasks 4.2/4.4 of `add-internetnl-cli`). The facade
   has nothing to front until then.
2. **Facade v1** behind issued credentials, private beta with a handful of
   known users; the CLI-unchanged acceptance test gates the beta.
3. **Community opening**: publish the endpoint, the terms, the limits and
   the credential-request runbook.
4. **Handover** when it holds: documentation, deploy recipe and issuance
   runbook complete enough that the community can run it without us.

## Impact

- **Capacity is one instance.** Limits are the product, not an afterthought;
  the docs say what the ceiling is and what happens above it (429, not
  queue-forever).
- **We become an operator.** Abuse handling (a consumer measuring hosts they
  do not own) lands on us: terms at the door, audit trail for the answer,
  revocation as the remedy. The facade never becomes an open relay: no
  anonymous submissions.
- **Privacy of domain lists.** What consumers submit is confidential
  operational data; the audit stores what accountability needs, the docs
  state retention, and result bodies are not kept after retention.
- **The upstream credential is a crown jewel.** It exists only in the
  facade's server-side environment; the CLI's no-leak discipline (never in
  replies, errors or logs) applies to the facade verbatim.
- Risk: moderate — a public endpoint in the homelab. Mitigated by the
  compose split (instance internal-only), issued credentials, limits, and
  the existing habitat review chain building it.
