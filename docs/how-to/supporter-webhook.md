---
status: current
last_reviewed: 2026-09-03
---

# Automatic supporter-key issuance

`POST /webhooks/bmc`, opt-in via `NETNL_BMC_WEBHOOK_SECRET`, turns a
qualifying Buy Me a Coffee (BMC) donation into a `netnl` tenant credential,
mailed directly to the donor — see
`openspec/changes/add-supporter-issuance/design.md` for the pinned
decisions this runbook operates. It replaces the manual half of
[Supporter keys](supporter-key.md): an operator no longer has to notice a
donation and run `netnl-admin user add` by hand, though that fallback still
works, unchanged, when the bridge is disabled.

## What this is not

This is not a payment integration. BMC remains the payment processor; the
facade only consumes its webhook notification after a donation has already
been captured. No card data, refund handling, or subscription state exists
here.

## Prerequisites

Alongside the ordinary facade settings (`NETNL_UPSTREAM_*`, `NETNL_DB`,
...), the bridge additionally requires:

- `NETNL_PUBLIC_ENDPOINT`: the facade's own public batch endpoint URL,
  interpolated into the credential mail.
- `NETNL_SMTP_HOST` and `NETNL_SMTP_FROM`: the bridge sends mail, so an
  SMTP relay must exist. `NETNL_SMTP_PORT`/`_USERNAME`/`_PASSWORD`/`_MODE`/
  `_TIMEOUT` tune it further — see `deploy/.env.example` for the full list
  and defaults.

Setting `NETNL_BMC_WEBHOOK_SECRET` while any of these is missing fails the
facade's startup outright (`SettingsError`, naming the missing variable) —
there is no state where the secret is live but mail delivery is
half-configured.

## 1. Generate the secret

```sh
openssl rand -hex 32
```

Treat this exactly like the upstream credential: never commit it, never
print it in a log line. In the co-located topology it goes in
`deploy/.env` (gitignored); in the K8s topology it goes in the
`netnl-secrets` equivalent alongside the upstream credential — see
[deploy-facade.md](deploy-facade.md) for both. Rotating it later is a
plain redeploy with a new value; BMC's dashboard needs the same new value
set at the same time, or every delivery starts failing signature
verification (401) until both sides agree again.

## 2. Configure the BMC dashboard

On the BMC dashboard's webhook configuration for this account:

1. Set the webhook URL to `https://<your-facade-host>/webhooks/bmc`.
2. Set the shared secret to the value generated above.
3. **Note the exact name of the header BMC signs the payload into.** This
   was not confirmed against a live delivery at build time — the facade
   defaults to `X-Signature-Sha256`
   (`NETNL_BMC_SIGNATURE_HEADER`), but if the dashboard shows a different
   header name, set `NETNL_BMC_SIGNATURE_HEADER` to match before relying on
   the bridge. `bmc.verify_signature` accepts the digest either hex- or
   base64-encoded, with an optional `sha256=` prefix, so the *encoding*
   should not need adjusting — only the header *name*, if it differs.

## 3. Roll out and verify the gate is on

Deploy with `NETNL_BMC_WEBHOOK_SECRET` (and the mail variables) set, then
confirm the bridge actually enforces its signature before trusting it with
a real donation:

```sh
curl -i -X POST https://<your-facade-host>/webhooks/bmc \
  -H 'Content-Type: application/json' \
  -d '{}'
```

Expect **401**. If instead you get **501**, the bridge is not enabled at
all (`NETNL_BMC_WEBHOOK_SECRET` is unset, or the deploy has not picked up
the new environment) — fix that before proceeding. A 401 here is the
signal that the route exists and rejects an unsigned request, which is the
entire security boundary this bridge has (see "Security notes" below).

## 4. Test with a real (test-mode) delivery

BMC's dashboard can usually send a test delivery. By default the facade
ignores anything not in `live_mode` (`IGNORE_TEST_MODE`) — deliberately,
so a stray test delivery from the dashboard never mints a real credential.
To exercise the bridge end to end with a test delivery:

```sh
NETNL_BMC_ACCEPT_TEST_MODE=1   # set alongside the other supporter variables
```

Trigger the test delivery from the BMC dashboard, then check the facade
logs (`logging.getLogger("netnl.supporter")`) for `event=issued`, and
confirm a mail arrived with a fresh credential. **Afterwards:**

