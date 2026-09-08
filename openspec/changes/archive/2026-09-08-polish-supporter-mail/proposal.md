# Change: polish-supporter-mail

## Why

Two owner decisions from 2026-09-04, both about the supporter bridge that
went live that day (`add-supporter-issuance`):

1. **The credential mail should look better.** It is the one thing a donor
   ever receives from this project, and today it is a plaintext message
   with a `INTERNETNL_CREDENTIAL=...` line and two copy-paste blocks. That
   is correct and readable, but it reads like a log line, not like a
   welcome. The owner asked for this at the time the plaintext version was
   merged ("comes later"); this is the "later".
2. **A donation should have a floor before it mints a key.** The bridge
   shipped with `NETNL_SUPPORTER_MIN_AMOUNT=0` — every live donation mints a
   key, explicitly parked as owner input O3 in that change's `tasks.md`.
   The owner has now decided: a key is issued for donations of **2.00 or
   more** (in the account's currency). A 0.10 donation must not mint a
   credential. Memberships or tiers for broader keys are a later
   conversation, not this change.

Both are small and touch the same module family (`netnl.mail`, the
supporter configuration, and the supporter-issuance spec), so they ship as
one change.

## What Changes

**HTML alternative for the credential mail.** `build_credential_mail`
produces, in addition to the plaintext body it produces today, an HTML
rendering of the same content, and `smtp_sender` sends the two as a
`multipart/alternative` message (plaintext first, HTML second). The
plaintext part remains the exact current content and is the source of
truth: a client that cannot or will not render HTML sees precisely what
donors see today. The notify mail to the operator stays plaintext-only.

The HTML part is a static template into which **only** the same three
values as today are interpolated — the combined `username:password`
credential string, the public endpoint, and the static doc/demo/install
constants already in `mail.py`. Nothing BMC sent ever reaches either part
(the module's existing invariant, restated as a requirement below). The
three values are HTML-escaped regardless — belt and braces, not a
substitute for the invariant.

The HTML follows mail-client reality, not web reality: no external
resources of any kind (no images, fonts, stylesheets, tracking pixels), no
scripts, inline CSS only, a single-column table layout of at most 600px,
system font stack, and the credential in a monospace block that survives
copy-paste in one piece. Dark-mode friendly via `color-scheme` meta and
colours that read on both backgrounds.

**Minimum donation defaults to 2.** `NETNL_SUPPORTER_MIN_AMOUNT` keeps its
semantics ("at or above", decimal string, `Decimal` never `float`) and
changes its default from `0` to `2`. The comparison is in whatever currency
BMC reports for the account; `NETNL_SUPPORTER_CURRENCY` is untouched and
stays optional. Docs, `.env.example`, and the supporter-issuance spec are
updated to match; O3 in `add-supporter-issuance/tasks.md` is thereby
resolved.

## Non-goals

- **No change to what is delivered.** Same credential shape, same single
  disclosure of the password, same "shown exactly once, never stored
  recoverably" promise, same docs links.
- **No new interpolation surface.** No donor name, no "thanks for the N
  coffees", no personalisation of any kind — that would reintroduce the
  injection surface `mail.py` was built to not have.
- **No templating dependency.** A Python string with `str.format` and
  `html.escape`, exactly like the plaintext template. No Jinja, no MJML,
  no build step.
- **No tiers, memberships, or per-amount differences.** Below the floor:
  ignored (200, no state, as today). At or above: exactly the key issued
  today. Nothing in between.
- **No retroactive effect.** Keys already issued for sub-2 donations (if
  any) are not revoked by this change; that is an operator decision made
  with `netnl-admin user revoke`.

## Impact

- `netnl.mail`: `Mail` gains an optional `html` field (default `None`, so
  every existing caller and `RecordingSender` test is unaffected);
  `smtp_sender` sends `multipart/alternative` when it is set and exactly
  today's `text/plain` message when it is not.
- `netnl.settings`: one default value changes (`0` → `2`). Operators who
  have set the variable explicitly see no change. The live deployment
  currently relies on the default and will move to the floor on its next
  rollout — which is the intent.
- `openspec/specs/supporter-issuance`: the qualification requirement's
  stated default changes; a new requirement pins the plaintext + HTML
  alternative shape and its constraints.
- Docs: `supporter-webhook.md` ("every donation qualifies by default")
  and `supporter-key.md` ("a small donation") gain the floor;
  `deploy/.env.example`; `CHANGELOG.md`.
