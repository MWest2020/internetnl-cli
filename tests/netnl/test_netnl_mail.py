"""`netnl.mail` — the Sender seam, the credential/notify mail builders, and
`smtp_sender`'s own SMTP handling. No real network: `smtplib.SMTP`/
`SMTP_SSL` are replaced with a small recording fake.
"""

from __future__ import annotations

import email
import re
import smtplib
import ssl

import pytest

from netnl import mail

_URL_RE = re.compile(r"https?://\S+?(?=[\"'<\s])")
_FORBIDDEN_RE = re.compile(r"<script|<img|<link|<form|@import|url\(|\son[a-z]+=", re.IGNORECASE)


class _FakeSMTP:
    """Records call order and arguments; never opens a real socket."""

    instances: list["_FakeSMTP"] = []

    def __init__(self, host, port, timeout=None):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.calls: list[tuple] = []
        self.sent = None
        type(self).instances.append(self)

    def starttls(self, context=None):
        self.calls.append(("starttls",))

    def login(self, username, password):
        self.calls.append(("login", username, password))

    def sendmail(self, from_addr, to_addrs, msg):
        self.calls.append(("sendmail", from_addr, tuple(to_addrs)))
        self.sent = msg

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeSMTPSSL(_FakeSMTP):
    def __init__(self, host, port, timeout=None, context=None):
        super().__init__(host, port, timeout=timeout)


@pytest.fixture(autouse=True)
def _reset_fake_instances():
    _FakeSMTP.instances = []
    yield
    _FakeSMTP.instances = []


@pytest.fixture
def fake_smtp(monkeypatch):
    monkeypatch.setattr(smtplib, "SMTP", _FakeSMTP)
    monkeypatch.setattr(smtplib, "SMTP_SSL", _FakeSMTPSSL)
    return _FakeSMTP


def _sender(fake_smtp, **overrides):
    kwargs = dict(
        host="smtp.example.org",
        port=587,
        username=None,
        password=None,
        from_addr="netnl@example.org",
        mode="starttls",
        timeout=5,
    )
    kwargs.update(overrides)
    return mail.smtp_sender(**kwargs)


# --- Mail / build_credential_mail ------------------------------------------


def test_build_credential_mail_interpolates_only_the_three_fields():
    m = mail.build_credential_mail(
        to="donor@example.org",
        username="supporter-aaaa1111",
        password="s3cr3t-pw",
        public_endpoint="https://facade.example.org",
    )
    assert m.to == "donor@example.org"
    assert "supporter-aaaa1111" in m.body
    assert "s3cr3t-pw" in m.body
    assert "https://facade.example.org" in m.body


def test_build_credential_mail_shows_a_single_credential_string_once():
    # Owner feedback: the password used to appear on its own line, next to
    # a separate username line. It must now appear exactly once, as one
    # "username:password" credential string — not twice (e.g. once in a
    # combined string and again in a copy-paste block).
    m = mail.build_credential_mail(
        to="donor@example.org",
        username="supporter-aaaa1111",
        password="s3cr3t-pw",
        public_endpoint="https://facade.example.org",
    )
    assert "supporter-aaaa1111:s3cr3t-pw" in m.body
    assert m.body.count("s3cr3t-pw") == 1
    assert m.body.count("supporter-aaaa1111") == 1
    assert "INTERNETNL_CREDENTIAL=supporter-aaaa1111:s3cr3t-pw" in m.body


def test_build_credential_mail_includes_action_and_cli_usage_and_docs_link():
    m = mail.build_credential_mail(
        to="donor@example.org",
        username="supporter-aaaa1111",
        password="s3cr3t-pw",
        public_endpoint="https://facade.example.org",
    )
    # GitHub Actions copy-paste block.
    assert "uses: MWest2020/internetnl-cli@main" in m.body
    assert "endpoint: https://facade.example.org" in m.body
    assert "credential: ${{ secrets.INTERNETNL_CREDENTIAL }}" in m.body
    assert "INTERNETNL_CREDENTIAL" in m.body  # named as the repo secret to add
    # Terminal/CLI copy-paste block — endpoint repeated, password is not.
    assert "uv tool install git+https://github.com/MWest2020/internetnl-cli" in m.body
    assert "export INTERNETNL_ENDPOINT=https://facade.example.org" in m.body
    # Docs/demo pointer.
    assert "https://github.com/MWest2020/internetnl-cli/blob/main/docs/how-to/ci.md" in m.body
    assert "https://mwest2020.github.io/internetnl-cli-demo/" in m.body


