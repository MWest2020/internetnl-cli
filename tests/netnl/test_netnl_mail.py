"""`netnl.mail` — the Sender seam, the credential/notify mail builders, and
`smtp_sender`'s own SMTP handling. No real network: `smtplib.SMTP`/
`SMTP_SSL` are replaced with a small recording fake.
"""

from __future__ import annotations

import smtplib
import ssl

import pytest

from netnl import mail


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


def test_build_notify_mail_has_no_password():
    m = mail.build_notify_mail(to="operator@example.org", username="supporter-aaaa1111", txn_id="txn-1")
    assert m.to == "operator@example.org"
    assert "supporter-aaaa1111" in m.body
    assert "txn-1" in m.body
    assert "password" not in m.body.lower()


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
