---
status: draft
last_reviewed: 2026-09-03
---

# Supporter keys: a lifetime credential for a small donation

This page describes the intended issuance model for opening the
`netnl` facade beyond the handful of known users in
[Running the netnl private beta](beta.md) — task 5 of
`openspec/changes/add-measurement-api` ("community opening"). It is
**draft**, not yet the live process: the donation link it depends on
does not exist yet (see "Open TODO" below). Treat this page as the
documented plan, and `beta.md` as the process that is actually live
today.

## The model, in one paragraph

Someone makes a small, one-off donation and gets back a `netnl`
tenant credential (`INTERNETNL_USERNAME`/`INTERNETNL_PASSWORD`) that
does not expire. There is no subscription, no recurring billing, and
no tiered plans — one donation, one lifetime key. What keeps that
sustainable is not a metered quota tied to the donation; it is the
same per-tenant rate limit every credential already gets (see "Fair
use, not a paid tier" below).

## Beta, best-effort, no SLA — explicitly

A supporter key is issued against the same homelab-grade facade and
instance described in
[deploy-facade.md](deploy-facade.md#not-an-sla) and
[deploy-instance-vps.md](deploy-instance-vps.md#not-an-sla): one
facade container, one VPS instance, cron-scheduled retention, no
managed-service guarantee. A donation buys a **lifetime key**, not
**lifetime uptime** — the service can go down, change limits, or (in
the worst case) be discontinued, and "I donated" does not create an
SLA the operator does not otherwise offer. State this plainly to
every supporter at issuance time, the same way `beta.md`'s "Terms"
section states it to beta users.

## Fair use, not a paid tier

A supporter key is a `netnl` tenant credential like any other —
subject to the same defaults as `beta.md`'s table:

| Default | Meaning |
|---|---|
| `NETNL_RATE_LIMIT=10` | submissions/credential/hour |
| `NETNL_MAX_DOMAINS=500` | domains/request |
| `NETNL_MAX_CONCURRENT=2` | non-terminal runs/credential |

Donating does not raise these. The rate limit is the mechanism that
makes "lifetime, best-effort, no metering" sustainable at all: it
bounds how much load any one credential — donor or beta user — can
put on the underlying instance, so the facade does not need a paid
tier, usage-based billing, or expiry to stay operable. If a supporter
has a legitimate need that does not fit the defaults, that is the
same "observe and adjust the default" loop `beta.md` already
describes, not a reason to special-case one credential.

## Issuing a supporter key (operator procedure)

Once a donation is confirmed (out of band — the facade has no
payment integration and none is planned), issue the credential the
same way as any other `netnl-admin` user, using the donor's chosen
name as the tenant identifier:

```sh
kubectl -n netnl exec deploy/netnl -- netnl-admin user add <donor-name>
```

(Or the compose form, `docker compose -f deploy/compose.yaml exec
netnl netnl-admin user add <donor-name>`, in the co-located topology
— see [deploy-facade.md](deploy-facade.md).) This prints the
generated password **once**; hand the username/password pair to the
donor out of band, together with:

- The endpoint (`https://api.westerweel.work`) and the pointer to the
  CLI quickstart in the [README](../../README.md#quickstart) — see
  `beta.md`'s "Onboarding a beta user" for the exact wording, which
  applies unchanged here.
- The terms from "Beta, best-effort, no SLA" above, stated plainly,
  every time — not left implicit.
- The rate-limit table from "Fair use, not a paid tier" above, so a
  supporter understands *why* a 429 can happen and that it is not a
  bug or a broken promise.

## Revoking a supporter key

Revocation is the same operation as for any other tenant, effective
immediately, no grace period:

```sh
kubectl -n netnl exec deploy/netnl -- netnl-admin user revoke <donor-name>
```

"Lifetime" describes the intended issuance policy (no built-in
expiry), not an unrevokable guarantee — abuse of the rate limit,
measuring hosts without permission, or the operator discontinuing the
service are all still grounds to revoke, same as for a beta
credential.

## Open TODO: the donation link

<!-- TODO: replace with the real donation link once it exists. -->
**Donation link — volgt.** No payment page exists yet; this page
intentionally does not invent one. Until a real link is in place,
"supporter key" is a documented model, not a self-serve flow —
issuance still goes through the same manual, out-of-band `netnl-admin`
process as a beta credential.

## See also

- [Running the netnl private beta](beta.md) — the process that is
  actually live today, for the handful of known users.
- [Use in CI](ci.md#e-getting-a-credential) — where this page is
  linked from as one of the ways to get a credential.
