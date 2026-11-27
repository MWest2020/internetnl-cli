# Spec Delta: supporter-issuance (add-bazibo-unlock)

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

- WHEN a validly-signed delivery's event is not `donation.created`, and is
  not an `extra_purchase.created` delivery routed to the `bazibo-unlock`
  capability
- THEN the facade answers 200 `{"status": "ignored"}` and writes no row in
  any table, whatever the rest of the payload looks like (only a missing
  or non-string event type is a 400)

#### Scenario: A donation never mints a Bazibo code

- WHEN a validly-signed, live `donation.created` delivery's note or message
  mentions Bazibo
- THEN it is qualified exactly like any other donation (a netnl credential
  at or above `NETNL_SUPPORTER_MIN_AMOUNT`, otherwise ignored) and no Bazibo
  unlock code is minted or mailed

#### Scenario: An ignored event is logged without its content

- WHEN a validly-signed delivery is ignored for its event type
- THEN the log line names the event type only if it is one of the types in
  BMC's published webhook list (otherwise `other`), and carries no other
  payload field

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
