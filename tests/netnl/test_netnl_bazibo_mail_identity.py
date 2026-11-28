"""Bazibo's own mail identity (openspec/changes/add-bazibo-mail-identity):
`NETNL_BAZIBO_SMTP_*` / `NETNL_BAZIBO_MAIL_REPLY_TO`, the routing sender
(`mail.build_sender`), the fallback to the bridge's relay, fail-fast on a
half configuration, and nothing secret in logs or errors. No real network:
`smtplib.SMTP`/`SMTP_SSL` are replaced with a recording fake.
"""

from __future__ import annotations

import argparse
import base64
import email
import email.utils
from email.utils import parseaddr
import io
import logging
import smtplib
from datetime import datetime, timezone

import pytest

from netnl import admin, bazibo, mail
from netnl.errors import SettingsError
from netnl.settings import load

TEST_SEED_B64 = base64.b64encode(bytes(range(32))).decode()
BAZIBO_PASSWORD = "bazibo-relay-pw-Zq9!"
MAIN_PASSWORD = "main-relay-pw-Xy7!"


class _FakeSMTP:
    instances: list["_FakeSMTP"] = []

    def __init__(self, host, port, timeout=None):
        self.host, self.port, self.timeout = host, port, timeout
        self.calls: list[tuple] = []
        self.sent: str | None = None
        type(self).instances.append(self)

    def starttls(self, context=None):
        self.calls.append(("starttls",))

    # The slice of `smtplib.SMTP` that `mail._authenticate_once` uses. The
    # server advertises PLAIN and LOGIN; each `auth()` call is one AUTH
    # command, recorded with the credentials it carries.
    esmtp_features = {"auth": "PLAIN LOGIN"}
    user = password = None

    def ehlo_or_helo_if_needed(self):
        pass

    def has_extn(self, name):
        return name.lower() in self.esmtp_features

    def auth_plain(self, challenge=None):
        raise AssertionError("not called by the fake auth()")

    auth_login = auth_plain

    def auth(self, mechanism, authobject, *, initial_response_ok=True):
        self.calls.append(("auth", mechanism, self.user, self.password))

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


@pytest.fixture
def fake_smtp(monkeypatch):
    _FakeSMTP.instances = []
    monkeypatch.setattr(smtplib, "SMTP", _FakeSMTP)
    monkeypatch.setattr(smtplib, "SMTP_SSL", _FakeSMTPSSL)
    yield _FakeSMTP
    _FakeSMTP.instances = []


@pytest.fixture
def env(supporter_env) -> dict:
    """The bridge on its main relay (with login) plus Bazibo, dormant: the
    sender routing does not depend on Bazibo being switched on."""
    e = dict(supporter_env)
    e["NETNL_SMTP_USERNAME"] = "netnl@example.org"
    e["NETNL_SMTP_PASSWORD"] = MAIN_PASSWORD
    e["NETNL_BAZIBO_SIGNING_KEY"] = TEST_SEED_B64
    return e


@pytest.fixture
def own_relay_env(env) -> dict:
    e = dict(env)
    e.update({
        "NETNL_BAZIBO_SMTP_HOST": "mail.bazibo.example",
        "NETNL_BAZIBO_SMTP_PORT": "2587",
        "NETNL_BAZIBO_SMTP_USERNAME": "hallo@bazibo.example",
        "NETNL_BAZIBO_SMTP_PASSWORD": BAZIBO_PASSWORD,
        "NETNL_BAZIBO_SMTP_FROM": "hallo@bazibo.example",
        "NETNL_BAZIBO_MAIL_REPLY_TO": "hallo@bazibo.example",
    })
    return e


def _bazibo_mail(to="buyer@example.org") -> mail.Mail:
    code = "BZB1.test"
    return mail.build_bazibo_mail(
        to=to, code=code, link=f"https://bazibo.nl/#unlock={code}",
        link_pages=f"https://mwest2020.github.io/bazibo/#unlock={code}", device_limit=10,
    )


def _credential_mail() -> mail.Mail:
    return mail.build_credential_mail(
        to="donor@example.org", username="supporter-x", password="pw",
        public_endpoint="https://facade.example.org",
    )


def _headers(conn: _FakeSMTP) -> email.message.Message:
    return email.message_from_string(conn.sent)


