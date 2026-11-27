---
status: current
last_reviewed: 2026-11-27
---

# Automatic supporter-key issuance

`POST /webhooks/bmc`, opt-in via `NETNL_BMC_WEBHOOK_SECRET`, turns a
qualifying Buy Me a Coffee (BMC) donation into a `netnl` tenant credential,
mailed directly to the donor — see
`openspec/changes/archive/2026-09-08-add-supporter-issuance/design.md` for the pinned
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
action. A donation qualifies by default once it is **2.00 or more** in the
account's currency (`NETNL_SUPPORTER_MIN_AMOUNT=2`) — raise it, lower it, or
set `NETNL_SUPPORTER_CURRENCY`, if that default does not fit; see
`deploy/.env.example` for all three. The credential mail is sent as
plaintext with an HTML alternative (`multipart/alternative`) — the HTML
part loads no image, font, or stylesheet, and makes no network request of
any kind; a client that renders only plaintext sees exactly the same
credential.

## Troubleshooting

| Symptom | Likely cause | What to check |
|---|---|---|
| Every delivery gets 401 | Header name or secret mismatch | Re-check the exact signature header name in the BMC dashboard against `NETNL_BMC_SIGNATURE_HEADER`; re-confirm both sides have the same secret |
| Facade refuses to start after setting the secret | A required mail/endpoint variable is missing | The `SettingsError` names the missing variable — see "Prerequisites" above |
| A real donation produced no mail and no error visible to the donor | Check facade logs for `event=delivery-failed txn_id=...` (`logging.getLogger("netnl.supporter")`); if that's inconclusive, inspect the `supporter_issuance` table directly with `sqlite3 <db-path> "SELECT * FROM supporter_issuance WHERE txn_id = '<id>'"` (there is no `netnl-admin` subcommand for this — it is a plain read-only query against the facade's own SQLite file) | Confirm SMTP credentials/relay are reachable from the facade; a transaction stuck past `NETNL_SUPPORTER_MAX_ATTEMPTS` is parked — an operator-issued key via the manual fallback ([supporter-key.md](supporter-key.md#manual-issuance-fallback)) unblocks that donor immediately |
| A donation with no email on file never issues | Working as intended (`UNDELIVERABLE_NO_EMAIL`) | There is no credential to reissue automatically — use the manual fallback if the donor can be reached another way |
| A donation from a test/sandbox delivery unexpectedly minted a key | `NETNL_BMC_ACCEPT_TEST_MODE=1` was left set | Unset it and redeploy; revoke the resulting credential |
| A transaction is parked (503 forever) after several retries, with no obvious mail failure | A slow or intermittently-reachable SMTP relay burned through `NETNL_SUPPORTER_MAX_ATTEMPTS` — each of BMC's own retries counts as one more attempt, whether it failed outright or simply timed out | Check facade logs for repeated `event=delivery-failed` for the same `txn_id`; fix the relay/network path, then issue the donor a credential by hand via the manual fallback ([supporter-key.md](supporter-key.md#manual-issuance-fallback)) rather than waiting for a further BMC retry (the transaction stays parked until an operator intervenes) |
| An audit row named `supporter-deliver-orphaned` appears for a transaction | A concurrent-delivery edge case (see design.md's "B1(b), a conditional outcome write") — vanishingly rare in practice, but not impossible at a lease boundary | The donor may already have received a mail carrying a now-revoked (dead) credential from the losing side of the race; the winning takeover's own mail carries the one that actually works. No action needed unless the donor reports the first mail's credential not working — that is expected; point them at the most recent mail for that donation |

## Security notes

- **The shared secret is the only gate.** There is no IP allowlist, no
  mTLS — anything that can produce a valid HMAC-SHA256 signature under the
  configured secret can trigger issuance. Treat it with the same care as
  the upstream credential. The facade refuses to start with a secret
  shorter than 32 characters (a floor against an obviously-too-short
  accidental value, not a strength target); `openssl rand -hex 32` (step
  1) is 64.
- **`NETNL_SMTP_MODE=plaintext` also puts the SMTP relay password on the
  wire unencrypted**, exactly like the credential mail body itself — it
  requires the explicit `NETNL_SMTP_ALLOW_PLAINTEXT=1` opt-in for that
  reason, same as `NETNL_ALLOW_HTTP` for the upstream hop. Prefer
  `starttls` or `ssl` unless the relay is genuinely local/trusted.
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
- **Retention, and post-prune replay (accepted residual risk).**
  `supporter_issuance` rows are pruned on the same
  `NETNL_AUDIT_RETENTION_DAYS` cutoff the audit trail uses (no separate
  retention variable) — an operator lengthening or shortening
  `NETNL_AUDIT_RETENTION_DAYS` moves this window too. Once a row is pruned,
  a resent, validly-signed delivery for that transaction id is
  indistinguishable from a brand-new one: it mints a *second* credential,
  and the original stays active unless separately revoked. This is
  accepted, not closed by a tombstone table — see
  `openspec/changes/archive/2026-09-08-add-supporter-issuance/design.md`, "Post-prune
  replay", for why (in short: closing it that way reintroduces the
  unbounded-growth problem retention exists to bound, against a threat
  that already requires the webhook secret or a captured signed request —
  at which point a live forgery is no harder than a replay). If you spot a
  donor with two active credentials from one donation, revoke the extra
  one by hand.
- **`NETNL_SUPPORTER_NOTIFY` (optional).** Set this to an operator address
  to get a short mail ("supporter key issued: `<username>`, txn `<id>`")
  after every successful delivery — no password, no supporter PII beyond
  what BMC's own dashboard/notifications already show the operator. A
  failure to send this notification is logged and never turns a successful
  delivery into a failure.

## Bazibo unlock codes

The same webhook also sells the full web version of
[Bazibo](https://github.com/MWest2020/bazibo): a €1 BMC **Extra**,
"Bazibo volledige versie (web)", gets the buyer a mailed unlock code
(owner decision, 27 Nov 2026). Design:
`openspec/changes/add-bazibo-unlock/design.md`.

### How routing works

The bridge looks at the event type first, before anything else in the
payload:

| BMC event | What happens |
|---|---|
| `donation.created` | Unchanged: a netnl credential at or above `NETNL_SUPPORTER_MIN_AMOUNT` (default 2). Never a Bazibo code. |
| `extra_purchase.created` whose `data.extras[]` holds the configured Extra | One Bazibo unlock code, mailed. Never a netnl credential. |
| `donation.created` ("support created") with `support_type: "Extra"` or a non-empty `extras[]` | An Extra purchase delivered as support: same as the row above, never a netnl credential. Without the Bazibo Extra in `extras[]` (or while dormant): 200 `ignored`. |
| `extra_purchase.created` for any other Extra | 200 `ignored`, nothing written. |
| anything else (memberships, refunds, commissions, unknown types) | 200 `ignored`, nothing written, whatever the payload looks like. Logged by type only (`type=other` for a type BMC does not list). |

A donation that says "for Bazibo" in its note is still a donation: it
follows the donation rules and never mints a code. Only the structured
fields `support_type` and `extras[]` decide. If that happens, issue a code
by hand (below). Since 27 Nov 2026 the web unlock is sold through a Stripe
Payment Link (next section), because BMC sent no webhook for real Extra
purchases and its coffee price cannot be €1.

Every signed `donation.created`/`extra_purchase.created` logs its shape,
without personal data:

```text
event=bmc-shape type=donation.created support_type=Extra extras=list(1) extra_ids=603413
```

On Mark's account the BMC dashboard offers only "support created" as a
webhook event. Whether an Extra purchase arrives that way, and with which
fields, is learnt from that line on the first real purchase.

The payload shape comes from BMC's published webhook spec
(`https://cdn.buymeacoffee.com/assets/integrations/bmc-webhooks-openapi.json`):
envelope `type`/`live_mode`/`attempt`/`data`; for Extras `data.transaction_id`,
`data.amount`, `data.currency`, `data.supporter_email` and
`data.extras[] = {id, title, amount, quantity, ...}`. A real
`extra_purchase.created` from this account has not been seen yet; check the
first one with the steps under "Switching it on".

### Stripe Payment Link: `POST /webhooks/stripe`

Mark, 27 Nov 2026: the Bazibo web unlock is a Stripe Payment Link at €1
(design.md D12). The route exists while `NETNL_STRIPE_WEBHOOK_SECRET` and
`NETNL_STRIPE_PAYMENT_LINK_ID` are both set (they live in the `netnl-bridge`
Secret; one without the other refuses to start).

1. The `Stripe-Signature` header is checked first (HMAC-SHA256 over
   `"<t>." + body` with the endpoint secret, at most 300 s old). Anything
   else: 401, nothing written.
2. `checkout.session.completed` (or `checkout.session.async_payment_succeeded`)
   with `payment_status: "paid"` and `payment_link` equal to
   `NETNL_STRIPE_PAYMENT_LINK_ID`: one code, mailed to
   `customer_details.email`, keyed on the Checkout Session id (`cs_…`) in
   `bazibo_issuance`. A replay sends nothing; a mail failure answers 503 and
   Stripe retries (for up to three days) with the same code.
3. Any other event, another payment link, an unpaid session: 200 `ignored`.
   A test-mode session (`livemode: false`) is ignored unless
   `NETNL_STRIPE_ALLOW_TEST_MODE=1`: a code from a test payment is a real
   code.

Setting it up (Mark gives a restricted key: Products, Prices, Payment Links,
Webhook Endpoints, all "Write"; test mode first):

```sh
STRIPE_API_KEY=rk_test_… uv run netnl-stripe-setup
# mode: test
# created: product, price, payment link, webhook endpoint
# payment link: https://buy.stripe.com/test_…
# NETNL_STRIPE_PAYMENT_LINK_ID=plink_…
# NETNL_STRIPE_WEBHOOK_SECRET=whsec_…
```

It finds what exists by metadata `bazibo=web-unlock` and creates only what
is missing. Stripe shows a webhook secret only when the endpoint is
created; `--new-webhook-secret` recreates the endpoint for a new one. Put
both values in `netnl-bridge` (plus `NETNL_STRIPE_ALLOW_TEST_MODE: "1"` in
the ConfigMap while testing), restart, pay once with a test card
(4242 4242 4242 4242) and look for `event=bazibo-issued source=stripe`.
For live, run the same with the live key, replace both values, remove the
test-mode switch. The payment link URL goes into Bazibo's `PAY_URL`.

### The code and the mail

The code is `BZB1.<payload>.<signature>` (format v1, shared with the Bazibo
verifier): 16 bytes of payload (version, product, issued day, 10 random
bytes = the **code id**) signed with Ed25519. The mail is Dutch and English
in one message, with the link `https://bazibo.nl/#unlock=<code>`, the
GitHub Pages alternative, the code as text, a QR code as an inline image,
and one line on how the code behaves. It loads nothing from the internet
when opened.

Per purchase the database keeps the transaction id, the code id, state,
an attempt counter and timestamps (`bazibo_issuance`). Not the buyer's
email, not the code. A mail failure answers 503 so BMC retries; the retry
re-sends the same code. A transaction that has used all
`NETNL_SUPPORTER_MAX_ATTEMPTS` is parked and answered 200 (no use letting
BMC retry: after 10 consecutive failures BMC disables the whole webhook).

### Activation: `POST /bazibo/activate`

Bazibo's web build calls this once per browser:

```http
POST https://api.westerweel.work/bazibo/activate
Origin: https://bazibo.nl
Content-Type: application/json

{"code": "BZB1.…", "device": "<32 hex characters, random, kept in the browser>"}
```

| Reply | Meaning |
|---|---|
| 200 `{"status":"ok"}` | Counted (or already counted) device. |
| 409 `{"error":"limit"}` | This code already has `NETNL_BAZIBO_DEVICE_LIMIT` (10) other devices. |
| 403 `{"error":"revoked"}` | The code is revoked (by hand, or automatically after 100 distinct devices). |
| 400 `{"error":"invalid"}` | Not a valid signed code, or not exactly `{code, device}`. |
| 429 `{"error":"rate"}` | More than 10 requests a minute from one address. |
| 403 `{"error":"origin"}` | Called from a page other than `https://bazibo.nl` or `https://mwest2020.github.io`. |
| 503 `{"error":"off"}` | Feature not switched on yet. Bazibo unlocks anyway (lenient). |

The signature is checked before anything touches the database. A device
already counted keeps succeeding. The eleventh distinct device gets 409.
At 100 distinct devices on one code the code is revoked for everyone; its
code id is logged (`event=bazibo-auto-revoked code_id=…`), as it is the first
time a code passes the limit (`event=bazibo-device-limit`). CORS allows only
the two Bazibo origins, `POST` and `OPTIONS`, no credentials.

### Hoe we het bijhouden / How we keep track

*Nederlands.* Als je je code invoert, vraagt Bazibo onze server één keer per
browser of de code klopt. Je browser maakt daarvoor zelf een willekeurig
nummer aan dat alleen op dat apparaat bewaard wordt. Op de server bewaren we
per code alleen: het code-nummer, een versleutelde vingerafdruk van dat
willekeurige nummer (zonder onze geheime sleutel valt die niet terug te
rekenen), en de datum waarop we het apparaat voor het eerst en voor het
laatst zagen. Geen tijdstip, geen IP-adres, geen browsergegevens, geen
e-mailadres. Zo tellen we hoeveel apparaten een code gebruiken: tien is
prima. Pas als één
code op honderd verschillende apparaten opduikt, zetten we hem uit. Om
misbruik af te remmen onthouden we heel kort, alleen in het werkgeheugen en
nooit op schijf, hoe vaak een netwerkadres het in de laatste minuut
probeerde. Je e-mailadres gebruiken we alleen om de code te sturen; we
bewaren het niet. Het verzoek loopt via Cloudflare, dat je IP-adres ziet
om het verkeer af te leveren; onze eigen server schrijft het niet op.

*English.* When you enter your code, Bazibo asks our server once per
browser whether the code is valid. For that, your browser makes up a random
number that stays on that device. On the server we keep, per code, only:
the code number, a keyed fingerprint of that random number (it cannot be
turned back without our secret key), and the dates we first and last saw
the device. No time of day, no IP address, no browser details, no email
address. That is how we count how many devices use a code: ten is fine.
Only when one code turns up on a hundred different devices do we switch it off. To slow down abuse
we remember, briefly, in memory only and never on disk, how often a network
address tried in the last minute. Your email address is used only to send
the code; we do not keep it. The request travels through Cloudflare, which
sees your IP address in order to deliver the traffic; our own server does
not write it down.

(Operator notes behind that text: the per-address limit keys on
`CF-Connecting-IP`, which Cloudflare sets; on the Tailscale Funnel ingress
to the same Service a client can set it itself and so dodge its own limit,
but not affect anyone else's (the table is a bounded LRU). The mail links
put the code in the fragment (`#unlock=`), which browsers never send to the
web server, so it does not appear in the access logs of bazibo.nl or GitHub
Pages; Bazibo still accepts the older `?unlock=` form. See design.md O6.

(Operator notes behind that text: device rows and purchase rows are kept
for as long as the code exists; there is no expiry. uvicorn's access log
skips `/bazibo/*`, and in the production topology its peer address is the
cloudflared pod anyway. cloudflared at its default log level writes no
per-request lines.)

### Switching it on

Production runs the code path **dormant**: the signing key and the device
salt are in the `netnl-bridge` Secret, the public-key cross-check and
`NETNL_BAZIBO_MIN_AMOUNT=1` are in the ConfigMap, and
`NETNL_BAZIBO_EXTRA_ID` is absent. To switch on:

1. The Extra exists: "Bazibo full version unlock!", €1, id **603413**.
2. In homelab, `cluster-config/infra/netnl/configmap.yaml`, add the one line

   ```yaml
   NETNL_BAZIBO_EXTRA_ID: "603413"
   ```

   and merge; Argo syncs and the pod restarts. A missing salt or key now
   fails the startup loudly instead of running half-configured. That also
   holds for every other process that reads the same ConfigMap and loads
   the facade settings (`netnl-prune` runs `netnl-admin prune`): it must
   mount the `netnl-bridge` Secret too, as the Deployment does.
3. BMC dashboard → Integrations → Webhooks: keep **support created**
   ticked (it is the netnl donation path, and the only event this account
   offers). If an extra-purchase event ever appears there, tick it too;
   both events of one purchase share a transaction id and mail once.
4. Check: `curl -s -X POST https://api.westerweel.work/bazibo/activate -H
   'Origin: https://bazibo.nl' -d '{}'` answers 400 `invalid` (no longer
   503 `off`).
5. First real purchase: buy the Extra once yourself, then
   `kubectl -n netnl logs deploy/netnl | grep -E 'bmc-shape|bazibo|ignored'`.
   - `event=bazibo-issued txn_id=… code_id=…` and a mail: done.
   - `event=bmc-shape … extras=absent` with `support_type=Extra` or
     `absent`: the delivery does not identify the Extra. Issue the code by
     hand from BMC's sale notification (below) and report the shape line.
   - No `bmc-shape` line at all: BMC sent no webhook for the purchase.
     Issue by hand. If the dashboard's test event is used, it arrives
   with `live_mode: false` and is ignored unless
   `NETNL_BMC_ACCEPT_TEST_MODE=1` is set for the test.

### The watch: a code that did not go out

Mark, 27 Nov 2026: "geen zin in ad hoc oplossingen". `netnl-admin bazibo
watch` runs as the CronJob `netnl-bazibo-watch` every 5 minutes. It mails
`NETNL_BAZIBO_ALERT_TO` once per transaction that qualified for a code and
has none delivered `NETNL_BAZIBO_ALERT_AFTER_MINUTES` (15) after it was
first seen, with the subject "Bazibo-code niet verstuurd voor transactie
<txn> — controleer". It covers:

- a qualifying donation or Extra with no issuance row at all (hourly cap,
  a crash or a database error before the row): the webhook writes a
  `bazibo_watch` row (transaction id, first seen) before anything else;
- an issuance row that is `failed`, parked (attempts used up), `pending`
  past a crash, or `undeliverable` (no address in the webhook).

The alert carries the transaction id (plain token characters only), the
state, attempts and code id, and the hand-issue command for that
transaction. Never the buyer's address, never the code. A transaction is
marked alerted only after its mail went out; a failed alert mail is retried
on the next run (exit 1). Without `NETNL_BAZIBO_ALERT_TO` the command says
"watch off" and exits 0. What it cannot see: a donation BMC never sent a
webhook for (that is what happened to the Extra); compare BMC's own sale
notifications for those.

### Operator commands

```sh
# Issue and mail a code for a purchase, from BMC's sale-notification mail.
# Same idempotency table as the webhook: re-running it, or a webhook that
# arrives later for the same transaction, sends nothing new.
kubectl -n netnl exec deploy/netnl -- netnl-admin bazibo mint --mail <buyer address> --ref <BMC transaction id>
kubectl -n netnl exec deploy/netnl -- netnl-admin bazibo mint            # print one code (lost mail, a "for Bazibo" donation)
kubectl -n netnl exec deploy/netnl -- netnl-admin bazibo show <code-id>  # devices and revocation state
kubectl -n netnl exec deploy/netnl -- netnl-admin bazibo revoke <code-id>
kubectl -n netnl exec deploy/netnl -- netnl-admin bazibo unrevoke <code-id>
```

`mint --mail --ref` prints only the transaction id and code id; it works
while the feature is dormant, and it can take over a transaction the
webhook recorded as undeliverable (no address) or parked. Plain `mint`
prints the code and its links once; it is stored nowhere.

**API poller (dormant).** `netnl-admin bazibo poll` reads BMC's legacy
developer API (`GET /v1/extras`: `purchase_id`, `purchased_on`,
`purchase_is_revoked`, `purchase_amount`, `purchase_currency`,
`payer_email`, `extra.reward_id`, `extra.reward_title`) and mails codes for
recent purchases of the Extra. It does nothing without
`NETNL_BMC_API_TOKEN` (in `netnl-bridge`), and no CronJob runs it: the
developer site did not issue a token. It keys on the API's `purchase_id`,
not the transaction id, so it runs only with `NETNL_BAZIBO_SOURCE=poll`,
which turns the webhook's Bazibo path off, and only for purchases dated at
or after `NETNL_BMC_POLL_NOT_BEFORE`. Settings: `NETNL_BAZIBO_SOURCE`
(`webhook`), `NETNL_BMC_API_TOKEN`, `NETNL_BMC_POLL_NOT_BEFORE`,
`NETNL_BMC_API_BASE`, `NETNL_BMC_POLL_LOOKBACK_HOURS` (168),
`NETNL_BMC_POLL_MAX_PAGES` (10). To find
the code id of a purchase (for example after a refund):
`sqlite3 /data/netnl.sqlite3 "SELECT code_id, state FROM bazibo_issuance WHERE txn_id = '<pi_…>'"`.
The revocation list is the `bazibo_revoked` table (`code_id`, `reason` =
`auto` or `manual`, `revoked_at`).

### Settings

| Variable | Default | Meaning |
|---|---|---|
| `NETNL_BAZIBO_SIGNING_KEY` | unset | Secret. base64 raw 32-byte Ed25519 seed. Presence switch (dormant without an Extra). |
| `NETNL_BAZIBO_EXTRA_ID` | unset | On switch: the BMC Extra id. |
| `NETNL_BAZIBO_EXTRA_TITLE` | unset | Title match, only used when no id is set. |
| `NETNL_BAZIBO_DEVICE_SALT` | unset | Secret, ≥ 32 characters. Required once on. |
| `NETNL_BAZIBO_PUBLIC_KEY` | unset | Startup refuses a key that does not match. |
| `NETNL_BAZIBO_MIN_AMOUNT` | `0` | Minimum payment for the Extra (independent of the netnl minimum). |
| `NETNL_BAZIBO_ALERT_TO` | unset | `bazibo watch`: one alert per undelivered transaction to this address. Off while unset. |
| `NETNL_BAZIBO_ALERT_AFTER_MINUTES` | `15` | How long a transaction may go without a delivered code before the alert. |
| `NETNL_STRIPE_WEBHOOK_SECRET` | unset | Secret. The Stripe endpoint's `whsec_…`; with the link id it switches `/webhooks/stripe` on. |
| `NETNL_STRIPE_PAYMENT_LINK_ID` | unset | The Payment Link (`plink_…`) whose paid sessions mint a code. |
| `NETNL_STRIPE_ALLOW_TEST_MODE` | `0` | `1`: test-mode sessions mint codes too (only while testing). |
| `NETNL_BAZIBO_DEVICE_LIMIT` | `10` | Devices per code. |
| `NETNL_BAZIBO_AUTO_REVOKE_AT` | `100` | Distinct devices that revoke a code. |
| `NETNL_BAZIBO_ACTIVATE_PER_MINUTE` | `10` | Per-address activation requests per minute. |
| `NETNL_BAZIBO_ALLOWED_ORIGINS` | `https://bazibo.nl,https://mwest2020.github.io` | CORS origins. |
| `NETNL_BAZIBO_CLIENT_IP_HEADER` | `CF-Connecting-IP` | Header the rate limit keys on. |

Rotating the signing key invalidates every code already sold (the Bazibo
verifier carries the public key): don't, unless it has leaked.

## See also

- [Supporter keys](supporter-key.md) — the issuance model this bridge
  automates, and the manual fallback.
- `openspec/changes/archive/2026-09-08-add-supporter-issuance/design.md` — the pinned
  decisions (D1-D5), configuration table, and route ordering this runbook
  operates.
- [deploy-facade.md](deploy-facade.md) — where the facade's own secrets
  live in each supported topology.
- `openspec/changes/add-bazibo-unlock/design.md` — Bazibo routing, code
  format, activation and the payload shape from BMC's published spec.
