"""`netnl.mail.smtp_sender` authenticates with exactly one AUTH command.

Against a real (local, in-process) SMTP server speaking the wire protocol,
including STARTTLS and implicit TLS with a throwaway certificate:
`smtplib.SMTP.login()` would try CRAM-MD5, then PLAIN, then LOGIN, moving on
after every failure. A server that offers CRAM-MD5 but cannot verify it (as
Plesk with hashed mailbox passwords does) then logs a failed login on every
mail, and a wrong password costs one failure per advertised mechanism; with a
fail2ban-style firewall at the hosting provider, either one risks an IP
block. These tests count the AUTH commands the server actually receives.

No outside network: the server binds 127.0.0.1 and lives for one test.
"""

from __future__ import annotations

import base64
import datetime
import ipaddress
import logging
import socketserver
import ssl
import threading
from dataclasses import dataclass, field

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from netnl import mail
from netnl.settings import load

USER = "mailer@example.org"
PASSWORD = "s3cret-relay-pw-Qx8!"
BAZIBO_USER = "hallo@bazibo.example"
BAZIBO_PASSWORD = "bazibo-relay-pw-Zq9!"
TEST_SEED_B64 = base64.b64encode(bytes(range(32))).decode()


# --- the fake server -------------------------------------------------------------


@dataclass
class _Auth:
    mechanism: str
    ok: bool
    tls: bool


@dataclass
class _ServerConfig:
    mechanisms: tuple[str, ...]
    username: str
    password: str
    tls: str = "none"  # "none" | "starttls" | "ssl"
    server_context: ssl.SSLContext | None = None
    auths: list[_Auth] = field(default_factory=list)
    delivered: list[tuple[str, list[str]]] = field(default_factory=list)


class _Handler(socketserver.BaseRequestHandler):
    """Just enough ESMTP for smtplib: EHLO/HELO, STARTTLS, AUTH
    (PLAIN, LOGIN, CRAM-MD5), MAIL/RCPT/DATA, RSET/NOOP, QUIT.

    CRAM-MD5 always fails, as on a server that advertises it but stores only
    password hashes. With STARTTLS configured, AUTH is advertised only after
    the upgrade, like a submission port that refuses AUTH in the clear."""

    cfg: _ServerConfig

    def setup(self):
        self.sock = self.request
        self.tls = False
        if self.cfg.tls == "ssl":
            self.sock = self.cfg.server_context.wrap_socket(self.sock, server_side=True)
            self.tls = True
        self.reader = self.sock.makefile("rb")

    def _send(self, line: str) -> None:
        self.sock.sendall(line.encode() + b"\r\n")

    def _recv(self) -> str | None:
        raw = self.reader.readline(65536)
        if not raw:
            return None
        return raw.decode().rstrip("\r\n")

    def handle(self):
        self._send("220 fake.example ESMTP")
        while (line := self._recv()) is not None:
            verb, _, arg = line.partition(" ")
            verb = verb.upper()
            if verb == "EHLO":
                features = ["fake.example"]
                if self.cfg.tls == "starttls" and not self.tls:
                    features.append("STARTTLS")
                if self.cfg.tls != "starttls" or self.tls:
                    features.append("AUTH " + " ".join(self.cfg.mechanisms))
                features.append("8BITMIME")
                for f in features[:-1]:
                    self._send("250-" + f)
                self._send("250 " + features[-1])
            elif verb == "HELO":
                self._send("250 fake.example")
            elif verb == "STARTTLS" and self.cfg.tls == "starttls" and not self.tls:
                self._send("220 2.0.0 ready")
                self.reader.close()
                self.sock = self.cfg.server_context.wrap_socket(self.sock, server_side=True)
                self.reader = self.sock.makefile("rb")
                self.tls = True
            elif verb == "AUTH":
                self._auth(arg)
            elif verb == "MAIL":
                self.mail_from, self.rcpts = arg, []
                self._send("250 2.1.0 ok")
            elif verb == "RCPT":
                self.rcpts.append(arg)
                self._send("250 2.1.5 ok")
            elif verb == "DATA":
                self._send("354 go ahead")
                while (data := self._recv()) not in (".", None):
                    pass
                self.cfg.delivered.append((self.mail_from, self.rcpts))
                self._send("250 2.0.0 queued")
            elif verb in ("RSET", "NOOP"):
                self._send("250 ok")
            elif verb == "QUIT":
                self._send("221 bye")
                return
            else:
                self._send("500 5.5.2 unknown command")

    def _auth(self, arg: str) -> None:
        mechanism, _, initial = arg.partition(" ")
        mechanism = mechanism.upper()
        if mechanism not in self.cfg.mechanisms:
            self._send("504 5.5.4 mechanism not offered")
            return
        if mechanism == "PLAIN":
            if not initial:
                self._send("334 ")
                initial = self._recv() or ""
            _, user, password = base64.b64decode(initial).decode().split("\0")
            ok = (user, password) == (self.cfg.username, self.cfg.password)
        elif mechanism == "LOGIN":
            if not initial:
                self._send("334 " + base64.b64encode(b"Username:").decode())
                initial = self._recv() or ""
            user = base64.b64decode(initial).decode()
            self._send("334 " + base64.b64encode(b"Password:").decode())
            password = base64.b64decode(self._recv() or "").decode()
            ok = (user, password) == (self.cfg.username, self.cfg.password)
        else:  # CRAM-MD5: offered, never verifiable here
            self._send("334 " + base64.b64encode(b"<1.2@fake.example>").decode())
            self._recv()
            ok = False
        self.cfg.auths.append(_Auth(mechanism, ok, self.tls))
        if ok:
            self._send("235 2.7.0 Authentication successful")
        else:
            self._send("535 5.7.8 Authentication credentials invalid")


