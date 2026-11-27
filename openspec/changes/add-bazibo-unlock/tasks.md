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

- [x] 9.1 `NETNL_BAZIBO_SIGNING_KEY` and `NETNL_BAZIBO_DEVICE_SALT` in the
      out-of-band Secret `netnl-bridge`
- [x] 9.2 `NETNL_BAZIBO_PUBLIC_KEY` and `NETNL_BAZIBO_MIN_AMOUNT` in the
      ConfigMap; `NETNL_BAZIBO_EXTRA_ID` left out (dormant)
- [x] 9.3 Image digest bump; pod healthy; `/webhooks/bmc` unsigned = 401;
      `/bazibo/activate` = 503 `off`

## T10. Round 2: support created, hand issue, poller (design D8-D11)

- [x] 10.1 `bmc.is_extra_marked`; routing of Extra-marked `donation.created`
- [x] 10.2 `bmc.shape_summary` log line
- [x] 10.3 `netnl-admin bazibo mint --mail --ref` via `_process(manual=True)`
- [x] 10.4 `netnl/bmc_poll.py`, `netnl-admin bazibo poll`, off without token
- [x] 10.5 Tests: `tests/netnl/test_netnl_bazibo_poll.py`
- [ ] 10.6 First real purchase: read the `event=bmc-shape` line, confirm
      the routing (owner action)

## T11. Round 3: codes from plain donations (design D12)

- [x] 11.1 `NETNL_BAZIBO_FROM_DONATIONS`, `NETNL_BAZIBO_DONATION_MIN` (settings, refusal without key, bridge or salt)
- [x] 11.2 `bazibo_issuance.handle_donation`; both paths run per donation, 503 if either failed
- [x] 11.3 Donation wording in the code mail
- [x] 11.4 Tests: `tests/netnl/test_netnl_bazibo_donations.py` (1 → code only, 2 → both, replay, mail failure and retry per path, off = before)
- [x] 11.5 Homelab ConfigMap `NETNL_BAZIBO_FROM_DONATIONS: "1"` and the image digest bump

## T12. The watch (design D13)

- [x] 12.1 `bazibo_watch` table, written before `_persist`
- [x] 12.2 `netnl-admin bazibo watch`, `NETNL_BAZIBO_ALERT_TO`, `NETNL_BAZIBO_ALERT_AFTER_MINUTES`
- [x] 12.3 Tests: `tests/netnl/test_netnl_bazibo_watch.py`
- [x] 12.4 Homelab CronJob `netnl-bazibo-watch` (every 5 min), `NETNL_BAZIBO_ALERT_TO` in the ConfigMap
