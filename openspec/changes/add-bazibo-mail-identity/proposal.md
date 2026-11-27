# Change: add-bazibo-mail-identity

## Why

Owner request, 27 Nov 2026 (Mark): Bazibo unlock-code mails must come from
**hallo@bazibo.nl**, not mark@westerweel.work. Today every mail of the BMC
bridge leaves through one SMTP config (`NETNL_SMTP_*`, Soverin), so a buyer
of a children's game gets a mail from a personal address that has nothing
to do with Bazibo. The netnl supporter-key mail must keep that config.

The target relay (`mail.bazibo.nl`, Yourhosting Plesk) is not reachable
from the cluster yet (Yourhosting's firewall blocks the home IP since
26 Nov), and Soverin only sends as addresses on its own account. So the
change has to work in two steps without a code change between them.

## What Changes

1. **Per-product sender identity.** Each `Mail` carries a `product`
   (`netnl` or `bazibo`); `mail.build_sender(settings)` routes on it.
   Only the buyer's code mail is `bazibo`; the supporter-key mail, the
   operator notification and the watch alerts stay on `NETNL_SMTP_*`,
   byte for byte as before.
2. **Settings** (read only while `NETNL_BAZIBO_SIGNING_KEY` is set):
   `NETNL_BAZIBO_SMTP_HOST/PORT/USERNAME/PASSWORD/MODE/FROM`,
   `NETNL_BAZIBO_SMTP_FROM_NAME` (default `Bazibo`),
   `NETNL_BAZIBO_MAIL_REPLY_TO`.
   - No host: the bridge's relay carries the code mail, with
     From = `<FROM_NAME> <NETNL_BAZIBO_SMTP_FROM>` (address default: that
     of `NETNL_SMTP_FROM`) and the Reply-To when set. This is the interim
     production shape: `Bazibo <mark@westerweel.work>`,
     `Reply-To: hallo@bazibo.nl`.
   - A host: Bazibo's own relay, same timeout as the bridge (the pending
     lease derived from it still holds).
3. **Fail-fast on a half configuration:** a port, username, password or
   mode without a host; a host without `NETNL_BAZIBO_SMTP_FROM`; a
   username without a password or the reverse; a From or Reply-To that is
   not one plain address; a display name with CR/LF or over 64
   characters; a host with a port in it; mode `plaintext` (not offered
   for this relay). No error echoes a username or password; the password
   is `repr=False`.
4. **`Reply-To`** support in `mail.smtp_sender`; the envelope sender is
   the bare address of `From`.

## Design notes

- Routing in the sender, not at the call sites: the four places that
  built an `smtp_sender` (the app, `bazibo poll`, `bazibo watch`,
  `bazibo mint --mail`) now call `build_sender`, and
  `bazibo_issuance._process` keeps taking one `Sender`, so the test seam
  (`RecordingSender`) is unchanged.
- The switch to the own relay is one Secret patch (host, port, mode,
  username, password, From together) plus a restart, so there is never a
  window where startup refuses a half-written relay. The Reply-To stays in
  the ConfigMap.

## Non-goals

- DKIM signing in the app (the relay signs; Plesk does for bazibo.nl).
- A second relay for the netnl mails.

## Impact

- Code: `netnl/settings.py`, `netnl/mail.py`, `netnl/api.py`,
  `netnl/admin.py`. Tests: `tests/netnl/test_netnl_bazibo_mail_identity.py`.
- Docs: `docs/how-to/supporter-webhook.md` (settings table),
  `deploy/.env.example`, `CHANGELOG.md`.
- Deploy: homelab ConfigMap gets `NETNL_BAZIBO_MAIL_REPLY_TO`; the switch
  to `mail.bazibo.nl` is documented in the homelab runbook `bazibo-nl`.