class _Server(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True


@pytest.fixture(scope="module")
def tls_contexts(tmp_path_factory):
    """A throwaway self-signed certificate for 127.0.0.1; the server context
    serves it, the client context trusts exactly it (hostname checked)."""
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name).issuer_name(name).public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(hours=1))
        .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), critical=False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    d = tmp_path_factory.mktemp("smtp-tls")
    cert_pem = cert.public_bytes(serialization.Encoding.PEM)
    (d / "cert.pem").write_bytes(cert_pem)
    (d / "key.pem").write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ))
    server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_ctx.load_cert_chain(d / "cert.pem", d / "key.pem")
    client_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    client_ctx.load_verify_locations(cadata=cert_pem.decode())
    return server_ctx, client_ctx


@pytest.fixture
def smtp_server(tls_contexts, monkeypatch):
    """Factory: `smtp_server(mechanisms, tls=...)` -> (config, port)."""
    server_ctx, client_ctx = tls_contexts
    # `smtp_sender` builds its client context with `ssl.create_default_context()`;
    # here that context trusts the throwaway certificate (and nothing else).
    monkeypatch.setattr(mail.ssl, "create_default_context", lambda *a, **k: client_ctx)
    servers: list[_Server] = []

    def start(mechanisms, *, tls="starttls", username=USER, password=PASSWORD):
        cfg = _ServerConfig(tuple(mechanisms), username, password, tls, server_ctx)
        handler = type("_BoundHandler", (_Handler,), {"cfg": cfg})
        server = _Server(("127.0.0.1", 0), handler)
        threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        servers.append(server)
        return cfg, server.server_address[1]

    yield start
    for server in servers:
        server.shutdown()
        server.server_close()


def _send(port, *, mode="starttls", username=USER, password=PASSWORD):
    sender = mail.smtp_sender(
        host="127.0.0.1", port=port, username=username, password=password,
        from_addr="netnl@example.org", mode=mode, timeout=5,
    )
    sender(mail.Mail(to="donor@example.org", subject="s", body="b"))


# --- exactly one AUTH ------------------------------------------------------------


def test_cram_md5_offered_first_still_one_auth_plain_after_starttls(smtp_server):
    cfg, port = smtp_server(["CRAM-MD5", "PLAIN"])
    _send(port)
    assert cfg.auths == [_Auth("PLAIN", ok=True, tls=True)]
    assert len(cfg.delivered) == 1


def test_plain_preferred_over_login_whatever_the_advertised_order(smtp_server):
    cfg, port = smtp_server(["CRAM-MD5", "LOGIN", "PLAIN"])
    _send(port)
    assert cfg.auths == [_Auth("PLAIN", ok=True, tls=True)]


def test_plain_only(smtp_server):
    cfg, port = smtp_server(["PLAIN"])
    _send(port)
    assert cfg.auths == [_Auth("PLAIN", ok=True, tls=True)]
    assert len(cfg.delivered) == 1


def test_login_only_when_plain_is_not_offered(smtp_server):
    cfg, port = smtp_server(["CRAM-MD5", "LOGIN"])
    _send(port)
    assert cfg.auths == [_Auth("LOGIN", ok=True, tls=True)]
    assert len(cfg.delivered) == 1


def test_implicit_tls_mode_also_sends_one_auth_plain(smtp_server):
    cfg, port = smtp_server(["CRAM-MD5", "PLAIN"], tls="ssl")
    _send(port, mode="ssl")
    assert cfg.auths == [_Auth("PLAIN", ok=True, tls=True)]


def test_each_mail_is_one_auth(smtp_server):
    cfg, port = smtp_server(["CRAM-MD5", "PLAIN", "LOGIN"])
    for _ in range(3):
        _send(port)
    assert [a.mechanism for a in cfg.auths] == ["PLAIN"] * 3
    assert all(a.ok for a in cfg.auths)


# --- failures: exactly one, and nothing secret -------------------------------------


