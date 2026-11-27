# Change: add-bazibo-unlock

## Why

Bazibo (MWest2020/bazibo) is a free children's game whose web version gets
a paid "full version". Owner decision, 27 Nov 2026 (Mark): sell it as a €1
Buy Me a Coffee Extra, "Bazibo volledige versie (web)", and mail the buyer
an unlock code: "dat via een code doen en buy me a coffee? we hebben zo de
api key voor netnl. ook 1 euro" … "ja, bouw maar".

This facade already receives BMC's signed webhooks (`POST /webhooks/bmc`,
add-supporter-issuance) and already mails. Issuing the code here reuses the
verified HMAC gate, the idempotency model and the mail path instead of
building a second webhook receiver.

A second owner request the same day: "bazibo op een servertje … puur de key
check … geen moeite met 10x gedeeld of op 10 devices, maar 100x een streep
… en eerlijk zijn hoe we dat bijhouden". So the facade also gets a minimal
activation check that counts devices per code, and the docs say plainly
what is kept.

## What Changes

1. **Routing by event type, before any event-specific parse.**
   `donation.created` keeps today's netnl path untouched (same minimum,
   default 2). `extra_purchase.created` with the configured Extra id (or
   title) mints a Bazibo code and never a credential. Every other event,
   whatever its payload shape, is answered 200 and logged by type only.
   A donation that mentions Bazibo is still a donation.
2. **Code issuance.** Ed25519 codes in the fixed, shared v1 format
   (`BZB1.` + payload + signature, domain prefix `bazibo-unlock-v1`),
   persist-then-mail, one code per BMC transaction, a mail failure marks
   the row failed and a BMC retry re-sends the same code. Stored: the
   transaction id, the code id, state, attempts, timestamps. Not stored:
   the buyer's email, the code.
3. **The mail.** Dutch and English in one message: the deep link, the
   GitHub Pages alternative, the code as text, an inline QR PNG, one line
   on how the code behaves. No remote resources.
4. **`POST /bazibo/activate`.** Signature first, revocation list, distinct
   devices per code counted by a keyed hash of a random browser id: 10
   devices succeed, the 11th gets 409, 100 distinct devices auto-revoke the
   code. CORS for the two Bazibo origins only, per-IP rate limit in memory,
   strict JSON, nothing about the request logged.
5. **Operator commands.** `netnl-admin bazibo mint|revoke|unrevoke|show`.
6. **Two new runtime dependencies** in the `api` extra: `cryptography`
   (Ed25519; the standard library has none) and `segno` (pure-Python QR
   with its own PNG writer, no Pillow). See design.md, D6.

## Non-goals

- **No payment logic.** BMC stays the payment processor.
- **No refund automation.** `extra_purchase.refunded` is ignored; a code
  can be revoked by hand (`netnl-admin bazibo revoke`). See design.md, O3.
- **No account, login or recovery channel.** The mail is the receipt; a
  lost mail is a hand-minted code.
- **No change to the netnl donation rules or to the netnl credential
  tables.**

## Impact

- New tables `bazibo_issuance`, `bazibo_device`, `bazibo_revoked`
  (`CREATE IF NOT EXISTS`, no migration).
- New opt-in route `POST /bazibo/activate` (+ `OPTIONS`), present only when
  `NETNL_BAZIBO_SIGNING_KEY` is set.
- An unknown BMC event with a payload that is not donation-shaped now gets
  200 instead of a 400 from the donation parser.
- uvicorn's access log no longer records `/bazibo/*` requests.