# --- routing ------------------------------------------------------------------


def test_bazibo_mail_is_marked_bazibo_and_other_mails_netnl():
    assert _bazibo_mail().product == "bazibo"
    assert _credential_mail().product == "netnl"
    assert mail.build_bazibo_notify_mail(to="op@example.org", code_id="ab", txn_id="t").product == "netnl"


def test_bazibo_mail_uses_its_own_relay(own_relay_env, fake_smtp):
    send = mail.build_sender(load(own_relay_env))
    send(_bazibo_mail())

    conn = fake_smtp.instances[0]
    assert (conn.host, conn.port) == ("mail.bazibo.example", 2587)
    assert conn.calls[0] == ("starttls",)
    assert ("auth", "PLAIN", "hallo@bazibo.example", BAZIBO_PASSWORD) in conn.calls
    assert ("sendmail", "hallo@bazibo.example", ("buyer@example.org",)) in conn.calls
    msg = _headers(conn)
    assert msg["From"] == "Bazibo <hallo@bazibo.example>"
    assert msg["Reply-To"] == "hallo@bazibo.example"


def test_supporter_mail_is_unchanged_when_bazibo_has_its_own_relay(own_relay_env, fake_smtp):
    send = mail.build_sender(load(own_relay_env))
    send(_credential_mail())

    conn = fake_smtp.instances[0]
    assert (conn.host, conn.port) == ("smtp.example.org", 587)
    assert ("auth", "PLAIN", "netnl@example.org", MAIN_PASSWORD) in conn.calls
    assert ("sendmail", "netnl@example.org", ("donor@example.org",)) in conn.calls
    msg = _headers(conn)
    assert msg["From"] == "netnl@example.org"
    assert msg["Reply-To"] is None


def test_supporter_mail_without_any_bazibo_config_is_unchanged(supporter_env, fake_smtp):
    send = mail.build_sender(load(supporter_env))
    send(_credential_mail())
    msg = _headers(fake_smtp.instances[0])
    assert msg["From"] == "netnl@example.org" and msg["Reply-To"] is None


def test_operator_notify_mail_stays_on_the_bridge_identity(own_relay_env, fake_smtp):
    send = mail.build_sender(load(own_relay_env))
    send(mail.build_bazibo_notify_mail(to="op@example.org", code_id="ab", txn_id="t"))
    conn = fake_smtp.instances[0]
    assert conn.host == "smtp.example.org" and _headers(conn)["From"] == "netnl@example.org"


# --- fallback -------------------------------------------------------------------


def test_fallback_to_the_bridge_relay_with_the_bazibo_from_and_reply_to(env, fake_smtp):
    """The interim production shape: Soverin carries it, From is the
    account's own address under the Bazibo name, replies go to bazibo.nl."""
    env = dict(env)
    env["NETNL_BAZIBO_SMTP_FROM"] = "mark@example.net"
    env["NETNL_BAZIBO_MAIL_REPLY_TO"] = "hallo@bazibo.example"
    send = mail.build_sender(load(env))
    send(_bazibo_mail())

    conn = fake_smtp.instances[0]
    assert (conn.host, conn.port) == ("smtp.example.org", 587)
    assert ("auth", "PLAIN", "netnl@example.org", MAIN_PASSWORD) in conn.calls
    assert ("sendmail", "mark@example.net", ("buyer@example.org",)) in conn.calls
    msg = _headers(conn)
    assert msg["From"] == "Bazibo <mark@example.net>"
    assert msg["Reply-To"] == "hallo@bazibo.example"


def test_fallback_without_a_bazibo_from_uses_the_bridge_address_under_the_bazibo_name(env, fake_smtp):
    send = mail.build_sender(load(env))
    send(_bazibo_mail())
    msg = _headers(fake_smtp.instances[0])
    assert msg["From"] == "Bazibo <netnl@example.org>"
    assert msg["Reply-To"] is None


def test_display_name_is_configurable(own_relay_env, fake_smtp):
    own_relay_env["NETNL_BAZIBO_SMTP_FROM_NAME"] = "Bazibo spelletjes"
    mail.build_sender(load(own_relay_env))(_bazibo_mail())
    assert _headers(fake_smtp.instances[0])["From"] == "Bazibo spelletjes <hallo@bazibo.example>"