1. Unset `NETNL_BMC_ACCEPT_TEST_MODE` and redeploy — leaving it set means
   every future test delivery (including one triggered by someone else
   with dashboard access) mints a real, working credential.
2. Revoke the test credential:
   ```sh
   netnl-admin user revoke <the-generated-username>
   ```
   (the username is in the credential mail, or `netnl-admin user list`).

## 5. Live

Once the above holds, real donations mint keys with no further operator
action. Every donation on the account qualifies by default
(`NETNL_SUPPORTER_MIN_AMOUNT=0`) — raise it, or set
`NETNL_SUPPORTER_CURRENCY`, if that default does not fit; see
`deploy/.env.example` for both.

## Troubleshooting

| Symptom | Likely cause | What to check |
|---|---|---|
| Every delivery gets 401 | Header name or secret mismatch | Re-check the exact signature header name in the BMC dashboard against `NETNL_BMC_SIGNATURE_HEADER`; re-confirm both sides have the same secret |
| Facade refuses to start after setting the secret | A required mail/endpoint variable is missing | The `SettingsError` names the missing variable — see "Prerequisites" above |
| A real donation produced no mail and no error visible to the donor | Check facade logs for `event=delivery-failed txn_id=...`; the transaction is retried by BMC's own retry schedule, or `netnl-admin` can inspect `supporter_issuance` directly against the database file | Confirm SMTP credentials/relay are reachable from the facade; a transaction stuck past `NETNL_SUPPORTER_MAX_ATTEMPTS` is parked — a operator-issued key via the manual fallback ([supporter-key.md](supporter-key.md#manual-issuance-fallback)) unblocks that donor immediately |
| A donation with no email on file never issues | Working as intended (`UNDELIVERABLE_NO_EMAIL`) | There is no credential to reissue automatically — use the manual fallback if the donor can be reached another way |
| A donation from a test/sandbox delivery unexpectedly minted a key | `NETNL_BMC_ACCEPT_TEST_MODE=1` was left set | Unset it and redeploy; revoke the resulting credential |

## Security notes

- **The shared secret is the only gate.** There is no IP allowlist, no
  mTLS — anything that can produce a valid HMAC-SHA256 signature under the
  configured secret can trigger issuance. Treat it with the same care as
  the upstream credential.
- **Rotation** is a plain redeploy with a new secret value, updated on both
  sides at the same time (see step 1). There is no overlap window where two
  secrets are simultaneously valid — a delivery signed under the old secret
  after rotation gets 401, which is why BMC's own retry schedule (not a
  grace period on the facade) is what recovers a delivery that lands mid-
  rotation.
- **Replay is bounded, not prevented outright.** A resent, validly-signed
  delivery for a transaction id already `delivered` is a safe no-op (no
  second credential, no second mail) for as long as that row exists — see
  "Retention" below for how long that is. There is no separate replay
  window or nonce; idempotency is entirely a function of the transaction id
  BMC itself assigns.
- **No supporter PII is ever stored.** The donor's email address and name
  exist only in memory for the duration of sending the mail. The database
  keeps only the transaction id, the generated username, delivery state,
  an attempt counter, and timestamps.
- **Retention.** `supporter_issuance` rows are pruned on the same
  `NETNL_AUDIT_RETENTION_DAYS` cutoff the audit trail uses (no separate
  retention variable) — an operator lengthening or shortening
  `NETNL_AUDIT_RETENTION_DAYS` moves this window too. Once a row is pruned,
  a resent delivery for that transaction id is treated as brand new (a
  fresh credential is minted) rather than recognised as a duplicate — in
  practice this only matters for a delivery BMC retries long after the
  retention window, which is not the ordinary case.
- **`NETNL_SUPPORTER_NOTIFY` (optional).** Set this to an operator address
  to get a short mail ("supporter key issued: `<username>`, txn `<id>`")
  after every successful delivery — no password, no supporter PII beyond
  what BMC's own dashboard/notifications already show the operator. A
  failure to send this notification is logged and never turns a successful
  delivery into a failure.

## See also

- [Supporter keys](supporter-key.md) — the issuance model this bridge
  automates, and the manual fallback.
- `openspec/changes/add-supporter-issuance/design.md` — the pinned
  decisions (D1-D5), configuration table, and route ordering this runbook
  operates.
- [deploy-facade.md](deploy-facade.md) — where the facade's own secrets
  live in each supported topology.
