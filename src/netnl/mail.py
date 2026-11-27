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
# The opening sentence: a purchase of the Extra, or a plain donation (Mark,
# 27 Nov 2026). Filled into both templates.
_BAZIBO_THANKS = {
    False: (
        "Dank je wel voor het kopen van de volledige versie van Bazibo (web).",
        "Thank you for buying the full version of Bazibo (web).",
    ),
    True: (
        "Bedankt voor je donatie! Hier is je code voor de volledige versie van Bazibo in de browser.",
        "Thank you for your donation! Here is your code for the full version of Bazibo in the browser.",
    ),
}

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


def build_bazibo_mail(
    *, to: str, code: str, link: str, link_pages: str, device_limit: int, donation: bool = False,
) -> Mail:
    """Interpolates only the code, its two links (fixed prefixes plus the
    code, which is base64url/`.` only) and an operator-configured integer —
    nothing from the BMC delivery. Every value is still HTML-escaped in the
    HTML part.

    `donation`: the code came from a plain BMC donation, not an Extra
    purchase; the thank-you line says so (Dutch and English).
    """
    thanks_nl, thanks_en = _BAZIBO_THANKS[bool(donation)]
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
    return Mail(to=to, subject=_BAZIBO_SUBJECT, body=body, html=html_body, inline_images=(image,))


def build_bazibo_notify_mail(*, to: str, code_id: str, txn_id: str, source: str = "extra") -> Mail:
    """Operator notification (`NETNL_SUPPORTER_NOTIFY`): the code id,
    transaction id and where it came from (`extra` or `donation`) only —
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
        "Controleer de BMC-melding van deze donatie en stuur de code zo nodig met de hand\n"
        "(dezelfde transactie, dus nooit een tweede code):\n\n"
        f"{fix}"
        "Deze melding komt één keer per transactie.\n"
    )
    return Mail(to=to, subject=f"Bazibo-code niet verstuurd voor transactie {ref} — controleer", body=body)


def smtp_sender(
    *,
    host: str,
    port: int,
    username: str | None,
    password: str | None,
    from_addr: str,
    mode: str,
    timeout: int,
) -> Sender:
    """Builds a `Sender` for the given SMTP configuration.

    - `mode="ssl"`: connects with implicit TLS (`SMTP_SSL`).
    - `mode="starttls"`: connects plain, then upgrades with `starttls()`
      *before* any `login()` call.
    - `mode="plaintext"`: neither — the caller (`netnl.settings`) already
      required an explicit `NETNL_SMTP_ALLOW_PLAINTEXT=1` opt-in for this.
    - `login()` is attempted only when `username` is set — some relays
      authenticate by network origin or client certificate, not a
      username/password pair (see `netnl.settings._load_supporter`).
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
                    # starttls() before login() — never the other order.
                    conn.starttls(context=ssl.create_default_context())
                if username:
                    conn.login(username, password or "")
                message = EmailMessage()
                message["Subject"] = mail.subject
                message["From"] = from_addr
                message["To"] = mail.to
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
                conn.sendmail(from_addr, [mail.to], message.as_string())
        except Exception as exc:
            _logger.warning("supporter mail delivery failed: %s", type(exc).__name__)
            raise DeliveryError("failed to deliver supporter credential mail") from exc

    return _send
