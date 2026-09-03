from __future__ import annotations

import base64
import logging

from fakes import raising_opener

from conftest import bmc_payload, post_webhook, queue_json
from internetnl_cli.errors import TransportError
from netnl import store

_UPSTREAM_USERNAME = "upstream-user"
_UPSTREAM_PASSWORD = "upstream-secret"
_UPSTREAM_BASIC = base64.b64encode(f"{_UPSTREAM_USERNAME}:{_UPSTREAM_PASSWORD}".encode()).decode()


def _assert_no_leak(resp, caplog):
    haystacks = [resp.text, str(dict(resp.headers)), caplog.text]
    for haystack in haystacks:
        assert _UPSTREAM_USERNAME not in haystack
        assert _UPSTREAM_PASSWORD not in haystack
        assert _UPSTREAM_BASIC not in haystack


def test_upstream_401_never_leaks_credential(client, fake_opener, tenant, caplog):
    caplog.set_level(logging.DEBUG, logger="netnl")
    caplog.set_level(logging.DEBUG, logger="uvicorn")

    queue_json(fake_opener, {"api_version": "2.6.0", "error": {"label": "unauthorised", "msg": "x"}}, status=401)
    resp = client.post(
        "/requests", json={"type": "web", "domains": ["example.nl"]}, headers=tenant["headers"]
    )

    assert resp.status_code == 502
    assert resp.json()["error"]["label"] == "upstream-error"
    _assert_no_leak(resp, caplog)
    # The upstream hostname is allowed to appear (it names the failure);
    # the credential must not.
    assert "batch.internal" in resp.json()["error"]["msg"]


def test_upstream_transport_failure_never_leaks_credential(settings_env, tmp_path, caplog):
    caplog.set_level(logging.DEBUG, logger="netnl")
    caplog.set_level(logging.DEBUG, logger="uvicorn")

    from starlette.testclient import TestClient

    from conftest import add_test_credential, basic_auth_header
    from netnl.api import create_app
    from netnl.settings import load

    # A DB path of its own — distinct from other tests' `settings` fixture
    # instance, so this credential insert cannot collide with theirs.
    env = dict(settings_env)
    env["NETNL_DB"] = str(tmp_path / "leak-transport.sqlite3")
    settings = load(env)

    failing_opener = raising_opener(
        TransportError("connection refused while contacting batch.internal")
    )
    failing_app = create_app(settings, opener=failing_opener)
    add_test_credential(failing_app, "tenant", "tenant-secret")
    failing_client = TestClient(failing_app, raise_server_exceptions=False)

    resp = failing_client.post(
        "/requests",
        json={"type": "web", "domains": ["example.nl"]},
        headers=basic_auth_header("tenant", "tenant-secret"),
    )

    assert resp.status_code == 502
    assert resp.json()["error"]["label"] == "upstream-unreachable"
    _assert_no_leak(resp, caplog)
    assert "batch.internal" in resp.json()["error"]["msg"]


# --- supporter webhook bridge (openspec/changes/add-supporter-issuance) ----

_WEBHOOK_SECRET = "webhook-super-secret"
_SMTP_PASSWORD = "smtp-super-secret"
_SUPPORTER_EMAIL = "donor-private@example.org"


def _supporter_leak_env(settings_env, tmp_path, **overrides) -> dict:
    env = dict(settings_env)
    env["NETNL_DB"] = str(tmp_path / "leak-supporter.sqlite3")
    env["NETNL_BMC_WEBHOOK_SECRET"] = _WEBHOOK_SECRET
    env["NETNL_PUBLIC_ENDPOINT"] = "https://facade.example.org"
    env["NETNL_SMTP_HOST"] = "smtp.example.org"
    env["NETNL_SMTP_FROM"] = "netnl@example.org"
    env["NETNL_SMTP_USERNAME"] = "mailer"
    env["NETNL_SMTP_PASSWORD"] = _SMTP_PASSWORD
    env.update(overrides)
    return env


def _raw_db_dump(db_path) -> bytes:
    """The main database file *and* its WAL file (WAL mode — see
    `store.connect` — means a recent write may not be checkpointed into the
    main file yet) concatenated, so a leak check against "the database"
    cannot pass merely because the write in question still lives in the
    WAL.
    """
    dump = db_path.read_bytes()
    wal_path = db_path.with_name(db_path.name + "-wal")
    if wal_path.exists():
        dump += wal_path.read_bytes()
    return dump


def _no_leak_haystacks(resp, caplog, db_path) -> list[str]:
    haystacks = [resp.text, str(dict(resp.headers)), caplog.text]
    haystacks.append(_raw_db_dump(db_path).decode("latin-1"))
    return haystacks


def _assert_supporter_no_leak(resp, caplog, db_path, *, issued_password: str | None = None):
    haystacks = _no_leak_haystacks(resp, caplog, db_path)
    for haystack in haystacks:
        assert _WEBHOOK_SECRET not in haystack
        assert _SMTP_PASSWORD not in haystack
        assert _SUPPORTER_EMAIL not in haystack
        if issued_password is not None:
            assert issued_password not in haystack


