# Design: add-supporter-issuance

## Pinned decisions (D1–D5, architect)

- **D1.** A new, standalone OpenSpec change (`add-supporter-issuance`), not
  an amendment to `add-measurement-api` — except for the spec delta to
  "Authenticated surface", which is amended again (it already carries the
  `add-demo-run` amendment) to add the webhook family to its existing,
  unnumbered "separate opt-in surface" enumeration.
- **D2.** One route, `POST /webhooks/bmc`. No other method is registered on
  that path — `GET`, `PUT`, etc. fall through to the ordinary 501
  not-implemented catch-all, identical to any other unmapped path/method.
- **D3.** Persist-then-mail, with revoke-on-mail-failure:
  - Credential + idempotency row are written together in one
    `BEGIN IMMEDIATE` transaction, state `pending`.
  - Mail is sent *outside* that transaction (a network call must never hold
    the write lock other requests wait on).
  - Success: the row moves to `delivered`.
  - Failure: the just-minted credential is revoked, the row moves to
    `failed` with `attempts` incremented, and the facade answers 503 so BMC
    retries — retrying re-runs the whole flow, revoking the previous
    (never-delivered) credential and minting a fresh one under the same
    transaction id.
  - Invariant, unconditionally: at most one *active* credential exists per
    BMC transaction id at any time; a credential that could not be
    delivered never remains usable; a password is never written to disk in
    plaintext (it exists only as a return value, exactly like
    `netnl-admin user add`'s).
- **D4.** No supporter PII at rest. The donor's email and name (whatever BMC
  sends) live only in memory for the duration of building and sending the
  mail. The `supporter_issuance` table persists only: BMC transaction id,
  the generated username, delivery state, an attempt counter, and
  timestamps.
- **D5.** Opt-in via `NETNL_BMC_WEBHOOK_SECRET`. Setting it while the
  mail-path variables (SMTP host/from, `NETNL_PUBLIC_ENDPOINT`) are missing
  is a startup failure (`SettingsError`, naming the missing variable) —
  there is no state where the webhook secret is live but mail delivery is
  half-configured.

## Owner decisions carried into defaults

- Webhooks are already enabled on the BMC account.
- **Every** donation on the live account mints a key —
  `NETNL_SUPPORTER_MIN_AMOUNT` defaults to `0`.
- The donation link is live: <https://buymeacoffee.com/mark.westerweel>.

## Configuration (environment only, prefix `NETNL_`)

`Settings.supporter` is `None` unless `NETNL_BMC_WEBHOOK_SECRET` is set —
every other `NETNL_BMC_*`/`NETNL_SUPPORTER_*`/`NETNL_SMTP_*`/
`NETNL_PUBLIC_ENDPOINT` variable is ignored (not even read) in that case,
mirroring `DemoSettings`' own "opt-in, not read at all" shape.

| Variable | Default | Notes |
|---|---|---|
| `NETNL_BMC_WEBHOOK_SECRET` | — (required, master switch) | Shared secret configured on the BMC dashboard; never logged. |
| `NETNL_BMC_SIGNATURE_HEADER` | `X-Signature-Sha256` | Header carrying the HMAC. BMC's dashboard names the header it actually sends; set this to match if it differs. |
| `NETNL_BMC_MAX_BODY_BYTES` | `65536` | Size cap on the raw webhook body, enforced by reading at most this many bytes — see "Body size" below. |
| `NETNL_BMC_ACCEPT_TEST_MODE` | unset (`"1"` to accept) | Only for the integration test against a real BMC test delivery; leave unset in production. |
| `NETNL_SUPPORTER_MIN_AMOUNT` | `0` | Decimal string, parsed with `decimal.Decimal` — never `float`. |
| `NETNL_SUPPORTER_CURRENCY` | unset (any currency accepted) | Matched case-insensitively against the delivery's currency when set. |
| `NETNL_SUPPORTER_MAX_PER_HOUR` | `20` | Ceiling on new issuances started per rolling hour. |
| `NETNL_SUPPORTER_MAX_ATTEMPTS` | `3` | A transaction stuck failing this many times is parked (503 forever, no further mint) until an operator intervenes. |
| `NETNL_SUPPORTER_USERNAME_PREFIX` | `supporter-` | Generated usernames are `<prefix><8 hex chars>`. |
| `NETNL_PUBLIC_ENDPOINT` | — (required) | The facade's own public batch endpoint URL, interpolated into the credential mail. CR/LF rejected. |
| `NETNL_SMTP_HOST` | — (required) | |
| `NETNL_SMTP_PORT` | `587` | |
| `NETNL_SMTP_USERNAME` | unset | Optional: some relays authenticate by network/allowlist, not credentials. `login()` is only attempted when set. |
| `NETNL_SMTP_PASSWORD` | unset | Read only when `NETNL_SMTP_USERNAME` is set. |
| `NETNL_SMTP_FROM` | — (required) | CR/LF rejected (written into a mail header). |
| `NETNL_SMTP_MODE` | `starttls` | One of `starttls`, `ssl`, `plaintext`. `plaintext` additionally requires `NETNL_SMTP_ALLOW_PLAINTEXT=1`. |
| `NETNL_SMTP_TIMEOUT` | `15` | Seconds. |
| `NETNL_SUPPORTER_NOTIFY` | unset | Optional operator address for the post-delivery notification mail. |