def test_wrong_password_is_exactly_one_failure(smtp_server, caplog):
    cfg, port = smtp_server(["CRAM-MD5", "PLAIN", "LOGIN"])
    caplog.set_level(logging.DEBUG)
    with pytest.raises(mail.DeliveryError) as exc:
        _send(port, password="wrong-password-Kp3!")
    assert cfg.auths == [_Auth("PLAIN", ok=False, tls=True)]
    assert cfg.delivered == []
    for secret in ("wrong-password-Kp3!", USER, "127.0.0.1", "535", "credentials invalid"):
        assert secret not in str(exc.value)
        assert secret not in caplog.text
    assert "SMTPAuthenticationError" in caplog.text
    assert exc.value.__cause__ is None and exc.value.__suppress_context__


def test_wrong_password_on_login_only_is_exactly_one_failure(smtp_server):
    cfg, port = smtp_server(["LOGIN"])
    with pytest.raises(mail.DeliveryError):
        _send(port, password="wrong-password-Kp3!")
    assert cfg.auths == [_Auth("LOGIN", ok=False, tls=True)]


def test_no_plain_or_login_means_no_auth_attempt_at_all(smtp_server, caplog):
    cfg, port = smtp_server(["CRAM-MD5"])
    caplog.set_level(logging.DEBUG)
    with pytest.raises(mail.DeliveryError):
        _send(port)
    assert cfg.auths == []
    assert cfg.delivered == []
    assert PASSWORD not in caplog.text


def test_no_auth_advertised_means_no_auth_attempt(smtp_server):
    cfg, port = smtp_server([], tls="starttls")
    with pytest.raises(mail.DeliveryError):
        _send(port)
    assert cfg.auths == []


def test_without_username_no_auth_is_sent(smtp_server):
    cfg, port = smtp_server(["CRAM-MD5", "PLAIN"])
    _send(port, username=None, password=None)
    assert cfg.auths == []
    assert len(cfg.delivered) == 1


# --- both relays behave the same ------------------------------------------------------


@pytest.fixture
def two_relays_env(supporter_env, smtp_server):
    main_cfg, main_port = smtp_server(["CRAM-MD5", "PLAIN", "LOGIN"])
    bazibo_cfg, bazibo_port = smtp_server(
        ["CRAM-MD5", "PLAIN", "LOGIN"], username=BAZIBO_USER, password=BAZIBO_PASSWORD
    )
    env = dict(supporter_env)
    env.update({
        "NETNL_SMTP_HOST": "127.0.0.1",
        "NETNL_SMTP_PORT": str(main_port),
        "NETNL_SMTP_USERNAME": USER,
        "NETNL_SMTP_PASSWORD": PASSWORD,
        "NETNL_BAZIBO_SIGNING_KEY": TEST_SEED_B64,
        "NETNL_BAZIBO_SMTP_HOST": "127.0.0.1",
        "NETNL_BAZIBO_SMTP_PORT": str(bazibo_port),
        "NETNL_BAZIBO_SMTP_USERNAME": BAZIBO_USER,
        "NETNL_BAZIBO_SMTP_PASSWORD": BAZIBO_PASSWORD,
        "NETNL_BAZIBO_SMTP_FROM": BAZIBO_USER,
    })
    return env, main_cfg, bazibo_cfg


def _bazibo_mail() -> mail.Mail:
    code = "BZB1.test"
    return mail.build_bazibo_mail(
        to="buyer@example.org", code=code, link=f"https://bazibo.nl/#unlock={code}",
        link_pages=f"https://mwest2020.github.io/bazibo/#unlock={code}", device_limit=10,
    )


def test_main_and_bazibo_relay_each_send_one_auth_plain(two_relays_env):
    env, main_cfg, bazibo_cfg = two_relays_env
    send = mail.build_sender(load(env))
    send(mail.build_notify_mail(to="op@example.org", username="u", txn_id="t"))
    send(_bazibo_mail())
    assert main_cfg.auths == [_Auth("PLAIN", ok=True, tls=True)]
    assert bazibo_cfg.auths == [_Auth("PLAIN", ok=True, tls=True)]
    assert len(main_cfg.delivered) == 1 and len(bazibo_cfg.delivered) == 1


def test_bazibo_relay_wrong_password_is_one_failure(two_relays_env):
    env, main_cfg, bazibo_cfg = two_relays_env
    env = dict(env, NETNL_BAZIBO_SMTP_PASSWORD="wrong-password-Kp3!")
    send = mail.build_sender(load(env))
    with pytest.raises(mail.DeliveryError) as exc:
        send(_bazibo_mail())
    assert bazibo_cfg.auths == [_Auth("PLAIN", ok=False, tls=True)]
    assert main_cfg.auths == []
    assert "wrong-password-Kp3!" not in str(exc.value)


def test_main_relay_wrong_password_is_one_failure(two_relays_env):
    env, main_cfg, bazibo_cfg = two_relays_env
    env = dict(env, NETNL_SMTP_PASSWORD="wrong-password-Kp3!")
    send = mail.build_sender(load(env))
    with pytest.raises(mail.DeliveryError):
        send(mail.build_notify_mail(to="op@example.org", username="u", txn_id="t"))
    assert main_cfg.auths == [_Auth("PLAIN", ok=False, tls=True)]
    assert bazibo_cfg.auths == []

