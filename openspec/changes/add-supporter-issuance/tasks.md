# Tasks: add-supporter-issuance

## Owner inputs — not this build's call

- [ ] O1 The exact signature-header name and encoding BMC's dashboard
      actually sends for this account — `NETNL_BMC_SIGNATURE_HEADER`
      defaults to `X-Signature-Sha256` and `bmc.verify_signature` accepts
      hex or base64, but this was not confirmed against a live delivery at
      build time; see `docs/how-to/supporter-webhook.md`'s troubleshooting
      note.
- [ ] O2 Final `NETNL_SUPPORTER_MAX_PER_HOUR`/`NETNL_SUPPORTER_MAX_ATTEMPTS`
      values once real donation volume exists — the defaults here are a
      starting point.
- [x] O3 Whether `NETNL_SUPPORTER_MIN_AMOUNT=0` (every donation mints a key)
      remains the policy once volume is non-trivial. Resolved 2026-09-04:
      the default becomes `2` — see `openspec/changes/polish-supporter-mail`.
- [x] O4 SMTP provider/account to use in production. Decided 2026-09-05
      (owner Mark): **a transactional email provider on a dedicated
      subdomain** (e.g. `mail.westerweel.work`) with its own SPF/DKIM/
      DMARC, kept separate from personal mail so a bounce or spam
      complaint from netnl cannot touch that reputation — and independent
      of Hetzner's blocked port 25. The account itself, host, from-address
      and credentials stay out of band: they are deployment configuration
      (`NETNL_SMTP_*`), never committed here.

## T1. OpenSpec change

- [x] 1.1 `proposal.md` — why, what changes, non-goals, impact
- [x] 1.2 `design.md` — pinned decisions D1–D5, configuration table, `bmc.py`/
      `mail.py` shapes, route ordering, storage, privacy invariant, testing
      constraints, exit criteria
- [x] 1.3 `tasks.md` — this file
- [x] 1.4 Spec delta `specs/supporter-issuance/spec.md` (ADDED) and
      `specs/measurement-api/spec.md` (MODIFIED "Authenticated surface",
      current main text plus the webhook family)
- Verify: `openspec validate add-supporter-issuance --strict` and
  `openspec validate --all --strict`

## T2. Settings

- [x] 2.1 `SupporterSettings` dataclass in `netnl/settings.py`
- [x] 2.2 `Settings.supporter: SupporterSettings | None`, `None` unless
      `NETNL_BMC_WEBHOOK_SECRET` is set
- [x] 2.3 Fail-fast on every required-when-on variable, naming itself
- [x] 2.4 `NETNL_SUPPORTER_MIN_AMOUNT` parsed with `decimal.Decimal`, never
      `float`
- [x] 2.5 CR/LF guards on `NETNL_SMTP_FROM`/`NETNL_PUBLIC_ENDPOINT`
- [x] 2.6 `NETNL_SMTP_MODE=plaintext` requires `NETNL_SMTP_ALLOW_PLAINTEXT=1`
- [x] 2.7 Tests per variable (default, override, missing-required, invalid)
- [x] 2.8 `.env.example` block
- Verify: `uv run pytest tests/netnl/test_netnl_settings.py -q`

## T3. Storage

- [x] 3.1 `supporter_issuance` table in `_SCHEMA` (`CREATE IF NOT EXISTS` —
      no-op migration for an existing database)
- [x] 3.2 `store.find_issuance`/`insert_issuance`/`update_issuance`/
      `count_issuances_since`
- [x] 3.3 `netnl/issue.py::issue_credential`, shared by `netnl-admin user
      add` and the webhook bridge
- [x] 3.4 `retention.prune` removes `supporter_issuance` rows past the
      existing audit-retention cutoff, counted as `issuance_deleted`
- [x] 3.5 Tests, including migrate-idempotency and the shared
      `issue_credential` helper
- Verify: `uv run pytest tests/netnl/test_netnl_store.py
  tests/netnl/test_netnl_retention.py tests/netnl/test_netnl_admin.py -q`

## T4. `netnl/bmc.py` (pure, no I/O)

- [x] 4.1 `verify_signature`: HMAC-SHA256 over the raw body, timing-safe,
      hex or base64, optional `sha256=` prefix, never raises
- [x] 4.2 `parse_delivery`: tolerant of top-level vs. `data`-nested fields,
      length-capped, `MalformedDelivery(field)` on failure
- [x] 4.3 `qualifies`: `Decision.ISSUE`/`IGNORE_EVENT`/`IGNORE_TEST_MODE`/
      `IGNORE_AMOUNT`/`IGNORE_CURRENCY`/`UNDELIVERABLE_NO_EMAIL`
