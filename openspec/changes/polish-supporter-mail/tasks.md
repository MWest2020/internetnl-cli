# Tasks: polish-supporter-mail

## Owner inputs — resolved

- [x] O1 Minimum donation that mints a key: **2.00** in the account's
      currency, "at or above" (owner, 2026-09-04). Resolves
      `add-supporter-issuance` O3.
- [x] O2 Currency pinning: not now — `NETNL_SUPPORTER_CURRENCY` stays
      optional and unset (the BMC account has one currency; pinning it is
      an operator choice, not a default).
- [ ] O3 (after merge, operator) If the live deployment should enforce the
      floor *before* the next rollout, set `NETNL_SUPPORTER_MIN_AMOUNT=2`
      in the homelab config now; the code default makes that redundant
      afterwards.

## T1. OpenSpec change

- [x] 1.1 `proposal.md`, `design.md`, `tasks.md`
- [x] 1.2 Spec delta `specs/supporter-issuance/spec.md`: MODIFIED
      "Issuance requires a qualifying live donation" (default 2), ADDED
      "Credential mail is plaintext with an HTML alternative"
- Verify: `openspec validate polish-supporter-mail --strict` and
  `openspec validate --all --strict`

## T2. `netnl/mail.py` — HTML alternative

- [ ] 2.1 `Mail.html: str | None = None`
- [ ] 2.2 `_CREDENTIAL_HTML_TEMPLATE` per design D3/D4: static, single
      column ≤ 600px, table layout, inline CSS + one dark-mode `<style>`,
      `color-scheme` metas, system/monospace stacks, credential in exactly
      one non-wrapping monospace block, Actions + CLI `<pre>` blocks that
      reference "the credential above"
- [ ] 2.3 `build_credential_mail` fills `html`, escaping every interpolated
      value with `html.escape(..., quote=True)`; signature unchanged
- [ ] 2.4 `smtp_sender._send`: `add_alternative(mail.html, subtype="html")`
      when set; nothing else changes
- [ ] 2.5 `build_notify_mail` unchanged (`html is None`)
- [ ] 2.6 Module docstring: extend the "only three values reach the mail"
      paragraph to cover both MIME parts
- Verify: `uv run pytest tests/netnl/test_netnl_mail.py -q`

## T3. Tests for the HTML part

- [ ] 3.1 Single-part `text/plain` when `html is None` (unchanged
      behaviour, now asserted)
- [ ] 3.2 `multipart/alternative`, parts `[text/plain, text/html]`, plain
      part equals `mail.body`
- [ ] 3.3 Credential exactly once in HTML; password substring exactly once
- [ ] 3.4 No donor-supplied sentinel in HTML
- [ ] 3.5 No `<script`/`<img`/`<link`/`<form`/`@import`/`url(`/`on*=`
- [ ] 3.6 URL allowlist: every `http(s)://` in the HTML is one of the
      static constants or `public_endpoint`
- [ ] 3.7 Escaping test with `&`, `<`, `>` in `public_endpoint`
- [ ] 3.8 `test_netnl_leak.py`: supporter leak assertions cover the HTML
      part too (no password/secret/supporter address)
- Verify: `uv run pytest tests/netnl/test_netnl_mail.py tests/netnl/test_netnl_leak.py -q`

## T4. Minimum amount default

- [ ] 4.1 `netnl/settings.py`: `_resolve_decimal(env, "NETNL_SUPPORTER_MIN_AMOUNT", Decimal("2"))`
- [ ] 4.2 `test_netnl_supporter_settings.py`: default is `Decimal("2")`
- [ ] 4.3 New test: default reaches `bmc.qualifies` — `1.99` → `IGNORE_AMOUNT`,
      `2.00`/`"2"`/`2` → `ISSUE`, with a `QualifyConfig` built from real
      `Settings`
- [ ] 4.4 `test_netnl_supporter.py` fixtures: payload amounts ≥ 2 where a
      delivery must be issued (prefer this over setting the env var to 0)
- Verify: `uv run pytest tests/netnl/test_netnl_supporter_settings.py tests/netnl/test_netnl_supporter.py tests/netnl/test_netnl_bmc.py -q`

## T5. Docs

- [ ] 5.1 `deploy/.env.example`: `# NETNL_SUPPORTER_MIN_AMOUNT=2` and the
      comment ("donations below this are acknowledged and ignored")
- [ ] 5.2 `docs/how-to/supporter-webhook.md`: replace "Every donation on the
      account qualifies by default (`...=0`)" with the floor; mention the
      mail is sent as plaintext + HTML with zero remote resources
- [ ] 5.3 `docs/how-to/supporter-key.md`: "a small donation" → state the
      floor once (2.00 or more); keep everything else
- [ ] 5.4 `CHANGELOG.md`: one `Changed` entry (default floor) and one
      `Added` entry (HTML alternative, constraints in one sentence)
- Verify: English throughout; no new pages, so no `docs/index.md` change

## T6. Evidence (design D6)

- [ ] 6.1 Run output includes the full rendered HTML of one sample
      credential mail built with dummy values (not committed)
- [ ] 6.2 `sh scripts/verify.sh` output in the run output
- Verify: `sh scripts/verify.sh`