## `bmc.py` — pure, no I/O

Imports nothing from `netnl.api`/`netnl.store`/`netnl.mail`: this module is
signature verification and payload parsing only, unit-testable with zero
fixtures beyond a payload dict and a secret.

- `verify_signature(secret, header_value, raw_body) -> bool`: HMAC-SHA256
  over the raw bytes, timing-safe (`hmac.compare_digest` on decoded bytes),
  never raises. Accepts the digest hex- or base64-encoded, with an optional
  `sha256=` prefix. Accepting either encoding is not a weakening: both are
  simply textual representations of the *same* computed digest for the
  *same* secret and body — an attacker who cannot compute a valid digest in
  one encoding cannot compute it in the other either, since both require
  knowing the secret. The tolerance exists because the exact header/encoding
  BMC's dashboard actually sends could not be confirmed against a live
  delivery at build time (see `docs/how-to/supporter-webhook.md`'s
  troubleshooting note).
- `parse_delivery(payload) -> Delivery`: tolerant of a field appearing
  top-level or nested under `data` (BMC's documented shape nests most
  fields there); raises `MalformedDelivery(field)` — carrying only the
  field name, never the payload — for anything missing or the wrong shape.
  Every string field is length-capped before use.
- `qualifies(delivery, cfg) -> Decision`: `ISSUE`, `IGNORE_EVENT`,
  `IGNORE_TEST_MODE`, `IGNORE_AMOUNT`, `IGNORE_CURRENCY`, or
  `UNDELIVERABLE_NO_EMAIL` (a present-but-invalid or absent recipient).
- `valid_recipient(email) -> bool`: single address, no CR/LF/space/comma/
  angle-bracket, exactly one `@`, at most 254 characters — a conservative
  guard used before the address is placed in *either* a mail header or the
  SMTP envelope (`RCPT TO`), since both are injection surfaces for an
  untrusted string.

Fixture payloads used in tests are literals commented "derived from
documented shape; replace with an owner-supplied real delivery" — BMC
webhook access to confirm the exact wire shape was not available at build
time.

## `mail.py` — a Sender seam, no provider strings in the mail body

- `Mail(to, subject, body)`, frozen.
- `Sender = Callable[[Mail], None]`.
- `build_credential_mail(to, username, password, public_endpoint) -> Mail`:
  interpolates **only** `username`/`password`/`public_endpoint` into a
  static template — no other field of the webhook delivery (name, email,
  note, ...) is ever placed in the mail body or subject. This removes the
  injection surface rather than escaping it: there is nothing
  attacker-influenced left to escape.
- `smtp_sender(cfg) -> Sender`: opens the connection per `cfg.smtp_mode`,
  `starttls()` before `login()` when in `starttls` mode, skips `login()`
  entirely when no username is configured, sends to exactly `[mail.to]`
  (no cc/bcc), and turns every `smtplib`/`ssl`/`OSError` into
  `DeliveryError` with a **static** message — the real exception's type
  name is logged, never its text (which can carry the SMTP host or a raw
  server response) and never reaches an HTTP reply.
- An optional second `Sender` call builds and sends the operator
  notification (`NETNL_SUPPORTER_NOTIFY`) after a successful delivery;
  its own `DeliveryError` is caught, logged, and never turns a `delivered`
  outcome into a `failed` one.

## The route: `POST /webhooks/bmc`

Ordering, each step short-circuiting the next:

1. **Size cap on bytes actually read**, not `Content-Length` — the
   existing `enforce_body_size` middleware trusts the declared
   `Content-Length` header, which a chunked-encoded request can omit or
   understate; this route reads its own body with an explicit byte cap
   regardless of what any header claims.
2. **HMAC verification** over the raw bytes. Failure: 401, no database
   connection opened, no scrypt-equivalent cost paid, no audit row.
3. **Parse.** Failure: 400, naming only the field.
4. **Qualification.** Any `IGNORE_*` decision: 200
   `{"status": "ignored"}`, no state written, no mail, no audit row —
   indistinguishable on the wire from any other filtered event.
5. **Everything blocking — the transaction, the mint, the mail send, the
   revoke-on-failure — runs in one `run_in_threadpool` call, on one
   connection this call opens and closes itself.** Not
   `Depends(store.get_conn)`: that dependency is deliberately opened with
   `allow_cross_thread=True` specifically because FastAPI can resolve a
   sync dependency, the handler, and its cleanup on three different real
   worker threads for one *ordinary* request (see `store.get_conn`'s own
   docstring for the measured `ProgrammingError` this works around). This
   handler is different: it is `async def`, and *itself* chooses to run
   its own blocking work in exactly one `run_in_threadpool` call — the
   connection is opened and used entirely inside that one call, on
   whichever single thread the threadpool hands it, so the cross-thread
   need `get_conn` exists for does not apply here. Using `store.connect`
   directly (the ordinary, `check_same_thread=True` default) is the
   correct, narrower tool.
