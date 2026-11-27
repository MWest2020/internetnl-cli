# Spec Delta: bazibo-unlock (add-bazibo-unlock)

## ADDED Requirements

### Requirement: Bazibo issuance is opt-in and refuses a half configuration

The facade SHALL treat `NETNL_BAZIBO_SIGNING_KEY` (base64 of a raw 32-byte
Ed25519 seed) as the presence switch of this capability and
`NETNL_BAZIBO_EXTRA_ID` or `NETNL_BAZIBO_EXTRA_TITLE` as its on switch. With
neither the key nor an Extra set, nothing in this capability exists. With
the key but no Extra, the capability is dormant: Extra purchases are ignored
like any other unhandled event and `POST /bazibo/activate` answers 503
`{"error": "off"}`. The facade SHALL refuse to start, naming the variable,
when an Extra is configured without the signing key, without the BMC bridge
(`NETNL_BMC_WEBHOOK_SECRET` and its mail settings), or without
`NETNL_BAZIBO_DEVICE_SALT`; when the key is not a 32-byte seed; when it does
not match `NETNL_BAZIBO_PUBLIC_KEY` if that is set; or when
`NETNL_BAZIBO_AUTO_REVOKE_AT` is not greater than `NETNL_BAZIBO_DEVICE_LIMIT`.
No startup error SHALL contain the key.

#### Scenario: Off without a key

- WHEN `NETNL_BAZIBO_SIGNING_KEY` is unset
- THEN `POST /bazibo/activate` answers 501 like any unmapped path and an
  `extra_purchase.created` delivery is ignored

#### Scenario: Dormant with a key and no Extra

- WHEN the key is set and neither `NETNL_BAZIBO_EXTRA_ID` nor
  `NETNL_BAZIBO_EXTRA_TITLE` is
- THEN `POST /bazibo/activate` answers 503 `{"error": "off"}` with the CORS
  headers for an allowed origin, and no Extra purchase mints anything

#### Scenario: Half configuration refused

- WHEN `NETNL_BAZIBO_EXTRA_ID` is set and the signing key, the BMC bridge
  secret, or the device salt is missing
- THEN `netnl-serve` exits non-zero at startup naming the missing variable

### Requirement: Only the configured Extra mints a code, and it never mints a credential

