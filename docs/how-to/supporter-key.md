---
status: current
last_reviewed: 2026-09-03
---

# Supporter keys: a lifetime credential for a small donation

This page describes the issuance model for opening the `netnl` facade
beyond the handful of known users in
[Running the netnl private beta](beta.md) — task 5 of
`openspec/changes/add-measurement-api` ("community opening"). The
donation link (<https://buymeacoffee.com/mark.westerweel>) is live, and
issuance is now automatic once an operator has enabled the webhook
bridge described below — see
[Automatic supporter-key issuance](supporter-webhook.md) for the
operator runbook (enabling it, testing it, troubleshooting it). Manual
issuance, the process this page originally described, remains the
fallback for when the bridge is disabled or the mail path is
unavailable.

## The model, in one paragraph

Someone makes a small, one-off donation and gets back a `netnl`
tenant credential that does not expire — the automatic mail (see
[Automatic supporter-key issuance](supporter-webhook.md)) presents it
as a single `INTERNETNL_CREDENTIAL=username:password` string, plus
copy-paste GitHub Actions and CLI snippets and a link to the CI guide,
because CI/CD only ever needs the one API key, not two loose fields.
There is no subscription, no recurring billing, and
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
SLA the operator does not otherwise offer. The credential mail sent by
the automatic bridge states this plainly; state it just as plainly if
you ever issue a key by hand.

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

## Getting a supporter key

Donate via [Buy Me a Coffee](https://buymeacoffee.com/mark.westerweel).
When the webhook bridge is enabled, a qualifying donation mints a
credential automatically and mails it to the address BMC has on file
for that donation — nothing further to do. If the bridge is disabled
(or a delivery genuinely cannot be completed — see
[Automatic supporter-key issuance](supporter-webhook.md#troubleshooting)),
issuance falls back to the manual process below.

## Automatic issuance (the normal path)

See [Automatic supporter-key issuance](supporter-webhook.md) for the
full operator runbook: enabling `NETNL_BMC_WEBHOOK_SECRET` and the
mail configuration it requires, verifying the bridge actually rejects
an unsigned request, running a test donation, and troubleshooting a
delivery that did not arrive.

In short, once enabled: BMC calls `POST /webhooks/bmc` for every
donation event; the facade verifies the signature, mints a credential
for a qualifying live donation, and mails it directly to the donor —
the same credential shape, the same fair-use limits, and the same
"beta, best-effort, no SLA" terms as a manually-issued key. No
supporter email address or name is ever stored by the facade; only the
BMC transaction id, the generated username, and delivery state are
kept (and pruned on the existing audit-retention window), which is
enough to make a duplicate delivery a safe no-op and a failed delivery
retryable, without holding onto anything that identifies the donor.

## Manual issuance (fallback)

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

Revocation is the same operation regardless of how the key was issued
— automatically by the webhook bridge, or by hand — effective
immediately, no grace period:

```sh
kubectl -n netnl exec deploy/netnl -- netnl-admin user revoke <username>
```

"Lifetime" describes the intended issuance policy (no built-in
expiry), not an unrevokable guarantee — abuse of the rate limit,
measuring hosts without permission, or the operator discontinuing the
service are all still grounds to revoke, same as for a beta
credential.

## See also

- [Automatic supporter-key issuance](supporter-webhook.md) — the
  operator runbook for the webhook bridge: enabling it, testing it,
  and troubleshooting a delivery that did not arrive.
- [Running the netnl private beta](beta.md) — the process this model
  builds on, for the handful of known users onboarded before the
  donation link existed.
- [Use in CI](ci.md#e-getting-a-credential) — where this page is
  linked from as one of the ways to get a credential.
