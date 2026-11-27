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

A validly-signed `extra_purchase.created` delivery whose `data.extras`
contains an entry with the configured id (or, when no id is configured, the
configured title, compared case-insensitively with whitespace normalised)
SHALL mint at most one Bazibo unlock code per BMC transaction id and SHALL
NOT create, revoke or touch any netnl credential or `supporter_issuance`
row. It SHALL qualify on BMC live mode (or `NETNL_BMC_ACCEPT_TEST_MODE=1`),
on the payment amount being at or above `NETNL_BAZIBO_MIN_AMOUNT` (default
0, independent of `NETNL_SUPPORTER_MIN_AMOUNT`), on the optional
`NETNL_SUPPORTER_CURRENCY`, and on a usable recipient address. An
`extra_purchase.created` for any other Extra SHALL be answered 200
`{"status": "ignored"}` and write nothing.

#### Scenario: The Bazibo Extra mints a code

- WHEN a signed, live `extra_purchase.created` for the configured Extra id
  with a usable `supporter_email` arrives
- THEN exactly one code is mailed, one `bazibo_issuance` row is recorded,
  and the credential and `supporter_issuance` tables are unchanged

#### Scenario: Another Extra is ignored

- WHEN the cart holds only Extras with other ids
- THEN the facade answers 200 `{"status": "ignored"}` and writes nothing

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
alternative, and carry the link `https://bazibo.nl/?unlock=<code>`, the
alternative `https://mwest2020.github.io/bazibo/?unlock=<code>`, the code as
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
