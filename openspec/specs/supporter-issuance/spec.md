# supporter-issuance Specification

## Purpose
TBD - created by archiving change add-supporter-issuance. Update Purpose after archive.
## Requirements
### Requirement: The bridge is opt-in and otherwise invisible

`POST /webhooks/bmc` SHALL exist as a route only when the operator has set
`NETNL_BMC_WEBHOOK_SECRET`. When unset, this path SHALL fall through to the
same 501 not-implemented catch-all as any other unmapped path, and no other
`NETNL_BMC_*`/`NETNL_SUPPORTER_*`/`NETNL_SMTP_*`/`NETNL_PUBLIC_ENDPOINT`
variable SHALL be read. No method other than `POST` SHALL be registered on
this path.

#### Scenario: Webhook path does not exist when disabled

- WHEN `NETNL_BMC_WEBHOOK_SECRET` is not set
- THEN any request to `/webhooks/bmc`, any method, answers 501, identically
  to any other unrecognised path

#### Scenario: Only POST is registered

- WHEN the bridge is enabled
- THEN `GET /webhooks/bmc` answers 501, not 405, identically to any other
  unmapped method on any other path

### Requirement: Signed deliveries only

The facade SHALL verify every request to `/webhooks/bmc` with an HMAC-SHA256
computed over the raw, unparsed request body using the configured secret,
compared in constant time. A request whose signature is missing, malformed,
or does not match SHALL be rejected with 401 and no further detail, and
SHALL NOT cause a database connection to be opened, a password-hashing
computation to be performed, a mail to be sent, or an audit row to be
written.

#### Scenario: Missing signature is rejected before any side effect

- WHEN a request to `/webhooks/bmc` carries no signature header at all
- THEN the facade answers 401 and neither opens a database connection nor
  writes an audit row

#### Scenario: Invalid signature is rejected before any side effect

- WHEN a request carries a signature header that does not match the
  HMAC-SHA256 of the raw body under the configured secret
- THEN the facade answers 401 with no detail beyond the generic error, and
  neither the credential table nor the audit table changes

#### Scenario: A re-serialised body is rejected

- WHEN a request's JSON body is parsed and re-serialised (whitespace or key
  order differs from the exact bytes the signature was computed over) before
  being resent with the original signature
- THEN the facade answers 401 — verification is over the exact raw bytes
  received, never a reparsed/reserialised form of them

### Requirement: Issuance requires a qualifying live donation

The facade SHALL mint a credential only for a `donation.created` delivery in
BMC's live mode whose amount is at or above the operator-configured minimum
(`NETNL_SUPPORTER_MIN_AMOUNT`, default: `2`, compared in the currency BMC
reports for the account) and, when an operator has configured an expected
currency, whose currency matches case-insensitively. A delivery that does
not qualify SHALL be acknowledged with 200 and an `{"status": "ignored"}`
body, and SHALL NOT cause any database row to be written, any mail to be
sent, or any audit row to be written. A qualifying delivery with no usable
recipient address SHALL instead record an `undeliverable` idempotency row
(never mint a credential) and answer 200 `{"status": "ignored"}`, unless the
hourly issuance cap (see "Issuance volume is bounded per hour" below) has
already been reached for a transaction id not previously seen, in which case
it SHALL answer 503 instead, identically to a qualifying, mintable delivery
under the same cap.

#### Scenario: A non-donation event is ignored

- WHEN a validly-signed delivery's event is not `donation.created`
- THEN the facade answers 200 `{"status": "ignored"}` and writes no row in
  any table

#### Scenario: A test-mode delivery is ignored by default

- WHEN a validly-signed `donation.created` delivery has `live_mode: false`
  and `NETNL_BMC_ACCEPT_TEST_MODE` is not set to `"1"`
- THEN the facade answers 200 `{"status": "ignored"}` and writes no row in
  any table

#### Scenario: An amount below the configured minimum is ignored

- WHEN a validly-signed, live, `donation.created` delivery's amount is below
  `NETNL_SUPPORTER_MIN_AMOUNT`
- THEN the facade answers 200 `{"status": "ignored"}` and writes no row in
  any table

#### Scenario: The default minimum is 2, at or above

- WHEN `NETNL_SUPPORTER_MIN_AMOUNT` is not set and a validly-signed, live,
  `donation.created` delivery with a usable recipient carries an amount of
  `1.99`