def test_ssl_mode_for_the_bazibo_relay(own_relay_env, fake_smtp):
    own_relay_env["NETNL_BAZIBO_SMTP_MODE"] = "ssl"
    own_relay_env["NETNL_BAZIBO_SMTP_PORT"] = "465"
    mail.build_sender(load(own_relay_env))(_bazibo_mail())
    conn = fake_smtp.instances[0]
    assert isinstance(conn, _FakeSMTPSSL) and ("starttls",) not in conn.calls


def test_hand_issue_without_injected_sender_goes_through_the_bazibo_relay(own_relay_env, fake_smtp, tmp_path):
    """`netnl-admin bazibo mint --mail --ref` builds its own sender: the code
    mail must leave through the Bazibo identity too."""
    own_relay_env["NETNL_BAZIBO_EXTRA_ID"] = "424242"
    own_relay_env["NETNL_BAZIBO_DEVICE_SALT"] = "test-device-salt-at-least-32-characters!"
    settings = load(own_relay_env)
    from netnl import store

    conn = store.connect(settings.db)
    store.migrate(conn)
    conn.close()
    out, err = io.StringIO(), io.StringIO()
    ns = argparse.Namespace(mail="buyer@example.org", ref="test-mailfrom-1")
    code = admin._bazibo_mint_mail(settings, ns, datetime(2026, 11, 27, tzinfo=timezone.utc), out, err)
    assert code == 0, err.getvalue()
    relay = fake_smtp.instances[0]
    assert relay.host == "mail.bazibo.example"
    assert _headers(relay)["From"] == "Bazibo <hallo@bazibo.example>"
    assert _headers(relay)["Reply-To"] == "hallo@bazibo.example"


# --- reply-to on the plain sender -------------------------------------------------


def test_smtp_sender_sets_reply_to_only_when_given(fake_smtp):
    common = dict(host="h.example", port=587, username=None, password=None,
                  from_addr="Bazibo <a@example.org>", mode="starttls", timeout=5)
    mail.smtp_sender(**common, reply_to="r@example.org")(_credential_mail())
    mail.smtp_sender(**common)(_credential_mail())
    first, second = fake_smtp.instances
    assert _headers(first)["Reply-To"] == "r@example.org"
    assert _headers(second)["Reply-To"] is None
    # The envelope sender is the bare address, not the display form.
    assert ("sendmail", "a@example.org", ("donor@example.org",)) in first.calls


# --- fail-fast --------------------------------------------------------------------


@pytest.mark.parametrize("var", [
    "NETNL_BAZIBO_SMTP_PORT", "NETNL_BAZIBO_SMTP_USERNAME",
    "NETNL_BAZIBO_SMTP_PASSWORD", "NETNL_BAZIBO_SMTP_MODE",
])
def test_relay_part_without_host_is_refused(env, var):
    env[var] = "465" if var.endswith("PORT") else "ssl" if var.endswith("MODE") else BAZIBO_PASSWORD
    with pytest.raises(SettingsError) as exc:
        load(env)
    assert "NETNL_BAZIBO_SMTP_HOST" in str(exc.value)
    assert BAZIBO_PASSWORD not in str(exc.value)


def test_host_without_from_is_refused(own_relay_env):
    del own_relay_env["NETNL_BAZIBO_SMTP_FROM"]
    with pytest.raises(SettingsError, match="NETNL_BAZIBO_SMTP_FROM"):
        load(own_relay_env)


def test_host_without_any_credentials_is_refused(own_relay_env):
    del own_relay_env["NETNL_BAZIBO_SMTP_USERNAME"]
    del own_relay_env["NETNL_BAZIBO_SMTP_PASSWORD"]
    with pytest.raises(SettingsError, match="NETNL_BAZIBO_SMTP_USERNAME, NETNL_BAZIBO_SMTP_PASSWORD"):
        load(own_relay_env)


def test_ssl_mode_defaults_to_port_465(own_relay_env):
    del own_relay_env["NETNL_BAZIBO_SMTP_PORT"]
    own_relay_env["NETNL_BAZIBO_SMTP_MODE"] = "ssl"
    assert load(own_relay_env).bazibo.mail.relay.port == 465


