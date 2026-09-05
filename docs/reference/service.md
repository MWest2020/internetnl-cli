---
status: current
last_reviewed: 2026-09-05
---

# The netnl service: endpoint, terms, limits, retention

What a tenant of the hosted `netnl` facade can count on. These are the
published values behind the deployment Mark runs; anyone self-hosting the
facade sets their own (every value below is an environment variable — see
[deploy-facade.md](../how-to/deploy-facade.md)).

Ratified 2026-09-05: the published policy is exactly what the code defaults
to, so documentation and behaviour cannot drift. The private beta exists to
test whether these numbers are right, and any of them can be changed with an
environment variable and a pod restart — no release needed.

## Endpoint

`https://api.westerweel.work` — the primary public hostname, fronted by a
Cloudflare Tunnel. A Tailscale Funnel `*.ts.net` hostname is kept up in
parallel as a fallback; the beta runbook hands out the primary.

The facade serves the Batch API v2 routes at the root, so this bare base URL
is what goes in `INTERNETNL_ENDPOINT` — the CLI appends `/requests`,
`/requests/{id}`, `/requests/{id}/results` and `/metadata/report` itself. Do
not append `/api/batch/v2`: that prefix belongs to a bare upstream instance,
not to this facade.

## Terms

**Only measure hosts you operate, or have explicit permission to test.** A
measurement makes the upstream instance connect to the target from the
outside; running it against someone else's domain is scanning a system you
do not own. This is the single condition every credential is issued under,
and the ground for revoking one.

Credentials are per tenant, issued by hand (`netnl-admin user add`) or
automatically on a qualifying donation (see
[supporter-key.md](../how-to/supporter-key.md)). They are not transferable.

## Getting a credential

Two routes, both ending in the same kind of credential:

1. **Ask.** Mail the operator with the domains you intend to measure and
   who you are. Beta credentials are issued by hand, to people whose
   ownership of those domains is plausible — the terms above are the whole
   admission test. The operator's side of this is
   [beta.md](../how-to/beta.md#issuing-revoking-and-listing-credentials).
2. **Donate.** A qualifying donation mints a lifetime credential
   automatically and mails it to the donor; see
   [supporter-key.md](../how-to/supporter-key.md). This is self-service and
   needs no correspondence.

Either way you receive one `INTERNETNL_CREDENTIAL` string
(`username:password`) plus this endpoint, and the credential mail contains
nothing else. A lost credential is reissued, not recovered: the operator
runs `netnl-admin user reissue <name>` and the old password stops working.

Credentials are revoked when the terms are broken, on request, or when a
tenant's use is clearly outside what one homelab instance can carry.

## Limits

All tenant limits below are **per credential**, not shared across the
facade: your neighbour's busy hour does not spend your budget.

| What | Value | Variable |
|---|---|---|
| Submissions per hour, per tenant | 10 | `NETNL_RATE_LIMIT` |
| Domains per request | 500 | `NETNL_MAX_DOMAINS` |
| Concurrent runs, per tenant | 2 | `NETNL_MAX_CONCURRENT` |
| Demo: requests per hour | 6 | `NETNL_DEMO_MAX_PER_HOUR` |
| Demo: concurrent runs | 2 | `NETNL_DEMO_MAX_CONCURRENT` |
| Demo: requests per IP per hour | 2 | `NETNL_DEMO_PER_IP_PER_HOUR` |
| Demo: polls per IP per hour | 120 | `NETNL_DEMO_POLLS_PER_IP_PER_HOUR` |
| Supporter keys minted per hour | 20 | `NETNL_SUPPORTER_MAX_PER_HOUR` |
| Delivery attempts per supporter key | 3 | `NETNL_SUPPORTER_MAX_ATTEMPTS` |

### How much runs at once, in total

There is **no facade-wide ceiling**: the limits above are per credential, so
the total work in flight is `2 × active tenants`, plus at most 2 for the
demo. One facade process (a single replica over one SQLite file) serialises
the bookkeeping, but not the measurements themselves.

Behind it all sits **one upstream batch instance**, and its own capacity has
never been measured under load — that measurement is the point of the
private beta (`add-measurement-api`, task 4.3). Until it exists, the honest
statement is: a handful of tenants is fine, and nobody knows where the knee
is. If the beta shows the instance saturating before the per-tenant caps
bite, the fix is a facade-wide ceiling on top of the per-tenant ones, not
lower per-tenant numbers.

The concurrency ceiling is the one that matters: there is a single upstream
batch instance behind this facade, and it is the scarce resource. Everything
else is a fair-use bound so one tenant — or the anonymous demo — cannot take
it all.

### What happens when you hit one

**Nothing queues.** A submission over any of these bounds is refused
immediately with `429` and a machine-readable `rate-limited` code; the body
says which bound and what the limit is (`"2 runs already in progress; the
limit is 2"`). There is no waiting room, no retry-after scheduling on our
side, and no partial acceptance — the request simply did not happen, and
retrying is the caller's decision.

That is deliberate. A queue on a facade in front of a single batch instance
would hide the scarcity rather than communicate it: submissions would sit in
a buffer the caller cannot see, time out somewhere in the middle, and turn a
crisp "not now" into an unbounded wait. Refusing fast keeps the caller in
control of their own retry policy, and keeps the facade stateless about work
it has not accepted.

Before refusing on concurrency, the facade first refreshes your non-terminal
runs against upstream — so a slot freed by a run that finished a minute ago
is noticed, and you are not refused on stale bookkeeping.

## Retention

| What | Window | Variable |
|---|---|---|
| Tenant results | 7 days | `NETNL_RESULT_RETENTION_DAYS` |
| Demo results | 24 hours | `NETNL_DEMO_RETENTION_HOURS` |
| Audit records | 90 days | `NETNL_AUDIT_RETENTION_DAYS` |

Windows are applied by `netnl-admin prune`, which runs on a cron; the cron
cadence is what actually bounds these in practice (see
[deploy-facade.md](../how-to/deploy-facade.md#6-schedule-prune)). The audit
trail is append-only — a database trigger refuses `UPDATE` and `DELETE`, so
only the prune pass's own retention window removes rows.

Fetch your results before the window closes. The facade stores what the
upstream instance returned; it is not an archive.

## Batch results are not website results

A score from this API will not always match what internet.nl's website shows
for the same domain: the batch API skips the connection test, does DNSSEC
without the registrar lookup, and does no A/AAAA prechecks. The full list of
differences is in
[self-hosted.md](self-hosted.md#batch-results-are-not-website-results).

## Not an SLA

Homelab-grade, best-effort, no managed-service guarantee: one upstream
instance, one facade, one operator, no on-call. It can be down while Mark is
asleep. Free tenants and supporter keys get the same treatment — a donation
buys a lifetime credential, not a support contract.