def _build_supporter_app(env, *, sender):
    from starlette.testclient import TestClient
    from fakes import FakeOpener
    from netnl.api import create_app
    from netnl.settings import load

    settings = load(env)
    app = create_app(settings, opener=FakeOpener(), sender=sender)
    return settings, TestClient(app, raise_server_exceptions=False)


def test_supporter_issuance_never_leaks_secrets_or_pii(settings_env, tmp_path, caplog):
    caplog.set_level(logging.DEBUG, logger="netnl")
    caplog.set_level(logging.DEBUG, logger="uvicorn")

    sent = []

    def sender(mail_obj):
        sent.append(mail_obj)

    env = _supporter_leak_env(settings_env, tmp_path)
    settings, client = _build_supporter_app(env, sender=sender)
    payload = bmc_payload(data={"email": _SUPPORTER_EMAIL, "transaction_id": "txn-leak-1"})

    # Success.
    resp = post_webhook(client, payload, secret=_WEBHOOK_SECRET)
    assert resp.status_code == 200
    assert len(sent) == 1
    issued_password = next(
        line.split(":", 1)[1].strip()
        for line in sent[0].body.splitlines()
        if line.strip().startswith("Password:")
    )
    _assert_supporter_no_leak(resp, caplog, tmp_path / "leak-supporter.sqlite3", issued_password=issued_password)
    # The mail itself legitimately carries the address and password — this
    # only proves neither reaches the reply, the log, or the database.
    assert sent[0].to == _SUPPORTER_EMAIL

    # Duplicate replay.
    resp_dup = post_webhook(client, payload, secret=_WEBHOOK_SECRET)
    assert resp_dup.status_code == 200
    _assert_supporter_no_leak(resp_dup, caplog, tmp_path / "leak-supporter.sqlite3", issued_password=issued_password)

    # Bad signature (401).
    resp_401 = client.post(
        "/webhooks/bmc", content=b'{"type": "donation.created"}',
        headers={"Content-Type": "application/json", "X-Signature-Sha256": "0" * 64},
    )
    assert resp_401.status_code == 401
    _assert_supporter_no_leak(resp_401, caplog, tmp_path / "leak-supporter.sqlite3")

    # Malformed body (400).
    resp_400 = post_webhook(client, {"type": "donation.created", "live_mode": True, "data": {}}, secret=_WEBHOOK_SECRET)
    assert resp_400.status_code == 400
    _assert_supporter_no_leak(resp_400, caplog, tmp_path / "leak-supporter.sqlite3")


def test_supporter_delivery_failure_never_leaks_secrets(settings_env, tmp_path, caplog):
    caplog.set_level(logging.DEBUG, logger="netnl")
    caplog.set_level(logging.DEBUG, logger="uvicorn")

    from netnl.mail import DeliveryError

    def failing_sender(mail_obj):
        raise DeliveryError("failed to deliver supporter credential mail")

    env = _supporter_leak_env(settings_env, tmp_path)
    settings, client = _build_supporter_app(env, sender=failing_sender)
    payload = bmc_payload(data={"email": _SUPPORTER_EMAIL, "transaction_id": "txn-leak-2"})

    resp = post_webhook(client, payload, secret=_WEBHOOK_SECRET)
    assert resp.status_code == 503
    assert resp.json()["error"]["label"] == "delivery-failed"
    _assert_supporter_no_leak(resp, caplog, tmp_path / "leak-supporter.sqlite3")


def test_supporter_undeliverable_never_leaks_the_address(settings_env, tmp_path, caplog):
    caplog.set_level(logging.DEBUG, logger="netnl")
    caplog.set_level(logging.DEBUG, logger="uvicorn")

    def sender(mail_obj):
        raise AssertionError("no mail should ever be sent for an undeliverable delivery")

    env = _supporter_leak_env(settings_env, tmp_path)
    settings, client = _build_supporter_app(env, sender=sender)
    payload = bmc_payload(data={"transaction_id": "txn-leak-3"})
    del payload["data"]["email"]

    resp = post_webhook(client, payload, secret=_WEBHOOK_SECRET)
    assert resp.status_code == 200
    assert resp.json() == {"status": "ignored"}
    _assert_supporter_no_leak(resp, caplog, tmp_path / "leak-supporter.sqlite3")


def test_supporter_db_dump_has_no_pii_after_issuance(settings_env, tmp_path):
    sent = []

    def sender(mail_obj):
        sent.append(mail_obj)

    env = _supporter_leak_env(settings_env, tmp_path)
    settings, client = _build_supporter_app(env, sender=sender)
    payload = bmc_payload(data={"email": _SUPPORTER_EMAIL, "transaction_id": "txn-leak-4"})
    resp = post_webhook(client, payload, secret=_WEBHOOK_SECRET)
    assert resp.status_code == 200

    conn = store.connect(settings.db)
    try:
        row = store.find_issuance(conn, "txn-leak-4")
        assert row is not None
        # The transaction id and generated username persist; the
        # supporter's own email address never does.
        dump = _raw_db_dump(tmp_path / "leak-supporter.sqlite3")
        assert _SUPPORTER_EMAIL.encode() not in dump
        assert row["username"].encode() in dump
        assert b"txn-leak-4" in dump
    finally:
        conn.close()