@pytest.mark.parametrize("drop", ["NETNL_BAZIBO_SMTP_USERNAME", "NETNL_BAZIBO_SMTP_PASSWORD"])
def test_username_without_password_and_vice_versa_is_refused(own_relay_env, drop):
    del own_relay_env[drop]
    with pytest.raises(SettingsError, match=drop) as exc:
        load(own_relay_env)
    assert BAZIBO_PASSWORD not in str(exc.value)
    assert "hallo@bazibo.example" not in str(exc.value)


@pytest.mark.parametrize("var,value", [
    ("NETNL_BAZIBO_SMTP_MODE", "plaintext"),
    ("NETNL_BAZIBO_SMTP_PORT", "0"),
    ("NETNL_BAZIBO_SMTP_HOST", "mail.bazibo.example:587"),
    ("NETNL_BAZIBO_SMTP_FROM", "Bazibo <hallo@bazibo.example>"),
    ("NETNL_BAZIBO_SMTP_FROM_NAME", "Bazibo\r\nBcc: x@example.org"),
    ("NETNL_BAZIBO_MAIL_REPLY_TO", "a@example.org, b@example.org"),
    ("NETNL_BAZIBO_MAIL_REPLY_TO", "a@example.org\r\nBcc: x@example.org"),
])
def test_bad_values_are_refused(own_relay_env, var, value):
    own_relay_env[var] = value
    with pytest.raises(SettingsError, match=var):
        load(own_relay_env)


def test_bazibo_mail_settings_are_not_read_while_bazibo_is_off(supporter_env):
    supporter_env["NETNL_BAZIBO_SMTP_PORT"] = "465"  # would be refused with a key set
    assert load(supporter_env).bazibo is None


# --- nothing secret in logs ---------------------------------------------------------


def test_settings_repr_never_carries_the_relay_password(own_relay_env):
    settings = load(own_relay_env)
    assert BAZIBO_PASSWORD not in repr(settings.bazibo)
    assert BAZIBO_PASSWORD not in repr(settings.bazibo.mail)
    assert MAIN_PASSWORD not in repr(settings)
    assert "test-webhook-secret" not in repr(settings)


def test_a_failing_bazibo_relay_logs_and_raises_nothing_secret(own_relay_env, monkeypatch, caplog):
    class _Refusing(_FakeSMTP):
        def auth(self, mechanism, authobject, *, initial_response_ok=True):
            raise smtplib.SMTPAuthenticationError(
                535, f"auth failed for {self.user} / {self.password} at mail.bazibo.example".encode()
            )

    monkeypatch.setattr(smtplib, "SMTP", _Refusing)
    caplog.set_level(logging.DEBUG)
    send = mail.build_sender(load(own_relay_env))
    with pytest.raises(mail.DeliveryError) as exc:
        send(_bazibo_mail())
    for secret in (BAZIBO_PASSWORD, "hallo@bazibo.example", "mail.bazibo.example", "buyer@example.org"):
        assert secret not in caplog.text
        assert secret not in str(exc.value)
    assert "SMTPAuthenticationError" in caplog.text
    assert exc.value.__cause__ is None and exc.value.__suppress_context__


def test_minted_code_never_appears_in_logs_of_a_routed_send(own_relay_env, fake_smtp, caplog):
    caplog.set_level(logging.DEBUG)
    code = bazibo.mint(bytes(range(32)), day=bazibo.day_from_iso("2026-11-27T00:00:00Z"),
                       code_id=bytes(10))
    m = mail.build_bazibo_mail(to="buyer@example.org", code=code, link="https://bazibo.nl/#unlock=" + code,
                               link_pages="https://mwest2020.github.io/bazibo/#unlock=" + code, device_limit=10)
    mail.build_sender(load(own_relay_env))(m)
    assert code not in caplog.text and BAZIBO_PASSWORD not in caplog.text


def test_app_without_injected_sender_routes_the_code_mail(own_relay_env, fake_smtp, fake_opener, clock):
    """`create_app` builds its own sender: the Stripe/BMC path then mails
    through the Bazibo relay and the netnl mail through the bridge's."""
    from netnl.api import create_app

    app = create_app(load(own_relay_env), opener=fake_opener, now=clock)
    app.state.sender(_bazibo_mail())
    app.state.sender(_credential_mail())
    bz_conn, main_conn = fake_smtp.instances
    assert bz_conn.host == "mail.bazibo.example" and main_conn.host == "smtp.example.org"


