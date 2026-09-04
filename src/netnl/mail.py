"""Outbound mail for the supporter webhook bridge.

The first thing in this facade that sends mail. Kept small and deliberately
paranoid about what ever reaches a mail header, an SMTP envelope, or an
error message: `build_credential_mail` interpolates only the generated
username/password (combined into a single `username:password` credential
string) and the public endpoint — nothing BMC sent — into the mail body,
which removes the injection surface entirely rather than escaping it. The
two doc/demo URLs in the template are static constants, not
provider-supplied strings, so they do not widen that surface.
"""

from __future__ import annotations

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
class Mail:
    to: str
    subject: str
    body: str


# `Sender` is the seam every test in this package uses instead of a real
# SMTP connection (see `RecordingSender` in `tests/netnl/conftest.py`).
Sender = Callable[[Mail], None]


_CREDENTIAL_SUBJECT = "Your netnl supporter key"

# Static — not provider-supplied — pointers into this repo's own docs and
# demo. Safe to interpolate unconditionally alongside username/password/
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
Live demo: {demo_url}
"""


def build_credential_mail(*, to: str, username: str, password: str, public_endpoint: str) -> Mail:
    """Interpolates **only** `username`/`password`/`public_endpoint` — no
    other field from the triggering webhook delivery (donor name, email,
    note, ...) is ever placed in this mail. `username` and `password` are
    combined into a single `username:password` credential string (the same
    shape `INTERNETNL_CREDENTIAL`/the action's `credential` input expect)
    and that combined string is shown exactly once, in the "how to use it"
    line — the CLI/CI copy-paste blocks below it reference "the credential
    above" rather than interpolating the password a second time. There is
    nothing attacker-influenced left in the template to escape.
    """
    credential = f"{username}:{password}"
    body = _CREDENTIAL_BODY_TEMPLATE.format(
        credential=credential,
        public_endpoint=public_endpoint,
        install_url=_INSTALL_URL,
        docs_url=_DOCS_URL,
        demo_url=_DEMO_URL,
    )
    return Mail(to=to, subject=_CREDENTIAL_SUBJECT, body=body)


def build_notify_mail(*, to: str, username: str, txn_id: str) -> Mail:
    """The optional operator notification (`NETNL_SUPPORTER_NOTIFY`) sent
    after a successful delivery. Deliberately carries no password and no
    supporter PII beyond what BMC itself already mailed the operator via
    its own notifications — just enough for the operator's own records.
    """
    body = f"supporter key issued: {username}, txn {txn_id}\n"
    return Mail(to=to, subject="netnl: supporter key issued", body=body)


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
                conn.sendmail(from_addr, [mail.to], message.as_string())
        except Exception as exc:
            _logger.warning("supporter mail delivery failed: %s", type(exc).__name__)
            raise DeliveryError("failed to deliver supporter credential mail") from exc

    return _send