def test_build_credential_mail_never_echoes_a_donor_supplied_field():
    # There is no parameter for it at all — this is a structural guarantee,
    # asserted here for documentation: passing extra kwargs is a TypeError.
    with pytest.raises(TypeError):
        mail.build_credential_mail(  # type: ignore[call-arg]
            to="donor@example.org",
            username="u",
            password="p",
            public_endpoint="https://facade.example.org",
            supporter_name="Evil <script>",
        )


# --- Mail / build_credential_mail HTML alternative -------------------------


def test_credential_mail_html_contains_credential_and_password_exactly_once():
    m = mail.build_credential_mail(
        to="donor@example.org",
        username="supporter-aaaa1111",
        password="s3cr3t-pw",
        public_endpoint="https://facade.example.org",
    )
    assert m.html is not None
    assert m.html.count("supporter-aaaa1111:s3cr3t-pw") == 1
    assert m.html.count("s3cr3t-pw") == 1


def test_credential_mail_html_never_echoes_the_recipient_address():
    # Mirrors the plaintext "never echoes a donor-supplied field" guarantee
    # (structurally enforced there by the absence of a parameter): the
    # recipient address is used only for the envelope `to`, never
    # interpolated into either part.
    m = mail.build_credential_mail(
        to="donor-address-should-not-appear@example.org",
        username="supporter-aaaa1111",
        password="s3cr3t-pw",
        public_endpoint="https://facade.example.org",
    )
    assert "donor-address-should-not-appear@example.org" not in m.html
    assert "donor-address-should-not-appear@example.org" not in m.body


def test_credential_mail_html_has_no_network_triggering_markup():
    m = mail.build_credential_mail(
        to="donor@example.org",
        username="supporter-aaaa1111",
        password="s3cr3t-pw",
        public_endpoint="https://facade.example.org",
    )
    assert _FORBIDDEN_RE.search(m.html) is None


def test_credential_mail_html_urls_are_all_allowlisted():
    m = mail.build_credential_mail(
        to="donor@example.org",
        username="supporter-aaaa1111",
        password="s3cr3t-pw",
        public_endpoint="https://facade.example.org/hook?a=1",
    )
    allowed = {
        "https://github.com/MWest2020/internetnl-cli/blob/main/docs/how-to/ci.md",
        "https://mwest2020.github.io/internetnl-cli-demo/",
        "https://github.com/MWest2020/internetnl-cli",  # from git+https://...
        "https://facade.example.org/hook?a=1",
    }
    for url in _URL_RE.findall(m.html):
        assert url in allowed, url


def test_credential_mail_html_escapes_markup_characters_plaintext_stays_raw():
    m = mail.build_credential_mail(
        to="donor@example.org",
        username="supporter-aaaa1111",
        password="s3cr3t-pw",
        public_endpoint="https://x.example/?a=1&b=<2>",
    )
    assert "https://x.example/?a=1&amp;b=&lt;2&gt;" in m.html
    assert "https://x.example/?a=1&b=<2>" not in m.html
    assert "https://x.example/?a=1&b=<2>" in m.body


def test_build_notify_mail_has_no_password():
    m = mail.build_notify_mail(to="operator@example.org", username="supporter-aaaa1111", txn_id="txn-1")
    assert m.to == "operator@example.org"
    assert "supporter-aaaa1111" in m.body
    assert "txn-1" in m.body
    assert "password" not in m.body.lower()


