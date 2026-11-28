"""Outbound mail for the supporter webhook bridge.

The first thing in this facade that sends mail. Kept small and deliberately
paranoid about what ever reaches a mail header, an SMTP envelope, or an
error message: `build_credential_mail` interpolates only the generated
username/password (combined into a single `username:password` credential
string) and the public endpoint — nothing BMC sent — into the mail body and
its HTML alternative alike, which removes the injection surface entirely
rather than escaping it. The two URLs in the template are static
constants, not provider-supplied strings, so they do not widen that
surface. The HTML part additionally passes every interpolated value
through `html.escape` — belt and braces, not a substitute for the
invariant above.
"""

from __future__ import annotations

import re
import html as _html
import logging
import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid, parseaddr
from typing import Callable

from netnl.errors import NetnlError

_logger = logging.getLogger("netnl.supporter")

_SMTP_MODES = {"starttls", "ssl", "plaintext"}


class DeliveryError(NetnlError):
    """Mail could not be sent. The message is always this fixed string —
    never the underlying SMTP host, port, or server response, which must
    never reach an HTTP reply or persisted state (see `smtp_sender`
    below)."""


@dataclass(frozen=True)
class InlineImage:
    """An image carried inside the mail itself (`multipart/related`,
    referenced from the HTML part as `cid:<cid>`) — never a remote URL, so
    opening the mail makes no network request (no tracking pixel)."""

    cid: str
    data: bytes
    subtype: str = "png"


@dataclass(frozen=True)
class Mail:
    to: str
    subject: str
    body: str
    html: str | None = None
    inline_images: tuple[InlineImage, ...] = ()
    # Which sender identity carries this mail (see `build_sender`):
    # "netnl" (the bridge's SMTP config) or "bazibo" (the buyer's code mail).
    product: str = "netnl"


# `Sender` is the seam every test in this package uses instead of a real
# SMTP connection (see `RecordingSender` in `tests/netnl/conftest.py`).
Sender = Callable[[Mail], None]


_CREDENTIAL_SUBJECT = "Your netnl supporter key"

# Static — not provider-supplied — pointers into this repo's own docs and
# try-it page. Safe to interpolate unconditionally alongside username/password/
# public_endpoint: see the module docstring.
_DOCS_URL = "https://github.com/MWest2020/internetnl-cli/blob/main/docs/how-to/ci.md"
_DEMO_URL = "https://mwest2020.github.io/internetnl-cli-demo/"
_INSTALL_URL = "git+https://github.com/MWest2020/internetnl-cli"

_CREDENTIAL_BODY_TEMPLATE = """\
Thank you for supporting netnl.

Here is your credential for the batch measurement facade — a single
"username:password" string:

  INTERNETNL_CREDENTIAL={credential}

This credential does not expire, but it is issued on a best-effort,
no-SLA basis -- see the supporter-key documentation for what that means
and the fair-use rate limit that applies to every credential, donor or
not.

Keep it safe: it is shown to you exactly once, in this mail, and is
never stored anywhere in a recoverable form. If you lose it, ask the
operator to reissue your credential.

How to use it
-------------

GitHub Actions -- add the credential above as a repository secret
named INTERNETNL_CREDENTIAL, then:

  - uses: MWest2020/internetnl-cli@main
    with:
      hosts: your-domain.nl
      endpoint: {public_endpoint}
      credential: ${{{{ secrets.INTERNETNL_CREDENTIAL }}}}

Terminal / any other CI:

  uv tool install {install_url}
  export INTERNETNL_ENDPOINT={public_endpoint}
  export INTERNETNL_CREDENTIAL=<the credential above>

Full guide (CI gate, exit codes, allowlists): {docs_url}
Try it in your browser: {demo_url}
"""