A validly-signed Extra purchase SHALL mint at most one Bazibo unlock code
per BMC transaction id and SHALL NOT create, revoke or touch any netnl
credential or `supporter_issuance` row. An Extra purchase is an
`extra_purchase.created` delivery, or a `donation.created` ("support
created") delivery that marks itself as one: `support_type` equal to
`Extra` (case-insensitive) or a non-empty `extras` list. Only those two
structured fields count; a note, message or title elsewhere SHALL NOT. A
code SHALL be minted only when `data.extras` contains an entry with the
configured id (or, when no id is configured, the configured title,
compared case-insensitively with whitespace normalised); an Extra purchase
that does not identify the Bazibo Extra that way SHALL be answered 200
`{"status": "ignored"}` and write nothing, and an Extra purchase SHALL be
ignored the same way while Bazibo is dormant. It SHALL qualify on BMC live
mode (or `NETNL_BMC_ACCEPT_TEST_MODE=1`), on the payment amount being at or
above `NETNL_BAZIBO_MIN_AMOUNT` (default 0, independent of
`NETNL_SUPPORTER_MIN_AMOUNT`), on the optional `NETNL_SUPPORTER_CURRENCY`,
and on a usable recipient address. The idempotency key SHALL be the BMC
transaction id, so both webhook events of one purchase mail once.

#### Scenario: The Bazibo Extra mints a code

- WHEN a signed, live `extra_purchase.created` for the configured Extra id
  with a usable `supporter_email` arrives
- THEN exactly one code is mailed, one `bazibo_issuance` row is recorded,
  and the credential and `supporter_issuance` tables are unchanged

#### Scenario: A support-created delivery of the Bazibo Extra mints a code

- WHEN a signed `donation.created` carries `support_type: "Extra"` and an
  `extras` entry with the configured id
- THEN it is handled exactly like the `extra_purchase.created` above, and
  a second delivery for the same transaction sends nothing

#### Scenario: An unidentified Extra mints nothing

- WHEN a signed `donation.created` says `support_type: "Extra"` but its
  `extras` list is missing, empty, or names only other Extras
- THEN the facade answers 200 `{"status": "ignored"}` and mints neither a
  code nor a credential

#### Scenario: Mentioning Bazibo does not make a donation an Extra

- WHEN a plain donation (no `support_type: "Extra"`, no non-empty `extras`)
  mentions the Extra's title or id in its note or message
- THEN it follows the donation rules and no code is minted

### Requirement: A paid Stripe Payment Link checkout mints a code

With `NETNL_STRIPE_WEBHOOK_SECRET` and `NETNL_STRIPE_PAYMENT_LINK_ID` set,
`POST /webhooks/stripe` SHALL verify the `Stripe-Signature` header (HMAC-SHA256
over `"<t>." + body`, 300 s tolerance, constant-time) before anything else,
and SHALL mint and mail one code for a `checkout.session.completed` (or
`checkout.session.async_payment_succeeded`) whose session has
`payment_status: "paid"` and the configured `payment_link`, keyed on the
Checkout Session id. Every other event SHALL be answered 200 and ignored.
The buyer's address SHALL NOT be stored.

#### Scenario: A good signature mints one code

- WHEN a signed, paid session of the configured link arrives
- THEN one code is mailed to `customer_details.email`, and a replay sends
  nothing new

#### Scenario: A bad or stale signature

- WHEN the signature does not match, or its timestamp is more than 300 s
  away
- THEN the answer is 401 and nothing is written

#### Scenario: Another link, or unpaid

- WHEN the session belongs to another payment link, or is not paid
- THEN the answer is 200 `ignored` and nothing is written

#### Scenario: A mail failure

- WHEN the code mail fails
- THEN the answer is 503, and Stripe's retry mails the same code

### Requirement: An undelivered code raises one alert

With `NETNL_BAZIBO_ALERT_TO` set, `netnl-admin bazibo watch` SHALL mail that
address once per transaction that qualified for a Bazibo code and has no
delivered issuance `NETNL_BAZIBO_ALERT_AFTER_MINUTES` (default 15) after it
was first seen, including a transaction with no issuance row and any
failed, parked, pending or undeliverable issuance row. The alert SHALL NOT
carry the buyer's address or the code.

#### Scenario: A failed mail is alerted once

- WHEN a code mail failed and no retry delivered it within 15 minutes
- THEN one mail "Bazibo-code niet verstuurd voor transactie <txn> —
  controleer" goes out, and later runs send nothing more for it

#### Scenario: Nothing written but the watch row

- WHEN a qualifying donation hit the hourly cap and no issuance row exists
- THEN the watch still alerts for its transaction

### Requirement: The delivery shape is logged without personal data

Every `donation.created` and `extra_purchase.created` delivery that passed
signature verification SHALL be logged with its event type, its
`support_type` (only when it is a short plain token, otherwise `other`),
whether `extras` is present and how many entries it has, and the integer
extra ids (at most 10). The line SHALL NOT carry an email address, a name,
a note or message, a title, or an amount.

#### Scenario: Shape line

- WHEN a support-created delivery with two extras arrives
- THEN one log line names `support_type`, `extras=list(2)` and both ids,
  and nothing else from the payload

### Requirement: An operator can issue and mail a code by transaction id

`netnl-admin bazibo mint --mail ADDRESS --ref TRANSACTION_ID` SHALL run the
same persist-then-mail step as the webhook, keyed on `TRANSACTION_ID` in
`bazibo_issuance`, and SHALL work while Bazibo is dormant. It SHALL send
nothing for a transaction already `delivered` (by the webhook or an
earlier run), SHALL take over an `undeliverable` or parked row, SHALL
re-send the same code on a retry after a mail failure, and SHALL print
neither the code nor the address. Without `--mail`/`--ref` it prints one
code, as before.

#### Scenario: Hand issue, then the webhook

- WHEN an operator issues a code with `--ref pi_X` and later a webhook
  delivery for transaction `pi_X` arrives
- THEN exactly one mail has been sent

### Requirement: The API poller is off without a token

`netnl-admin bazibo poll` SHALL do nothing (exit 0, one line saying why)
unless Bazibo is enabled and `NETNL_BMC_API_TOKEN` is set. When on, it
SHALL list `GET /v1/extras` pages by number from `NETNL_BMC_API_BASE` only,
send the token only there, refuse redirects, mint for purchases of the
configured Extra within `NETNL_BMC_POLL_LOOKBACK_HOURS`, key each on
`bmc-extra:<purchase_id>`, and never log the token or an address.

#### Scenario: No token

- WHEN `NETNL_BMC_API_TOKEN` is unset
- THEN the command makes no request and exits 0

### Requirement: Codes follow the shared v1 format

A minted code SHALL be `"BZB1." + b64url(payload) + "." + b64url(sig)`
(base64url without padding) where `payload` is 16 bytes (version 1, product
1, issued day as a big-endian uint32 of days since 1970-01-01 UTC, then 10
random bytes forming the code id) and `sig` is the Ed25519 signature over
`b"bazibo-unlock-v1" || payload` under the configured seed.

#### Scenario: A minted code verifies against the public key

- WHEN a code is minted
- THEN it verifies under the matching public key with the domain prefix,
  and does not verify as a signature over the payload alone

### Requirement: Idempotent, persist-then-mail issuance of one code per purchase

Issuance SHALL record the transaction in one `BEGIN IMMEDIATE` transaction
before any mail is sent, drawing the code id once on the first attempt. Mail
SHALL be sent outside that transaction. On a mail failure the row SHALL be
marked `failed` and the facade SHALL answer 503 so BMC retries; a retry
SHALL re-send the same code (same code id and issued day). A delivery for a
transaction already `delivered` SHALL send nothing and answer 200. A
`pending` row younger than the pending lease SHALL refuse a concurrent
delivery with 503. A transaction that has used `NETNL_SUPPORTER_MAX_ATTEMPTS`
attempts SHALL be parked and answered 200 without mail. New transactions
SHALL be capped by `NETNL_SUPPORTER_MAX_PER_HOUR`.

#### Scenario: Replay sends nothing

- WHEN the same signed delivery arrives again after delivery
- THEN no mail is sent and the reply is 200

#### Scenario: Mail failure then retry

- WHEN the first mail attempt fails
- THEN the reply is 503 and the row is `failed`; the next delivery for that
  transaction mails the identical code and marks the row `delivered`

### Requirement: No email, no code, and no device data at rest or in logs

`bazibo_issuance` SHALL hold only the transaction id, the code id (hex),
state, an attempt counter and timestamps. The buyer's email address SHALL
exist only in memory while the mail is sent. Neither the code nor the email
SHALL be logged or stored. Log lines MAY carry the transaction id and the
code id.

#### Scenario: Nothing sensitive after a full flow

- WHEN a purchase fails once, is retried and delivered, and is replayed
- THEN neither the email, the buyer's name, nor any part of the code appears
  in any log record or anywhere in the database

### Requirement: The code mail is bilingual, inline and untracked

The mail SHALL be Dutch and English in one message, plaintext with an HTML
alternative, and carry the link `https://bazibo.nl/#unlock=<code>`, the
alternative `https://mwest2020.github.io/bazibo/#unlock=<code>`, the code as
text, a QR code of the first link as an inline PNG (`multipart/related`,
referenced by `cid:`), and one line saying the code works in every browser
of the family up to the device limit, is kept on the device, and can be
entered again from the mail. The HTML SHALL reference no remote image,
stylesheet, font or script.

#### Scenario: Only inline images

- WHEN the HTML part is inspected
- THEN its only `<img src>` is the `cid:` of the inline QR PNG

### Requirement: Activation counts devices per code

`POST /bazibo/activate` with a JSON body of exactly `{"code", "device"}`
(device: 128 bits as 32 hex characters, or a dashed UUID) SHALL, in order:
refuse a disallowed `Origin` (403 `origin`); answer 503 `off` while dormant;
apply an in-memory per-IP limit of `NETNL_BAZIBO_ACTIVATE_PER_MINUTE` (10)
requests per minute (429 `rate`); refuse a body over 1 KiB or that is not
strict JSON of that exact shape (400 `invalid`); verify the code's signature
and refuse a bad one (400 `invalid`) before any database access; refuse a
revoked code id (403 `revoked`); and otherwise count the device. The first
`NETNL_BAZIBO_DEVICE_LIMIT` (10) distinct devices of a code SHALL succeed
(200 `{"status": "ok"}`) on every call; later distinct devices SHALL get
409 `limit`. When `NETNL_BAZIBO_AUTO_REVOKE_AT` (100) distinct devices have
tried one code, the code SHALL be revoked, and every device, counted or
not, SHALL get 403 `revoked` from then on. The code id SHALL be logged once
when a code first passes the device limit and when it is auto-revoked.

#### Scenario: Limit and idempotent re-activation

- WHEN ten devices have activated a code
- THEN an eleventh distinct device gets 409 `limit`, and any of the ten
  activating again gets 200

#### Scenario: Auto-revoke

- WHEN the hundredth distinct device tries a code
- THEN the code is revoked and even a previously counted device gets 403

#### Scenario: Signature first

- WHEN the code does not verify
- THEN the reply is 400 `invalid` and no database connection is opened

### Requirement: Activation keeps only what it counts

For activation the facade SHALL store only the code id, an HMAC-SHA256 of
the device id under the server secret `NETNL_BAZIBO_DEVICE_SALT`, and the
first-seen and last-seen dates (no time of day). It SHALL NOT store or log
the code, the device id, the client IP or the user agent. The per-IP rate
limit SHALL live in process memory only. uvicorn's access log SHALL omit
`/bazibo/*` requests.

#### Scenario: Nothing sensitive in logs or the database

- WHEN several devices activate, pass the limit and trigger auto-revoke
- THEN no log record and no database row contains the code, a device id or
  the client IP

### Requirement: Activation CORS is limited to the Bazibo origins

Replies on `/bazibo/*` SHALL carry `Cache-Control: no-store` and
`Vary: Origin`, and `Access-Control-Allow-Origin` echoing the request
`Origin` only when it is exactly one of `NETNL_BAZIBO_ALLOWED_ORIGINS`
(default `https://bazibo.nl,https://mwest2020.github.io`). The preflight
SHALL allow only `POST, OPTIONS` and the `content-type` header, and no reply
SHALL carry `Access-Control-Allow-Credentials`. Only `POST` and `OPTIONS`
SHALL be registered on the path.

#### Scenario: Disallowed origin

- WHEN a request comes from another origin
- THEN the preflight carries no CORS grant and the POST is refused with 403
  `origin`