# --- smtp_sender: MIME shape ------------------------------------------------


def test_mail_without_html_is_sent_as_single_part_text_plain(fake_smtp):
    send = _sender(fake_smtp)
    send(mail.Mail(to="donor@example.org", subject="s", body="b"))

    conn = fake_smtp.instances[0]
    sent = email.message_from_string(conn.sent)
    assert not sent.is_multipart()
    assert sent.get_content_type() == "text/plain"


def test_credential_mail_is_sent_as_multipart_alternative_plain_then_html(fake_smtp):
    send = _sender(fake_smtp)
    m = mail.build_credential_mail(
        to="donor@example.org",
        username="supporter-aaaa1111",
        password="s3cr3t-pw",
        public_endpoint="https://facade.example.org",
    )
    send(m)

    conn = fake_smtp.instances[0]
    sent = email.message_from_string(conn.sent)
    assert sent.is_multipart()
    parts = sent.get_payload()
    assert [p.get_content_type() for p in parts] == ["text/plain", "text/html"]
    plain_payload = parts[0].get_payload(decode=True).decode(parts[0].get_content_charset() or "utf-8")
    assert plain_payload == m.body


# --- smtp_sender: connection shape -----------------------------------------


def test_starttls_called_before_login(fake_smtp):
    send = _sender(fake_smtp, username="mailer", password="hunter2")
    send(mail.Mail(to="donor@example.org", subject="s", body="b"))

    conn = fake_smtp.instances[0]
    names = [c[0] for c in conn.calls]
    assert names.index("starttls") < names.index("login")


def test_login_skipped_without_username(fake_smtp):
    send = _sender(fake_smtp, username=None)
    send(mail.Mail(to="donor@example.org", subject="s", body="b"))

    conn = fake_smtp.instances[0]
    names = [c[0] for c in conn.calls]
    assert "login" not in names
    assert "starttls" in names  # starttls mode still upgrades the connection


def test_login_attempted_with_configured_username(fake_smtp):
    send = _sender(fake_smtp, username="mailer", password="hunter2")
    send(mail.Mail(to="donor@example.org", subject="s", body="b"))

    conn = fake_smtp.instances[0]
    login_calls = [c for c in conn.calls if c[0] == "login"]
    assert login_calls == [("login", "mailer", "hunter2")]


def test_ssl_mode_uses_smtp_ssl_and_skips_starttls(fake_smtp):
    send = _sender(fake_smtp, mode="ssl")
    send(mail.Mail(to="donor@example.org", subject="s", body="b"))

    conn = fake_smtp.instances[0]
    assert isinstance(conn, fake_smtp)
    names = [c[0] for c in conn.calls]
    assert "starttls" not in names


def test_to_addrs_is_exactly_the_one_recipient(fake_smtp):
    send = _sender(fake_smtp)
    send(mail.Mail(to="donor@example.org", subject="s", body="b"))

    conn = fake_smtp.instances[0]
    sendmail_call = next(c for c in conn.calls if c[0] == "sendmail")
    assert sendmail_call == ("sendmail", "netnl@example.org", ("donor@example.org",))


def test_from_addr_used_as_envelope_sender(fake_smtp):
    send = _sender(fake_smtp, from_addr="custom-from@example.org")
    send(mail.Mail(to="donor@example.org", subject="s", body="b"))

    conn = fake_smtp.instances[0]
    sendmail_call = next(c for c in conn.calls if c[0] == "sendmail")
    assert sendmail_call[1] == "custom-from@example.org"


# --- smtp_sender: failure handling ------------------------------------------


def test_connection_refused_becomes_delivery_error_with_static_message(monkeypatch):
    def _raise(*args, **kwargs):
        raise OSError("Connection refused to smtp.internal.example:25")

    monkeypatch.setattr(smtplib, "SMTP", _raise)
    send = mail.smtp_sender(
        host="smtp.internal.example",
        port=25,
        username=None,
        password=None,
        from_addr="netnl@example.org",
        mode="starttls",
        timeout=5,
    )
    with pytest.raises(mail.DeliveryError) as exc_info:
        send(mail.Mail(to="donor@example.org", subject="s", body="b"))

    assert "smtp.internal.example" not in str(exc_info.value)
    assert "25" not in str(exc_info.value)