# Mail-client HTML, not web HTML (openspec/changes/polish-supporter-mail,
# design D3): no external resource of any kind, no script/form/event
# handler, inline CSS plus one <style> block for the dark-mode overrides
# that cannot be expressed inline (mail clients ignore prefers-color-scheme
# on inline styles), table layout for Outlook, single column <= 600px. The
# only URLs anywhere in this template are the three static constants and
# `public_endpoint` — every one of them HTML-escaped by the caller before
# it reaches `.format()` here, same as the credential.
_CREDENTIAL_HTML_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark">
<meta name="supported-color-schemes" content="light dark">
<title>Your netnl supporter key</title>
<style>
  @media (prefers-color-scheme: dark) {{
    .netnl-bg {{ background-color: #1a1a1a !important; }}
    .netnl-card {{ background-color: #242424 !important; border-color: #3a3a3a !important; }}
    .netnl-text {{ color: #e8e8e8 !important; }}
    .netnl-muted {{ color: #a8a8a8 !important; }}
    .netnl-mono {{ background-color: #0f0f0f !important; color: #7fd88f !important; border-color: #3a3a3a !important; }}
    .netnl-link {{ color: #8ab4f8 !important; }}
  }}
</style>
</head>
<body class="netnl-bg" style="margin:0; padding:0; background-color:#f4f4f4; font-family:-apple-system, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background-color:#f4f4f4;">
  <tr>
    <td align="center" style="padding:24px 16px;">
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" class="netnl-card" style="max-width:600px; width:100%; background-color:#ffffff; border:1px solid #e0e0e0; border-radius:8px;">
        <tr>
          <td style="padding:32px;">
            <h1 class="netnl-text" style="margin:0 0 16px 0; font-size:20px; font-weight:600; color:#1a1a1a;">Thank you for supporting netnl</h1>
            <p class="netnl-text" style="margin:0 0 24px 0; font-size:15px; line-height:1.5; color:#1a1a1a;">
              Here is your credential for the batch measurement facade &mdash; a single
              "username:password" string.
            </p>

            <h2 class="netnl-text" style="margin:0 0 8px 0; font-size:15px; font-weight:600; color:#1a1a1a;">Your credential</h2>
            <div class="netnl-mono" style="margin:0 0 24px 0; padding:14px 16px; background-color:#f0f0f0; border:1px solid #d0d0d0; border-radius:6px; font-family:'SFMono-Regular', Consolas, 'Liberation Mono', Menlo, monospace; font-size:14px; color:#1a1a1a; white-space:pre; word-break:keep-all; overflow-x:auto;">INTERNETNL_CREDENTIAL={credential}</div>

            <p class="netnl-muted" style="margin:0 0 24px 0; font-size:13px; line-height:1.5; color:#555555;">
              This credential does not expire, but it is issued on a best-effort, no-SLA basis
              &mdash; see the supporter-key documentation for what that means and the fair-use
              rate limit that applies to every credential, donor or not.
            </p>

            <p class="netnl-muted" style="margin:0 0 24px 0; font-size:13px; line-height:1.5; color:#555555;">
              Keep it safe: it is shown to you exactly once, in this mail, and is never stored
              anywhere in a recoverable form. If you lose it, ask the operator to reissue your
              credential.
            </p>

            <h2 class="netnl-text" style="margin:0 0 8px 0; font-size:15px; font-weight:600; color:#1a1a1a;">How to use it</h2>

            <p class="netnl-text" style="margin:0 0 8px 0; font-size:14px; line-height:1.5; color:#1a1a1a;">GitHub Actions &mdash; add the credential above as a repository secret named <code>INTERNETNL_CREDENTIAL</code>, then:</p>
            <pre class="netnl-mono" style="margin:0 0 24px 0; padding:14px 16px; background-color:#f0f0f0; border:1px solid #d0d0d0; border-radius:6px; font-family:'SFMono-Regular', Consolas, 'Liberation Mono', Menlo, monospace; font-size:13px; color:#1a1a1a; white-space:pre; overflow-x:auto;">  - uses: MWest2020/internetnl-cli@main
    with:
      hosts: your-domain.nl
      endpoint: {public_endpoint}
      credential: ${{{{ secrets.INTERNETNL_CREDENTIAL }}}}</pre>

            <p class="netnl-text" style="margin:0 0 8px 0; font-size:14px; line-height:1.5; color:#1a1a1a;">Terminal / any other CI:</p>
            <pre class="netnl-mono" style="margin:0 0 24px 0; padding:14px 16px; background-color:#f0f0f0; border:1px solid #d0d0d0; border-radius:6px; font-family:'SFMono-Regular', Consolas, 'Liberation Mono', Menlo, monospace; font-size:13px; color:#1a1a1a; white-space:pre; overflow-x:auto;">uv tool install {install_url}
export INTERNETNL_ENDPOINT={public_endpoint}
export INTERNETNL_CREDENTIAL=&lt;the credential above&gt;</pre>

            <p class="netnl-text" style="margin:0 0 4px 0; font-size:14px; line-height:1.5; color:#1a1a1a;">Full guide (CI gate, exit codes, allowlists): <a class="netnl-link" href="{docs_url}" style="color:#0a5cad;">{docs_url}</a></p>
            <p class="netnl-text" style="margin:0; font-size:14px; line-height:1.5; color:#1a1a1a;">Try it in your browser: <a class="netnl-link" href="{demo_url}" style="color:#0a5cad;">{demo_url}</a></p>
          </td>
        </tr>
      </table>
    </td>
  </tr>
</table>
</body>
</html>
"""


def build_credential_mail(*, to: str, username: str, password: str, public_endpoint: str) -> Mail:
    """Interpolates **only** `username`/`password`/`public_endpoint` — no
    other field from the triggering webhook delivery (donor name, email,
    note, ...) is ever placed in this mail, in either the plaintext body or
    the HTML alternative. `username` and `password` are combined into a
    single `username:password` credential string (the same shape
    `INTERNETNL_CREDENTIAL`/the action's `credential` input expect) and
    that combined string is shown exactly once per part, in the "how to
    use it" line — the CLI/CI copy-paste blocks below it reference "the
    credential above" rather than interpolating the password a second
    time. There is nothing attacker-influenced left in either template to
    escape, but every interpolated value is HTML-escaped before it reaches
    the HTML part regardless — defence in depth, not the control.
    """
    credential = f"{username}:{password}"
    body = _CREDENTIAL_BODY_TEMPLATE.format(
        credential=credential,
        public_endpoint=public_endpoint,
        install_url=_INSTALL_URL,
        docs_url=_DOCS_URL,
        demo_url=_DEMO_URL,
    )
    html_body = _CREDENTIAL_HTML_TEMPLATE.format(
        credential=_html.escape(credential, quote=True),
        public_endpoint=_html.escape(public_endpoint, quote=True),
        install_url=_html.escape(_INSTALL_URL, quote=True),
        docs_url=_html.escape(_DOCS_URL, quote=True),
        demo_url=_html.escape(_DEMO_URL, quote=True),
    )
    return Mail(to=to, subject=_CREDENTIAL_SUBJECT, body=body, html=html_body)


def build_notify_mail(*, to: str, username: str, txn_id: str) -> Mail:
    """The optional operator notification (`NETNL_SUPPORTER_NOTIFY`) sent
    after a successful delivery. Deliberately carries no password and no
    supporter PII beyond what BMC itself already mailed the operator via
    its own notifications — just enough for the operator's own records.
    """
    body = f"supporter key issued: {username}, txn {txn_id}\n"
    return Mail(to=to, subject="netnl: supporter key issued", body=body)


# --- Bazibo unlock code mail (openspec/changes/add-bazibo-unlock) ----------

_BAZIBO_SUBJECT = "Je Bazibo-code / Your Bazibo code"
_BAZIBO_QR_CID = "bazibo-qr@bazibo.nl"

_BAZIBO_BODY_TEMPLATE = """\
(English below)

Hoi!

{thanks_nl}

Open deze link op het apparaat waarop gespeeld wordt:

  {link}

Werkt die niet? Gebruik dan deze:

  {link_pages}

Of vul de code zelf in bij Bazibo:

  {code}

De code werkt in alle browsers van je gezin (tot {device_limit} apparaten), wordt op het apparaat zelf bewaard en je kunt hem altijd opnieuw invoeren vanuit deze mail.

Veel speelplezier!
Mark, Bazibo

------------------------------------------------------------

Hi!

{thanks_en}

Open this link on the device your child plays on:

  {link}

If that does not work, use this one:

  {link_pages}

Or enter the code in Bazibo yourself:

  {code}

The code works in every browser of your family (up to {device_limit} devices), is kept on the device itself, and you can always enter it again from this mail.

Have fun playing!
Mark, Bazibo
"""

# Mail-client HTML: no external resource of any kind (the QR code is an
# inline `cid:` part), no script/form, inline CSS only, single column.
# The opening sentence of the code mail (text and HTML).
_BAZIBO_THANKS = (
    "Dank je wel voor het kopen van de volledige versie van Bazibo (web).",
    "Thank you for buying the full version of Bazibo (web).",
)

_BAZIBO_HTML_TEMPLATE = """\
<!DOCTYPE html>
<html lang="nl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Bazibo</title>
</head>
<body style="margin:0; padding:0; background-color:#fdf8f0; font-family:-apple-system, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; color:#1a1a1a;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
  <tr>
    <td align="center" style="padding:24px 16px;">
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="max-width:560px; width:100%; background-color:#ffffff; border:1px solid #eadfcf; border-radius:12px;">
        <tr>
          <td style="padding:28px;">
            <h1 style="margin:0 0 12px 0; font-size:20px;">Dank je wel!</h1>
            <p style="margin:0 0 16px 0; font-size:15px; line-height:1.5;">{thanks_nl} Open deze link op het apparaat waarop gespeeld wordt:</p>
            <p style="margin:0 0 16px 0;"><a href="{link}" style="display:inline-block; padding:12px 18px; background-color:#2f7d4f; color:#ffffff; text-decoration:none; border-radius:8px; font-size:15px;">Bazibo openen</a></p>
            <p style="margin:0 0 16px 0; font-size:13px; line-height:1.5;">Werkt die niet? <a href="{link_pages}" style="color:#2f7d4f;">Gebruik deze link</a>. Of scan de QR-code, of vul de code zelf in:</p>
            <p style="margin:0 0 16px 0; text-align:center;"><img src="cid:{qr_cid}" width="200" height="200" alt="QR-code met je Bazibo-link" style="display:inline-block; width:200px; height:200px;"></p>
            <div style="margin:0 0 16px 0; padding:12px 14px; background-color:#f4f1ea; border-radius:6px; font-family:'SFMono-Regular', Consolas, Menlo, monospace; font-size:13px; word-break:break-all;">{code}</div>
            <p style="margin:0 0 24px 0; font-size:13px; line-height:1.5; color:#555555;">De code werkt in alle browsers van je gezin (tot {device_limit} apparaten), wordt op het apparaat zelf bewaard en je kunt hem altijd opnieuw invoeren vanuit deze mail.</p>
            <hr style="border:none; border-top:1px solid #eadfcf; margin:0 0 24px 0;">
            <h1 lang="en" style="margin:0 0 12px 0; font-size:20px;">Thank you!</h1>
            <p lang="en" style="margin:0 0 16px 0; font-size:15px; line-height:1.5;">{thanks_en} Open <a href="{link}" style="color:#2f7d4f;">this link</a> on the device your child plays on (or <a href="{link_pages}" style="color:#2f7d4f;">this one</a> if the first does not work), scan the QR code above, or enter the code above in Bazibo yourself.</p>
            <p lang="en" style="margin:0 0 16px 0; font-size:13px; line-height:1.5; color:#555555;">The code works in every browser of your family (up to {device_limit} devices), is kept on the device itself, and you can always enter it again from this mail.</p>
            <p style="margin:0; font-size:14px;">Veel speelplezier! / Have fun playing!<br>Mark, Bazibo</p>
          </td>
        </tr>
      </table>
    </td>
  </tr>
</table>
</body>
</html>
"""


def qr_png(text: str) -> bytes:
    """A QR code for `text` as PNG bytes. `segno` writes PNG itself (zlib
    from the standard library), so this needs no Pillow."""
    import io

    import segno

    buffer = io.BytesIO()
    segno.make(text, error="m", micro=False).save(buffer, kind="png", scale=6, border=4)
    return buffer.getvalue()


def build_bazibo_mail(*, to: str, code: str, link: str, link_pages: str, device_limit: int) -> Mail:
    """Interpolates only the code, its two links (fixed prefixes plus the
    code, which is base64url/`.` only) and an operator-configured integer —
    nothing from the BMC delivery. Every value is still HTML-escaped in the
    HTML part.
    """
    thanks_nl, thanks_en = _BAZIBO_THANKS
    body = _BAZIBO_BODY_TEMPLATE.format(
        link=link, link_pages=link_pages, code=code, device_limit=device_limit,
        thanks_nl=thanks_nl, thanks_en=thanks_en,
    )
    html_body = _BAZIBO_HTML_TEMPLATE.format(
        link=_html.escape(link, quote=True),
        link_pages=_html.escape(link_pages, quote=True),
        code=_html.escape(code, quote=True),
        device_limit=int(device_limit),
        qr_cid=_html.escape(_BAZIBO_QR_CID, quote=True),
        thanks_nl=_html.escape(thanks_nl, quote=True),
        thanks_en=_html.escape(thanks_en, quote=True),
    )
    image = InlineImage(cid=_BAZIBO_QR_CID, data=qr_png(link))
    return Mail(
        to=to, subject=_BAZIBO_SUBJECT, body=body, html=html_body, inline_images=(image,),
        product="bazibo",
    )


def build_bazibo_notify_mail(*, to: str, code_id: str, txn_id: str, source: str = "bmc") -> Mail:
    """Operator notification (`NETNL_SUPPORTER_NOTIFY`): the code id,
    payment reference and where it came from (`bmc` or `stripe`) only —
    never the code, never the buyer's address."""
    body = f"bazibo code issued ({source}): code id {code_id}, txn {txn_id}\n"
    return Mail(to=to, subject="netnl: bazibo code issued", body=body)


_SAFE_REF = re.compile(r"[^A-Za-z0-9_.:-]")


def safe_ref(txn_id: str) -> str:
    """A transaction id reduced to plain token characters (`?` otherwise),
    at most 128: safe in a header, a log or an audit line."""
    return _SAFE_REF.sub("?", txn_id)[:128] or "?"


def build_bazibo_alert_mail(
    *, to: str, txn_id: str, state: str | None, attempts: int | None, code_id: str | None,
    seen_at: str,
) -> Mail:
    """`netnl-admin bazibo watch`: one alert per transaction whose code was
    not delivered in time. The transaction id (reduced to plain token
    characters, so nothing from BMC can reach a header), the issuance state
    and attempts, the code id — never the buyer's address, never the code."""
    ref = safe_ref(txn_id)
    where = (
        f"Stripe-dashboard, Checkout Session {ref}" if ref.startswith("cs_")
        else "BMC-verkoopmelding" if ref.startswith("pi_") or ref.isdigit()
        else "de melding van de betaaldienst"
    )
    # The command only when it names the exact transaction: a changed id
    # would make a new issuance row (and a second code).
    fix = (
        f"  kubectl -n netnl exec deploy/netnl -- netnl-admin bazibo mint --mail <adres> --ref {ref}\n\n"
        if ref == txn_id
        else "  (de transactie-id bevat bijzondere tekens: neem hem letterlijk uit de tabel\n"
             "  bazibo_watch voor netnl-admin bazibo mint --mail <adres> --ref <id>)\n\n"
    )
    what = {
        None: "geen uitgifte-rij: de code is nooit aangemaakt (limiet per uur, crash of databasefout)",
        "pending": "pending: de mail is niet afgerond (crash tijdens versturen)",
        "failed": "failed: de mail kon niet worden verstuurd",
        "undeliverable": "undeliverable: geen bruikbaar e-mailadres in de webhook",
    }.get(state, f"{state}")
    body = (
        f"Bazibo-code niet verstuurd voor transactie {ref}.\n\n"
        f"Status: {what}\n"
        f"Pogingen: {attempts if attempts is not None else 0}\n"
        f"Code-id: {code_id or '-'}\n"
        f"Eerst gezien: {seen_at}\n\n"
        f"Controleer de betaling ({where}) en stuur de code zo nodig met de hand\n"
        "(dezelfde transactie, dus nooit een tweede code):\n\n"
        f"{fix}"
        "Deze melding komt één keer per transactie.\n"
    )
    return Mail(to=to, subject=f"Bazibo-code niet verstuurd voor transactie {ref} — controleer", body=body)


# Only these two, in this order of preference. `smtplib.SMTP.login()` is
# deliberately not used: it tries CRAM-MD5, then PLAIN, then LOGIN, moving to
# the next one after every SMTPAuthenticationError. A server that offers
# CRAM-MD5 but cannot check it (Plesk, with hashed mailbox passwords) then
# logs a failed login on every mail before PLAIN succeeds, and a wrong
# password costs one failure per advertised mechanism — enough to trip a
# fail2ban-style IP block at the hosting provider.
_AUTH_PREFERENCE = ("PLAIN", "LOGIN")


class _NoUsableAuthMechanism(smtplib.SMTPException):
    """The server offers no AUTH, or none of `_AUTH_PREFERENCE`."""


def _authenticate_once(conn: smtplib.SMTP, username: str, password: str) -> None:
    """Sends exactly one AUTH command: PLAIN when the server advertises it,
    else LOGIN, else nothing at all (`_NoUsableAuthMechanism`). A failure
    is never followed by another mechanism or another attempt — the caller
    turns it into `DeliveryError`.

    Called after `starttls()` in starttls mode; `ehlo_or_helo_if_needed`
    then re-reads the post-TLS EHLO, which is the list a server that only
    offers AUTH inside TLS advertises."""
    conn.ehlo_or_helo_if_needed()
    if not conn.has_extn("auth"):
        raise _NoUsableAuthMechanism("server offers no AUTH")
    advertised = conn.esmtp_features["auth"].upper().split()
    mechanism = next((m for m in _AUTH_PREFERENCE if m in advertised), None)
    if mechanism is None:
        raise _NoUsableAuthMechanism("server offers neither AUTH PLAIN nor AUTH LOGIN")
    # `auth_plain`/`auth_login` read these two attributes, the same ones
    # `login()` would have set.
    conn.user, conn.password = username, password
    if mechanism == "PLAIN":
        # One round trip: `AUTH PLAIN <base64>`.
        conn.auth("PLAIN", conn.auth_plain, initial_response_ok=True)
    else:
        # No initial response: a server that ignores it and still asks
        # "Username:" would otherwise get the password as the username.
        conn.auth("LOGIN", conn.auth_login, initial_response_ok=False)


def smtp_sender(
    *,
    host: str,
    port: int,
    username: str | None,
    password: str | None,
    from_addr: str,
    mode: str,
    timeout: int,
    reply_to: str | None = None,
) -> Sender:
    """Builds a `Sender` for the given SMTP configuration.

    - `from_addr` is the `From:` header as given (it may carry a display
      name, `Bazibo <hallo@bazibo.nl>`); the envelope sender is its bare
      address. `reply_to`, when set, becomes the `Reply-To:` header. Both
      are operator configuration, validated in `netnl.settings`.

    - `mode="ssl"`: connects with implicit TLS (`SMTP_SSL`).
    - `mode="starttls"`: connects plain, then upgrades with `starttls()`
      *before* authenticating.
    - `mode="plaintext"`: neither — the caller (`netnl.settings`) already
      required an explicit `NETNL_SMTP_ALLOW_PLAINTEXT=1` opt-in for this.
    - Authentication is attempted only when `username` is set — some
      relays authenticate by network origin or client certificate, not a
      username/password pair (see `netnl.settings._load_supporter`). When
      it is, exactly one AUTH command is sent per mail: see
      `_authenticate_once` (never `smtplib.SMTP.login()`, which walks every
      advertised mechanism after a failure).
    - Sends to exactly `[mail.to]` — no cc/bcc, no additional envelope
      recipient ever added.
    - **Every** exception raised while connecting, authenticating, or
      sending becomes `DeliveryError` with a fixed, static message; only
      `type(exc).__name__` is logged — the exception's own text (which can
      carry the host, a raw server response, or other transport detail)
      never propagates further. Security review fix: a bare
      `except (smtplib.SMTPException, OSError, ssl.SSLError)` here used to
      let anything else through unwrapped — measured: `smtplib.SMTP.
      login()` raises a bare `UnicodeEncodeError` (not one of those three
      types) when the configured username/password contains a non-ASCII
      character it tries to `.encode("ascii")` before base64-encoding it,
      which reached `netnl.supporter._process` as an unhandled exception —
      the just-minted credential was never revoked and the `pending` row
      was never recorded as failed, both violating the "never an
      undeliverable-but-working key" invariant (design.md, D3). A `Sender`
      contract that permits *any* exception type on failure cannot be
      relied on by a caller matching one specific except clause; catching
      bare `Exception` here is the one place that guarantee can actually be
      made, so every caller of a `Sender` may assume it never raises
      anything but `DeliveryError`.
    """
    if mode not in _SMTP_MODES:
        raise ValueError(f"unknown SMTP mode: {mode!r}")
    envelope_from = parseaddr(from_addr)[1] or from_addr
    # The Message-ID carries the From address's domain (bazibo.nl for the
    # code mail, westerweel.work for the bridge's mails), not this pod's
    # hostname, which `make_msgid` would use by default.
    msgid_domain = envelope_from.rpartition("@")[2] or None

    def _send(mail: Mail) -> None:
        try:
            if mode == "ssl":
                connection = smtplib.SMTP_SSL(
                    host, port, timeout=timeout, context=ssl.create_default_context()
                )
            else:
                connection = smtplib.SMTP(host, port, timeout=timeout)
            with connection as conn:
                if mode == "starttls":
                    # starttls() before AUTH — never the other order.
                    conn.starttls(context=ssl.create_default_context())
                if username:
                    _authenticate_once(conn, username, password or "")
                message = EmailMessage()
                message["Subject"] = mail.subject
                message["From"] = from_addr
                message["To"] = mail.to
                # RFC 5322 requires Date; spam filters score a mail without
                # Date or Message-ID lower (issue #19). UTC, set per send.
                message["Date"] = formatdate(localtime=False)
                message["Message-ID"] = make_msgid(domain=msgid_domain)
                if reply_to:
                    message["Reply-To"] = reply_to
                message.set_content(mail.body)
                if mail.html is not None:
                    message.add_alternative(mail.html, subtype="html")
                    if mail.inline_images:
                        html_part = message.get_payload()[-1]
                        for image in mail.inline_images:
                            html_part.add_related(
                                image.data, maintype="image", subtype=image.subtype,
                                cid=f"<{image.cid}>", disposition="inline",
                            )
                conn.sendmail(envelope_from, [mail.to], message.as_string())
        except Exception as exc:
            _logger.warning("supporter mail delivery failed: %s", type(exc).__name__)
            # `from None`: the cause can quote the host, the username or a
            # server reply; a later traceback must not print it.
            raise DeliveryError("failed to deliver supporter credential mail") from None

    return _send


def build_sender(settings) -> Sender:
    """The production `Sender` for `settings` (which must have the bridge's
    mail settings, `settings.supporter`): routes each `Mail` on its
    `product` (openspec/changes/add-bazibo-mail-identity).

    - "netnl" (supporter key, operator notifications and alerts): the
      bridge's `NETNL_SMTP_*` config, unchanged.
    - "bazibo" (the buyer's code mail): Bazibo's own relay when one is
      configured, else the bridge's relay; From is
      `<from_name> <from_addr>` (the address falls back to that of
      `NETNL_SMTP_FROM`), plus `Reply-To` when set.
    """
    cfg = settings.supporter
    main = smtp_sender(
        host=cfg.smtp_host, port=cfg.smtp_port, username=cfg.smtp_username,
        password=cfg.smtp_password, from_addr=cfg.smtp_from, mode=cfg.smtp_mode,
        timeout=cfg.smtp_timeout,
    )
    bz = settings.bazibo
    if bz is None:
        return main
    identity = bz.mail
    address = identity.from_addr or parseaddr(cfg.smtp_from)[1] or cfg.smtp_from
    from_header = formataddr((identity.from_name, address))
    relay = identity.relay
    if relay is None:
        bazibo = smtp_sender(
            host=cfg.smtp_host, port=cfg.smtp_port, username=cfg.smtp_username,
            password=cfg.smtp_password, from_addr=from_header, mode=cfg.smtp_mode,
            timeout=cfg.smtp_timeout, reply_to=identity.reply_to,
        )
    else:
        bazibo = smtp_sender(
            host=relay.host, port=relay.port, username=relay.username,
            password=relay.password, from_addr=from_header, mode=relay.mode,
            timeout=cfg.smtp_timeout, reply_to=identity.reply_to,
        )

    def _route(mail: Mail) -> None:
        (bazibo if mail.product == "bazibo" else main)(mail)

    return _route