- THEN the facade answers 200 `{"status": "ignored"}` and writes no row in
  any table
- WHEN the same delivery carries an amount of `2.00` (or `2`, or `"2"`)
- THEN the facade mints and mails a credential exactly as for any other
  qualifying delivery

#### Scenario: A currency mismatch is ignored

- WHEN `NETNL_SUPPORTER_CURRENCY` is configured and a qualifying delivery's
  currency does not match it (case-insensitively)
- THEN the facade answers 200 `{"status": "ignored"}` and writes no row in
  any table

#### Scenario: A qualifying delivery with no usable recipient is undeliverable, not issued

- WHEN a validly-signed, live, qualifying `donation.created` delivery carries
  no email address, or one that is not a single plain address
- THEN the facade records an `undeliverable` row for that transaction, mints
  no credential, sends no mail, and answers 200 `{"status": "ignored"}`

#### Scenario: An undeliverable delivery under the hourly cap is deferred, not recorded

- WHEN the hourly issuance cap has been reached and a qualifying delivery
  with no usable recipient arrives for a transaction id not previously seen
- THEN the facade answers 503, mints no credential, sends no mail, and
  records no `undeliverable` row either — a flood of no-usable-email
  deliveries cannot fill the table past the cap any more than a flood of
  mintable ones can

### Requirement: Idempotent, persist-then-mail issuance with no orphaned working credential

The facade SHALL treat a BMC transaction id as the sole idempotency key for
issuance. A credential and an idempotency row SHALL be written together,
atomically, before any mail is sent. A credential that could not be
delivered by mail SHALL be revoked before the request's reply is sent, and
SHALL NOT remain a usable credential. Replaying an already-`delivered`
transaction SHALL NOT mint a second credential. A transaction whose most
recent attempt has not yet concluded (its credential was persisted but
mail delivery has not yet been confirmed to have succeeded or failed)
SHALL NOT be taken over by a concurrent or near-simultaneous request for
the same transaction id; such a request SHALL instead be answered 503,
without minting a second credential, until that conclusion is reached or a
bounded waiting period has passed.

#### Scenario: A fresh qualifying transaction is issued and mailed

- WHEN a qualifying delivery's transaction id has never been seen before
- THEN the facade mints a credential, records the transaction as `delivered`
  once mail succeeds, and the mailed credential authenticates successfully
  against the facade's own authenticated surface

#### Scenario: Replaying a delivered transaction is a safe no-op

- WHEN a request replays the exact transaction id of an already-`delivered`
  issuance
- THEN the facade answers 200, mints no second credential, and sends no
  further mail

#### Scenario: A mail failure revokes the credential and never leaves it usable

- WHEN a qualifying delivery is persisted but the subsequent mail attempt
  fails
- THEN the just-minted credential is revoked before the reply is sent, the
  transaction's state records the failure with an incremented attempt
  count, and the facade answers 503 so the sender may retry

#### Scenario: A concurrent request for an in-flight transaction is asked to retry, not raced

- WHEN a request for a transaction id arrives while a previous request for
  that same transaction id has persisted a credential but not yet
  concluded whether mail delivery succeeded or failed, and that previous
  attempt is still within its bounded waiting period
- THEN the facade answers 503 and does not mint a second credential, take
  over the in-flight attempt's credential, or revoke it

#### Scenario: A retry after a mail failure mints a fresh credential and revokes the failed one

- WHEN the same transaction id is retried after a recorded mail failure, and
  the attempt count is still below the configured maximum
- THEN the facade revokes the previous (undelivered) credential, mints a new
  one under the same transaction id, and attempts mail delivery again

#### Scenario: A transaction exhausting its retry budget is parked, not retried forever

- WHEN a transaction id's recorded attempt count has reached
  `NETNL_SUPPORTER_MAX_ATTEMPTS`
- THEN the facade answers 503 without minting another credential or sending
  another mail attempt

### Requirement: Untrusted delivery content never reaches a mail header, envelope, or reply

The facade SHALL validate a delivery's recipient address before it is placed
in a mail header or an SMTP envelope recipient. The facade SHALL NOT
interpolate any BMC-supplied field other than the generated username and
password into the credential mail, and SHALL NOT include any detail of a
mail-transport failure (host, port, or server response) in an HTTP reply or
in the delivery's persisted state.

