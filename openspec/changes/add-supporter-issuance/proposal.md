# Change: add-supporter-issuance

## Why

[Supporter keys](../../../docs/how-to/supporter-key.md) already describe the
model — a small donation buys a lifetime `netnl` tenant credential — and the
donation link (<https://buymeacoffee.com/mark.westerweel>) is live. Issuance
itself is still entirely manual: an operator watches for a Buy Me a Coffee
(BMC) notification, then runs `netnl-admin user add <name>` and hands the
printed password to the donor out of band. That does not scale past a
handful of donations and puts a person in the loop for something a webhook
can do automatically.

This change closes that gap with the smallest bridge that can plausibly be
trusted: BMC's webhook, verified, drives credential issuance and delivery
directly. It does not change the fair-use model in
`docs/how-to/supporter-key.md` — a supporter key is still an ordinary
tenant credential subject to the same `NETNL_RATE_LIMIT`/`NETNL_MAX_DOMAINS`/
`NETNL_MAX_CONCURRENT` defaults — only *how the credential reaches the
donor* changes.

## What Changes

**An opt-in webhook bridge**, `POST /webhooks/bmc`, gated entirely by
`NETNL_BMC_WEBHOOK_SECRET`:

1. **Signed deliveries only.** Every request is verified with HMAC-SHA256
   over the raw request body, timing-safe, before anything else happens — no
   database connection is opened, no password hash is computed, no mail is
   sent and no audit row is written for a request that fails this check. An
   invalid or missing signature answers 401 with no further detail.
2. **Narrow qualification.** Only a `donation.created` event in `live_mode`
   (BMC's own test/live flag) at or above an operator-configured minimum
   amount (default: 0 — the owner's decision is that *every* donation on
   the live account mints a key) qualifies for issuance. Anything else — a
   different event type, a test-mode delivery (unless explicitly accepted
   for the integration test), an amount below the threshold, or a currency
   mismatch when one is configured — is acknowledged 200 and otherwise
   ignored: no state is written, no mail is sent, no credential is touched.
3. **Persist-then-mail, with revoke-on-mail-failure.** A qualifying delivery
   mints a credential and records an idempotency row for its BMC transaction
   id in a single `BEGIN IMMEDIATE` transaction, *before* any mail is sent.
   Mail delivery happens outside that transaction. If it fails, the
   just-minted credential is revoked and the row is marked failed
   (with an attempt counter) inside its own short transaction, and the
   facade answers 503 so BMC retries the webhook — a retry mints a fresh
   key and revokes the previous (now-orphaned) one. The invariant this
   protects: at most one *active* credential exists per BMC transaction at
   any time, and a credential that could not be delivered never stays
   usable.
4. **No supporter PII at rest.** The donor's email address and name exist
   only in memory for the duration of sending the mail. Persistent state is
   limited to the BMC transaction id, the generated username, delivery
   state, an attempt counter, and timestamps.
5. **Bounded and audited.** Issuance is capped to a configurable number per
   hour (protects the underlying instance from a burst exactly like the
   demo family's own per-IP cap protects it from anonymous traffic), and
   every state transition is audited — without ever writing a secret
   (password, webhook secret, SMTP credential) to the audit trail.
6. **An operator notification, opt-in.** When `NETNL_SUPPORTER_NOTIFY` is
   set, a short second mail ("supporter key issued: `<username>`, txn
   `<id>`") goes to the operator after a successful delivery — no password,
   no supporter PII beyond what BMC itself already mailed. A failure to send
   this notification is logged and never turns a successful delivery into a
   failure.

## Non-goals

- **No change to the fair-use model.** A supporter key issued this way is
  identical to one issued by hand — same defaults, same revocation
  mechanism (`netnl-admin user revoke`), same "beta, best-effort, no SLA"
  terms in `docs/how-to/supporter-key.md`.
- **No payment processing.** BMC remains the payment processor; this change
  only consumes its webhook notification. No card data, payment method, or
  refund handling exists here or is ever proposed.
- **No supporter-facing dashboard, login, or self-service credential
  recovery.** The credential arrives once, by mail, exactly like the manual
  process it replaces — there is no second channel to retrieve a lost one
  short of the operator reissuing it out of band.
- **No relaxing of `Authenticated surface`'s existing invariant** ("no
  measurement route SHALL be anonymous"). `/webhooks/bmc` is not a
  measurement route; it is a separate, signed-only bridge, off by default,
  that never touches the upstream instance or existing tenant data.

## Impact

- **A third opt-in surface**, alongside `/demo/*` and `security.txt`: unset
  `NETNL_BMC_WEBHOOK_SECRET` means `/webhooks/bmc` does not exist as far as
  any caller can tell — the same 501 not-implemented catch-all as any other
  unmapped path.
- **A new outbound dependency: SMTP.** This is the first thing in the
  facade that sends mail. Its configuration fails closed at startup
  (`SettingsError`, naming the missing variable) exactly like every other
  required setting — there is no way to have the webhook secret set but the
  mail path half-configured.
- **A new table, `supporter_issuance`**, and one new retention path pruning
  it on the existing audit-retention cutoff — no new retention variable.
- **Shared issuance code.** `netnl-admin user add` and the webhook bridge
  now mint credentials through the same helper (`netnl/issue.py`), so the
  two paths cannot silently diverge in how a credential is created.