- [x] 4.4 `valid_recipient`: single address, no CR/LF/space/comma/bracket,
      one `@`, max 254 chars
- [x] 4.5 Fixture payloads as literals, commented "derived from documented
      shape; replace with an owner-supplied real delivery"
- [x] 4.6 Tests, including "signature over a re-serialised body is
      rejected" (proves verification is over the raw bytes, not a
      round-tripped parse)
- Verify: `uv run pytest tests/netnl/test_netnl_bmc.py -q`

## T5. `netnl/mail.py`

- [x] 5.1 `Mail` (frozen), `Sender` callable seam, `DeliveryError`
- [x] 5.2 `build_credential_mail`: interpolates only
      username/password/public_endpoint — no provider string
- [x] 5.3 `smtp_sender`: per-`NETNL_SMTP_MODE` connection, timeout,
      `starttls()` before `login()`, `login()` skipped without a configured
      username, `sendmail` to exactly `[mail.to]`
- [x] 5.4 Every `smtplib`/`ssl`/`OSError` becomes `DeliveryError` with a
      static message; only `type(exc).__name__` is logged
- [x] 5.5 `build_notify_mail` + best-effort send (`NETNL_SUPPORTER_NOTIFY`):
      no password, failure is logged and non-fatal
- [x] 5.6 Tests: starttls-before-login, login skipped without username,
      `to_addrs` exact, no host in `DeliveryError`, notify mail has no
      password, notify failure is non-fatal
- Verify: `uv run pytest tests/netnl/test_netnl_mail.py -q`

## T6. Route + orchestration

- [x] 6.1 `netnl/supporter.py`: the ordering from `design.md` — size cap,
      HMAC, parse, qualify, one `run_in_threadpool` call with its own
      `store.connect` connection, the `BEGIN IMMEDIATE` idempotency/mint/
      issue step, mail outside the transaction, revoke-on-failure
- [x] 6.2 `POST /webhooks/bmc` registered in `api.py` only when
      `settings.supporter` is not `None`
- [x] 6.3 `delivery-failed` added to `netnl.replies.LABEL_STATUS` (503)
- [x] 6.4 Logging via `logging.getLogger("netnl.supporter")`: event/
      txn_id/username only
- [x] 6.5 `tests/netnl/conftest.py`: `supporter_env`, `RecordingSender`,
      a signing helper
- Verify: `uv run pytest tests/netnl/test_netnl_supporter.py -q`

## T7. Cross-cutting tests

- [x] 7.1 End-to-end: a webhook-issued credential authenticates on
      `POST /requests` against a fake upstream
- [x] 7.2 Idempotency: replaying the same transaction id after `delivered`
      is a no-op 200, never a second mint
- [x] 7.3 Delivery-failure → revoked, retry mints a fresh key, hitting
      `NETNL_SUPPORTER_MAX_ATTEMPTS` parks it (503 forever)
- [x] 7.4 Every `IGNORE_*` filter: zero state rows, zero mail, zero audit
      rows
- [x] 7.5 No-email / invalid-email → `undeliverable`, no mail, no usable
      credential
- [x] 7.6 Hourly cap, exercised with the injected clock
- [x] 7.7 An unauthenticated (bad-signature) request writes nothing —
      table row counts identical before/after; an oversized body is
      rejected before the signature is ever checked
- [x] 7.8 `test_netnl_leak.py` extension: no secret/password/supporter
      address across success, duplicate, 401, 400, 503
- [x] 7.9 A header-injection-shaped address never reaches `Mail`
- [x] 7.10 Security headers present on every `/webhooks/bmc` reply
- Verify: `sh scripts/verify.sh`

## T8. Docs

- [x] 8.1 Rewrite `docs/how-to/supporter-key.md` for the automatic flow,
      manual issuance kept as the documented fallback
- [x] 8.2 New `docs/how-to/supporter-webhook.md`: secret generation
      (`openssl rand -hex 32`), out-of-band secret handling, BMC dashboard
      configuration (incl. noting the exact signature-header name and
      setting `NETNL_BMC_SIGNATURE_HEADER` if it differs), rollout and
      verifying 401-without-signature means "on", a test-donation
      procedure (`NETNL_BMC_ACCEPT_TEST_MODE=1`, then unset it and revoke
      the test key), a troubleshooting table, and security notes (the
      secret is the only gate, rotation, replay bounded by idempotency +
      retention, no supporter PII stored, the `NOTIFY` option)
- [x] 8.3 `docs/index.md` links the new page
- [x] 8.4 `docs/how-to/beta.md` cross-reference updated if it names the
      manual issuance process
- [x] 8.5 `CHANGELOG.md` entry
- Verify: docs build/lint (none configured beyond the language rule —
  English throughout)