def test_smtp_exception_becomes_delivery_error_with_static_message(monkeypatch):
    class _FailingSMTP(_FakeSMTP):
        def sendmail(self, from_addr, to_addrs, msg):
            raise smtplib.SMTPRecipientsRefused({"donor@example.org": (550, b"mailbox full")})

    monkeypatch.setattr(smtplib, "SMTP", _FailingSMTP)
    send = mail.smtp_sender(
        host="smtp.example.org",
        port=587,
        username=None,
        password=None,
        from_addr="netnl@example.org",
        mode="starttls",
        timeout=5,
    )
    with pytest.raises(mail.DeliveryError) as exc_info:
        send(mail.Mail(to="donor@example.org", subject="s", body="b"))
    assert "mailbox full" not in str(exc_info.value)
    assert "550" not in str(exc_info.value)


def test_ssl_error_becomes_delivery_error(fake_smtp, monkeypatch):
    def _raise_ssl(*args, **kwargs):
        raise ssl.SSLError("certificate verify failed for smtp.example.org")

    monkeypatch.setattr(smtplib, "SMTP", _raise_ssl)
    send = _sender(fake_smtp)
    with pytest.raises(mail.DeliveryError) as exc_info:
        send(mail.Mail(to="donor@example.org", subject="s", body="b"))
    assert "smtp.example.org" not in str(exc_info.value)


def test_plaintext_mode_skips_starttls(fake_smtp):
    send = _sender(fake_smtp, mode="plaintext")
    send(mail.Mail(to="donor@example.org", subject="s", body="b"))

    conn = fake_smtp.instances[0]
    names = [c[0] for c in conn.calls]
    assert "starttls" not in names


# --- security review round: root-fix, `except Exception`, not a narrow list


def test_unicode_encode_error_from_login_becomes_delivery_error(monkeypatch):
    """Security review finding: `smtplib.SMTP.login` raises a bare
    `UnicodeEncodeError` (not `SMTPException`/`OSError`/`ssl.SSLError`) when
    the configured username/password contains a non-ASCII character — this
    used to escape `smtp_sender` entirely as an unhandled exception. Proves
    the `except Exception` root-fix actually catches it, wraps it in
    `DeliveryError`, and never leaks the exception's own text.
    """

    class _NonAsciiLoginSMTP(_FakeSMTP):
        def login(self, username, password):
            raise UnicodeEncodeError(
                "ascii", "wächter", 0, 1, "ordinal not in range(128)"
            )

    monkeypatch.setattr(smtplib, "SMTP", _NonAsciiLoginSMTP)
    send = _sender(fake_smtp=_NonAsciiLoginSMTP, username="wächter", password="hunter2")
    with pytest.raises(mail.DeliveryError) as exc_info:
        send(mail.Mail(to="donor@example.org", subject="s", body="b"))
    assert "wächter" not in str(exc_info.value)
    assert "ordinal not in range" not in str(exc_info.value)


def test_value_error_from_email_formatting_becomes_delivery_error(monkeypatch):
    """The same root-fix also covers a `ValueError` from `EmailMessage`
    formatting (e.g. a header value `stdlib`'s own `email` package
    refuses) — any exception type, not just the three previously listed.
    """

    class _RaisingSMTP(_FakeSMTP):
        def sendmail(self, from_addr, to_addrs, msg):
            raise ValueError("header value contains newline")

    monkeypatch.setattr(smtplib, "SMTP", _RaisingSMTP)
    send = _sender(fake_smtp=_RaisingSMTP)
    with pytest.raises(mail.DeliveryError) as exc_info:
        send(mail.Mail(to="donor@example.org", subject="s", body="b"))
    assert "header value contains newline" not in str(exc_info.value)