# --- Date and Message-ID (issue #19) -------------------------------------------


def _alert_mail() -> mail.Mail:
    return mail.build_bazibo_alert_mail(
        to="op@example.org", txn_id="cs_test_1", state="failed", attempts=2,
        code_id="ab12", seen_at="2026-11-28T12:00:00Z",
    )


@pytest.fixture
def real_domains_env(own_relay_env) -> dict:
    """The production identities: the bridge as westerweel.work, Bazibo's
    code mail as bazibo.nl on its own relay."""
    e = dict(own_relay_env)
    e["NETNL_SMTP_FROM"] = "mark@westerweel.work"
    e["NETNL_BAZIBO_SMTP_FROM"] = "hallo@bazibo.nl"
    e["NETNL_BAZIBO_MAIL_REPLY_TO"] = "hallo@bazibo.nl"
    return e


def _assert_date_and_msgid(sent: email.message.Message, domain: str) -> str:
    assert sent.get_all("Date") is not None and len(sent.get_all("Date")) == 1
    # `formatdate(localtime=False)`: UTC, written as "-0000" (RFC 5322
    # 3.3), which `parsedate_to_datetime` returns as a naive datetime.
    assert sent["Date"].endswith(" -0000")
    when = email.utils.parsedate_to_datetime(sent["Date"]).replace(tzinfo=timezone.utc)
    assert abs((datetime.now(timezone.utc) - when).total_seconds()) < 300
    ids = sent.get_all("Message-ID")
    assert ids is not None and len(ids) == 1
    msgid = ids[0]
    assert msgid.startswith("<") and msgid.endswith(">")
    assert msgid[1:-1].rpartition("@")[2] == domain
    return msgid


@pytest.mark.parametrize(
    ("build", "domain"),
    [
        (_bazibo_mail, "bazibo.nl"),
        (_credential_mail, "westerweel.work"),
        (_alert_mail, "westerweel.work"),
        (lambda: mail.build_bazibo_notify_mail(to="op@example.org", code_id="ab", txn_id="t"),
         "westerweel.work"),
        (lambda: mail.build_notify_mail(to="op@example.org", username="s", txn_id="t"),
         "westerweel.work"),
    ],
    ids=["bazibo-code", "supporter-key", "watch-alert", "bazibo-notify", "supporter-notify"],
)
def test_every_mail_has_date_and_message_id_of_its_from_domain(
    real_domains_env, fake_smtp, build, domain,
):
    send = mail.build_sender(load(real_domains_env))
    send(build())
    (conn,) = fake_smtp.instances
    sent = _headers(conn)
    assert parseaddr(sent["From"])[1].rpartition("@")[2] == domain
    _assert_date_and_msgid(sent, domain)


def test_bazibo_code_mail_on_the_interim_relay_takes_the_bridge_domain(env, fake_smtp):
    """Without its own relay the code mail leaves as the bridge's address,
    so its Message-ID follows that From domain."""
    e = dict(env)
    e["NETNL_SMTP_FROM"] = "mark@westerweel.work"
    mail.build_sender(load(e))(_bazibo_mail())
    (conn,) = fake_smtp.instances
    _assert_date_and_msgid(_headers(conn), "westerweel.work")


def test_message_id_is_unique_per_mail(real_domains_env, fake_smtp):
    send = mail.build_sender(load(real_domains_env))
    send(_bazibo_mail())
    send(_bazibo_mail())
    first, second = (_headers(c)["Message-ID"] for c in fake_smtp.instances)
    assert first != second


def test_message_id_domain_from_a_display_name_from(fake_smtp):
    send = mail.smtp_sender(
        host="relay.example", port=25, username=None, password=None,
        from_addr="Bazibo <hallo@bazibo.nl>", mode="plaintext", timeout=5,
    )
    send(_bazibo_mail())
    (conn,) = fake_smtp.instances
    _assert_date_and_msgid(_headers(conn), "bazibo.nl")