6. **`BEGIN IMMEDIATE`:**
   - Idempotency lookup by transaction id.
     - `delivered` → 200 `{"status": "ok"}` (a duplicate looks identical
       on the wire to a fresh issuance — this never reveals which).
     - `undeliverable` → 200 `{"status": "ignored"}`.
     - `attempts >= NETNL_SUPPORTER_MAX_ATTEMPTS` → 503
       `delivery-failed`, no further mint attempted.
     - Otherwise (no row yet, or a `failed` row under the attempt cap):
       continue.
   - `UNDELIVERABLE_NO_EMAIL` decision (or a present-but-invalid address,
     per `bmc.valid_recipient`): write the row as `undeliverable` (no
     credential ever minted), commit, 200 `{"status": "ignored"}`.
   - Hourly cap (`NETNL_SUPPORTER_MAX_PER_HOUR`) exceeded: 503
     `rate-limited`.
   - If retrying, revoke the previous username in place.
   - Mint a fresh username (`<prefix><8 hex>`, regenerated on a
     `sqlite3.IntegrityError` collision, up to 5 attempts) and issue a
     credential via `netnl.issue.issue_credential` (shared with
     `netnl-admin user add`).
   - Upsert the `supporter_issuance` row, state `pending`.
   - Commit.
7. **Mail, outside the transaction.** Success → update the row to
   `delivered`, audit `supporter-deliver`, and — if `NETNL_SUPPORTER_NOTIFY`
   is set — best-effort the operator notification.
8. **`DeliveryError`** → revoke the just-minted credential, update the row
   to `failed` with `attempts + 1`, audit `supporter-deliver-failed`
   (host-free), answer 503 `delivery-failed` (`Retry-After` not required —
   BMC's own retry schedule applies).

Every reply from this route also carries the same provenance/security
headers every other reply does; `X-Netnl-Notice`/`X-Netnl-Instance` are
harmless here (BMC ignores them).

## Storage

```sql
CREATE TABLE IF NOT EXISTS supporter_issuance (
    id INTEGER PRIMARY KEY,
    txn_id TEXT NOT NULL UNIQUE,
    username TEXT NOT NULL,
    state TEXT NOT NULL,       -- pending | delivered | undeliverable | failed
    attempts INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
```

`CREATE TABLE IF NOT EXISTS` in the shared `_SCHEMA` script — a no-op
migration against an existing database, exactly like every other table
here. Pruned unconditionally (not gated on `settings.supporter`) on the
existing `NETNL_AUDIT_RETENTION_DAYS` cutoff — no new retention variable —
counted as `issuance_deleted` in `retention.prune`'s return value and
folded into the existing `prune` audit record's total, and printed by
`netnl-admin prune` alongside the existing counters.

## Reused, not reimplemented

- `netnl.store.add_credential`/`revoke_credential` — issuance and
  revoke-on-failure use the exact same primitives `netnl-admin` does.
- `netnl.auth.new_password`/`new_salt`/`hash_password` — via the new shared
  `netnl.issue.issue_credential`, used by both `netnl-admin user add` and
  this bridge, so the two paths cannot silently diverge in how a credential
  is minted.
- `netnl.replies.error_body`/`LABEL_STATUS` — the new `delivery-failed`
  label is added here, following the existing pattern
  (`demo-unavailable`, `forbidden-origin`).
- `netnl.errors.NetnlHTTPError` — every rejection on this route raises the
  same exception type every other route does, so the existing exception
  handlers, header allowlist and provenance/security-header middleware
  apply unchanged.

## Privacy and audit (D4, stated as an invariant)

No supporter email, name, or any other BMC-supplied free-text field is ever
written to the database, the audit trail, or a log line. `supporter_issuance`
persists only the transaction id, the generated username, state, attempts,
and timestamps — the transaction id and username are sanitised
(printable-only, length-capped, mirroring `netnl.auth`'s own
`_sanitize_username`) before being written into `audit.detail`, since the
transaction id is itself BMC-supplied input.

## Testing constraints

- No real SMTP server, no real BMC account: `RecordingSender` (a `Sender`
  that appends to a list instead of connecting anywhere) and a
  signing helper (`hmac.new(secret, body, sha256).hexdigest()`) in
  `tests/netnl/conftest.py`.
- An end-to-end test proves a webhook-issued credential actually
  authenticates against `POST /requests` against a fake upstream — the
  same shape `test_netnl_requests.py` already uses for the tenant path.
- `tests/netnl/test_netnl_leak.py` gains coverage: the webhook secret, the
  SMTP password, the issued password, and the supporter's own address must
  never appear in a response body, response headers, `caplog`, or a raw
  dump of the database file, across every outcome (issued, duplicate, 401,
  400, 503).

## Exit criteria for this build

- `openspec validate add-supporter-issuance --strict` and
  `openspec validate --all --strict` both pass.
- `sh scripts/verify.sh` passes, substantially above the pre-change test
  count.
- A webhook-issued credential submits a real measurement against a fake
  upstream in a test, end to end.
- `git` working tree clean; this change is **not** merged by the builder.
