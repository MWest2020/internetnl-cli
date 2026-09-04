## MODIFIED Requirements

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

## ADDED Requirements

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