#### Scenario: A header-injection-shaped address is never used to send mail

- WHEN a delivery's recipient address contains a carriage return, line feed,
  comma, space, or bracket
- THEN the facade treats the delivery as undeliverable and never constructs
  or sends a mail to that value

#### Scenario: A mail delivery failure never discloses transport details

- WHEN sending the credential mail fails for any reason
- THEN the facade's 503 reply and its persisted failure record both contain
  a fixed, static message with no SMTP host, port, or server response text

### Requirement: No supporter PII is retained

The facade SHALL NOT persist a supporter's email address, name, or any other
BMC-supplied free-text field to disk or to a log line. Persistent state for
an issuance SHALL be limited to the BMC transaction id, the generated
username, delivery state, an attempt counter, and timestamps.

#### Scenario: A successful issuance leaves no supporter PII on disk

- WHEN a qualifying delivery is issued and mailed successfully
- THEN a raw dump of the facade's database file contains the transaction id
  and the generated username but not the supporter's email address or name

### Requirement: Issuance is audited without ever recording a secret

The facade SHALL record every state transition on `/webhooks/bmc` that
writes a row (issuance, delivery, delivery failure, undeliverable) in the
audit trail, identified by the transaction id and the generated username,
and SHALL NOT record the webhook secret, the SMTP credential, or the issued
password in that trail.

#### Scenario: A successful issuance is audited without a password

- WHEN a credential is issued and mailed successfully
- THEN the audit trail records the event with the transaction id and
  username, and no audit row anywhere contains the issued password

### Requirement: Issuance volume is bounded per hour

The facade SHALL cap the number of new credential issuances started within
a rolling hour to `NETNL_SUPPORTER_MAX_PER_HOUR`, answering 503 for a
delivery that would exceed it rather than minting an unbounded number of
credentials from an unbounded number of qualifying deliveries.

#### Scenario: Exceeding the hourly cap is rejected without minting

- WHEN `NETNL_SUPPORTER_MAX_PER_HOUR` new issuances have already been
  started within the current rolling hour
- THEN a further qualifying delivery answers 503 and mints no credential

### Requirement: Credential mail is plaintext with an HTML alternative

The credential mail SHALL be sent as a `multipart/alternative` message whose
first part is the plaintext body and whose second part is an HTML rendering
of the same content. The plaintext part SHALL be complete and readable on
its own. Both parts SHALL interpolate only the generated
`username:password` credential string, the configured public endpoint, and
static documentation constants — no field of the triggering delivery. The
HTML part SHALL contain no script, form, image, stylesheet link, imported
style, web font, or any other reference that would cause a mail client to
make a network request; the only URLs it SHALL contain are the static
documentation constants and the public endpoint. The credential SHALL
appear exactly once in each part. Every interpolated value SHALL be
HTML-escaped in the HTML part. The operator notification mail SHALL remain
plaintext-only.

#### Scenario: A donor's client renders either part and sees the same credential once

- WHEN a qualifying delivery is issued and mailed
- THEN the message is `multipart/alternative` with a `text/plain` part
  followed by a `text/html` part, the plaintext part equals the body the
  facade produced before this change, and the credential string occurs
  exactly once in each part

#### Scenario: Rendering the HTML part causes no network request

- WHEN the HTML part of a credential mail is inspected
- THEN it contains no `<script>`, `<img>`, `<link>`, `<form>`, `@import`,
  `url(` or inline event handler, and every `http`/`https` URL in it is
  one of the static documentation constants or the public endpoint

#### Scenario: No delivery field reaches either part

- WHEN a qualifying delivery carries a supporter name, note, or any other
  field beyond the recipient address
- THEN neither the plaintext nor the HTML part of the credential mail
  contains any of those values

#### Scenario: A public endpoint with markup characters is escaped in HTML and raw in plaintext

- WHEN `NETNL_PUBLIC_ENDPOINT` contains `&`, `<` or `>`
- THEN the HTML part carries them as `&amp;`, `&lt;` and `&gt;` and the
  plaintext part carries them verbatim

#### Scenario: The operator notification is unchanged

- WHEN `NETNL_SUPPORTER_NOTIFY` is set and a delivery is issued
- THEN the notification mail is a single-part `text/plain` message with no
  HTML alternative and no password

