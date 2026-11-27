# Design: add-bazibo-unlock

## Context

- `POST /webhooks/bmc` is live in production since 2026-09-04 (two real
  deliveries). Its HMAC gate, idempotency table, lease and mail path are in
  `netnl.supporter`; its race history (B1/B2) is in that module's
  docstring.
- The code format is owned jointly with the Bazibo verifier (built in
  MWest2020/bazibo by another agent) and is fixed. The operator copy of the
  format note is `~/.config/bazibo-unlock/FORMAT.md`; the public key is
  committed as `tests/fixtures/bazibo-ed25519.pub`.

## BMC extra-purchase payload (confirmed from BMC's published spec)

BMC publishes an OpenAPI description of its webhooks
(`https://cdn.buymeacoffee.com/assets/integrations/bmc-webhooks-openapi.json`,
"Buy Me a Coffee — Webhook Events" 1.0.0, linked from the help article "How
to setup and use Buy Me a Coffee webhooks"; fetched 2026-11-27). Relevant:

- Envelope on every event: `event_id`, `type`, `live_mode` (false for the
  dashboard's "Send test event"), `created`, `attempt`, `data`.
- Signature: `x-signature-sha256`, hex HMAC-SHA256 of the raw body. This
  matches the bridge's existing default (confirmed by a real delivery on
  2026-09-04).
- Event types: `donation.created|refunded`,
  `extra_purchase.created|updated|refunded`, `commission_order.*`,
  `wishlist_payment.*`, `membership.*`, `recurring_donation.*`.
- `extra_purchase.*` `data` (`ExtraData`): the supporter fields
  (`supporter_name`, `supporter_id`, `supporter_email`, `support_note`),
  `transaction_id` (Stripe PaymentIntent), `status`, `amount`, `currency`,
  `support_type: "Extra"`, and `extras[]` with one entry per shop item:
  `id` (integer), `title`, `amount`, `quantity`, `currency`, ...
- Retries: up to 4, at 15/30/45/60 minutes, `attempt` incremented. After 10
  consecutive failures BMC disables the webhook. A 2xx is expected within
  15 seconds.

Still unconfirmed: a real `extra_purchase.created` delivery from this
account. The parser is tolerant (fields top-level or under `data`, id as
integer or digit string) and the matching is configurable (id, or title).

## Decisions

### D1 — Route on event type first

`bmc.event_type` reads `type` (or `event`) before any parse. Only
`donation.created` reaches the existing donation parser and qualification,
unchanged. Only `extra_purchase.created` reaches the Bazibo path, and only
while Bazibo is enabled. Everything else is 200 `ignored`. Before this
change an unknown event without donation fields failed the donation parser
with 400, which makes BMC retry and counts toward the webhook being
disabled.

The reverse guard is structural: a donation can only ever reach the
donation path. Nothing reads a donation's note or message. If someone
donates "for Bazibo" instead of buying the Extra, the operator can mint a
code by hand (`netnl-admin bazibo mint`) and mail it.

### D2 — Matching the Extra

`NETNL_BAZIBO_EXTRA_ID` (BMC's numeric extra id, visible in the Extra's
URL, `buymeacoffee.com/<page>/e/<id>`) is the primary match. Without it,
`NETNL_BAZIBO_EXTRA_TITLE` matches the title exactly after whitespace
normalisation and case folding. The id is preferred because a title can be
renamed in the dashboard. A cart that contains the Bazibo Extra next to
other items still yields exactly one code (the code is a family licence,
quantity does not multiply it).

### D3 — Separate table, deterministic code, nothing to revoke

`bazibo_issuance` is its own table, so a transaction id can never move a
row between the two paths. The code id is drawn once per transaction; the
code is re-derived from the seed, the code id and the issued day (the
row's `created_at`). Ed25519 signing is deterministic, so a retry re-sends
the identical code. This removes the B1/B2 class of race from this path:
there is no second valid code to orphan, so nothing needs revoking on a
failed or lost attempt. The lease is kept only to avoid double mails.

Parked transactions (attempts exhausted) answer 200, not 503 as the netnl
path does: no further attempt will happen, so a 503 only burns BMC's budget
of 10 consecutive failures for the whole webhook. See O2 for the netnl
path.

`bazibo_issuance` rows are not pruned. They hold no personal data
(transaction id, code id, state, timestamps), they are what lets an operator
find the code id to revoke after a refund, and keeping them means a
replayed, validly signed delivery can never mint a second code.

### D4 — Activation

Order: origin, off switch, per-IP rate limit, bounded read, strict parse,
signature, then one `BEGIN IMMEDIATE` step (revoked? known device? count,
insert, auto-revoke). A device's acceptance is its rank among the code's
device rows by insertion order, so no extra column is stored and raising
the limit later admits the devices that were refused.

The device id is hashed with HMAC-SHA256 under `NETNL_BAZIBO_DEVICE_SALT`:
without the server secret, the stored hashes cannot be tied back to a
browser's id. Dates have no time of day.

Distinct attempts are counted across accepted and refused devices; the
row for a refused device is what makes "100 distinct" countable. Rows stop
growing once a code is revoked (the revoked check comes before the insert),
so the table is bounded by 100 rows per valid code, and only a valid,
signed code can create rows.

The rate limit uses the same client-IP derivation as the demo
(`CF-Connecting-IP` by default, `/32` or `/64`, missing header shares one
bucket). It is in process memory only and bounded to 4096 keys as an LRU
with O(1) work per request. Security review round 1: the header is set by
Cloudflare on the tunnel path, but the Tailscale Funnel ingress to the same
Service passes it through from the client, so there a client can choose its
key. The LRU makes that harmless for everyone else (a flood of fresh keys
evicts old keys rather than starving new real clients in a shared overflow
bucket, and costs ~1.5 µs per request); the spoofer only evades the limit
for itself, which buys it nothing the signature check does not already
stop: without a valid code nothing is stored, and with one the code's own
device limit applies. See O5.

An Extra purchase is matched on its `extras` before the strict parse
(review round 1): a purchase of another shop item is 200 whatever its
shape, and only a malformed Bazibo purchase is a 400, so an unexpected
payload for an unrelated item cannot push BMC towards disabling the
webhook.

The route exists whenever the signing key is set, so the Bazibo web build
gets a CORS-readable 503 `off` while the feature is dormant and can unlock
leniently.

### D5 — Logging and the access log

Nothing from the activation request is logged. The code id is logged when a
code first passes the device limit and when it is auto-revoked. uvicorn's
access log (which records the peer address and path) gets a filter for
`/bazibo/*` via `netnl.server.log_config`. In the production topology the
peer address is the cloudflared pod, not the visitor, but the filter makes
the promise independent of the topology.

### D6 — Dependencies

- **Ed25519: `cryptography`.** The standard library has no Ed25519. The
  options are `cryptography` (pyca, OpenSSL-backed, the de facto standard,
  wheels for every platform the image uses), PyNaCl (libsodium, also
  cffi-based, no smaller), or a pure-Python implementation (not constant
  time, and hand-rolled crypto). `cryptography` it is, in the `api` extra
  only; the CLI install is unaffected. Locked at 50.0.2 (with `cffi`
  2.1.1 and `pycparser` 3.12) in `uv.lock`.
- **QR: `segno==1.6.6`**, pinned exactly. Pure Python, no dependencies,
  writes PNG itself with `zlib`. The alternative `qrcode` needs Pillow (or
  pypng) for PNG, which is the heavy dependency this avoids; writing a QR
  encoder (Reed-Solomon, masking, version selection) here is not worth the
  risk for one image.

### D7 — Configuration

| Variable | Default | Meaning |
|---|---|---|
| `NETNL_BAZIBO_SIGNING_KEY` | unset | base64 32-byte Ed25519 seed; presence switch (secret) |
| `NETNL_BAZIBO_EXTRA_ID` | unset | BMC extra id; on switch |
| `NETNL_BAZIBO_EXTRA_TITLE` | unset | title match, used only without an id |
| `NETNL_BAZIBO_DEVICE_SALT` | unset | server secret for device hashes; required when on (secret) |
| `NETNL_BAZIBO_PUBLIC_KEY` | unset | optional cross-check of the seed at startup |
| `NETNL_BAZIBO_MIN_AMOUNT` | `0` | minimum payment amount for the Extra |
| `NETNL_BAZIBO_DEVICE_LIMIT` | `10` | devices per code |
| `NETNL_BAZIBO_AUTO_REVOKE_AT` | `100` | distinct devices that revoke a code |
| `NETNL_BAZIBO_ACTIVATE_PER_MINUTE` | `10` | per-IP activation requests per minute |
| `NETNL_BAZIBO_ALLOWED_ORIGINS` | `https://bazibo.nl,https://mwest2020.github.io` | CORS |
| `NETNL_BAZIBO_CLIENT_IP_HEADER` | `CF-Connecting-IP` | rate-limit key header |

Reused from the bridge: `NETNL_BMC_ACCEPT_TEST_MODE`,
`NETNL_SUPPORTER_CURRENCY`, `NETNL_SUPPORTER_MAX_PER_HOUR`,
`NETNL_SUPPORTER_MAX_ATTEMPTS`, `NETNL_SUPPORTER_NOTIFY` (gets the code id
and transaction id, never the code), the SMTP settings.

## Open owner questions

- **O1** The Extra's id, once Mark has created it. Until then the feature
  is dormant in production. Default: dormant.
- **O2** The netnl path answers 503 for a parked transaction, which counts
  toward BMC's 10-consecutive-failures auto-disable. Default: leave it; the
  Bazibo path answers 200 for parked.
- **O3** Revoke a code automatically on `extra_purchase.refunded`. Default:
  no; revoke by hand.
- **O5** Close the Tailscale Funnel ingress for `/bazibo/*` (or entirely)
  so `CF-Connecting-IP` can only come from Cloudflare. Default: leave it;
  see D4 for why spoofing it only lets a client evade its own limit.
- **O6** The deep link carries the code in the query (`?unlock=`), so it
  lands in the access logs of bazibo.nl and GitHub Pages and in browser
  history. A fragment (`#unlock=`) would stay in the browser. The format
  note fixes `?unlock=`, and the Bazibo client reads it. Default: keep
  `?unlock=`; if the Bazibo client also accepts `#unlock=`, the mail can
  switch with a one-line change in `bazibo.py`.
- **O4** Device rows never expire. A device not seen for a long time could
  free its slot. Default: keep them; 10 is generous.
