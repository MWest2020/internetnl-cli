# Tasks: add-bazibo-unlock

## Owner inputs

- [ ] O1 Create the €1 Extra "Bazibo volledige versie (web)" in BMC and
      give its id. Until then production stays dormant (key deployed, no
      `NETNL_BAZIBO_EXTRA_ID`).
- [ ] O2-O4 see design.md, "Open owner questions" (each has a default).

## T1. OpenSpec change

- [x] 1.1 proposal.md, design.md, tasks.md
- [x] 1.2 Spec deltas: `bazibo-unlock` (ADDED), `supporter-issuance`
      (MODIFIED routing scenarios), `measurement-api` (MODIFIED
      "Authenticated surface")
- Verify: `openspec validate add-bazibo-unlock --strict`

## T2. Settings

- [x] 2.1 `BaziboSettings`, `Settings.bazibo`; key = presence, Extra = on
- [x] 2.2 Fail-fast on every half configuration, key never echoed
- [x] 2.3 Optional public-key cross-check
- Verify: `uv run pytest tests/netnl/test_netnl_bazibo.py -k "config or settings or dormant or off_when"`

## T3. Code format

- [x] 3.1 `netnl/bazibo.py`: mint, verify (canonical encoding, version,
      product, signature), links
- [x] 3.2 Test against the committed public key with the production seed
      (opt-in via `BAZIBO_TEST_SIGNING_KEY_FILE`), plus an OpenSSL
      cross-check done once by hand at build time

## T4. Routing and issuance

- [x] 4.1 `bmc.event_type`, `parse_extra_purchase`, `qualifies_bazibo`
- [x] 4.2 Routing in `supporter.handle_webhook`; unknown events 200
- [x] 4.3 `netnl/bazibo_issuance.py`: persist-then-mail, same code on retry,
      parked = 200
- [x] 4.4 `bazibo_issuance` table and store helpers

## T5. Mail

- [x] 5.1 `build_bazibo_mail` (NL + EN, links, code, inline QR, device line)
- [x] 5.2 `Mail.inline_images`, `multipart/related` in `smtp_sender`
- [x] 5.3 `segno` for the QR PNG

## T6. Activation

- [x] 6.1 `POST /bazibo/activate` + `OPTIONS`, CORS middleware
- [x] 6.2 `bazibo_device`, `bazibo_revoked`; limit, idempotency, auto-revoke
- [x] 6.3 In-memory per-IP rate limit, strict JSON, 1 KiB cap
- [x] 6.4 Access-log filter for `/bazibo/*`

## T7. Operator

- [x] 7.1 `netnl-admin bazibo mint|revoke|unrevoke|show`
- [x] 7.2 Runbook section in `docs/how-to/supporter-webhook.md`, including
      "Hoe we het bijhouden / How we keep track"; `.env.example`; CHANGELOG

## T8. Tests

- [x] 8.1 `tests/netnl/test_netnl_bazibo.py`: routing, signature, idempotency,
      mail failure and retry, logs and DB, half config at startup,
      activation (signature first, revoked, limit, re-activation,
      auto-revoke, CORS, rate limit, strict parsing, privacy)
- Verify: `sh scripts/verify.sh`

## T9. Deploy (homelab)

- [ ] 9.1 `NETNL_BAZIBO_SIGNING_KEY` and `NETNL_BAZIBO_DEVICE_SALT` in the
      out-of-band Secret `netnl-bridge`
- [ ] 9.2 `NETNL_BAZIBO_PUBLIC_KEY` and `NETNL_BAZIBO_MIN_AMOUNT` in the
      ConfigMap; `NETNL_BAZIBO_EXTRA_ID` left out (dormant)
- [ ] 9.3 Image digest bump; pod healthy; `/webhooks/bmc` unsigned = 401;
      `/bazibo/activate` = 503 `off`
