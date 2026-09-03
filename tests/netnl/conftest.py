from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from datetime import datetime, timedelta, timezone

import pytest
from starlette.testclient import TestClient

from fakes import FakeOpener
from internetnl_cli.client import HttpResponse
from netnl import auth, store
from netnl.api import create_app
from netnl.mail import Mail
from netnl.settings import Settings, load


class Clock:
    """An injectable, manually-advanced stand-in for wall-clock time."""

    def __init__(self, start: datetime | None = None) -> None:
        self._now = start or datetime(2026, 1, 1, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += timedelta(seconds=seconds)


@pytest.fixture(autouse=True)
def isolated_netnl_env(monkeypatch):
    """Strip any ambient `NETNL_*` variable before each test."""
    for key in [k for k in os.environ if k.startswith("NETNL_")]:
        monkeypatch.delenv(key, raising=False)
    yield


@pytest.fixture(autouse=True)
def _reset_auth_failure_aggregator():
    """`netnl.auth`'s per-minute auth-failure aggregator (round-2 fix,
    finding 2) is deliberately process-global, in-memory state — it is not
    scoped to a single app/test. Without this, one test's failed-auth
    buckets (keyed on username + route, both commonly reused across test
    files, e.g. `"tenant"` + `"/metadata/report"`) could leak into another
    test's assertions about exactly how many audit rows exist.
    """
    from netnl import auth

    auth._auth_failure_buckets.clear()
    yield
    auth._auth_failure_buckets.clear()


@pytest.fixture
def settings_env(tmp_path) -> dict:
    return {
        "NETNL_UPSTREAM_ENDPOINT": "https://batch.internal/api/batch/v2",
        "NETNL_UPSTREAM_USERNAME": "upstream-user",
        "NETNL_UPSTREAM_PASSWORD": "upstream-secret",
        "NETNL_DB": str(tmp_path / "netnl.sqlite3"),
    }


@pytest.fixture
def settings(settings_env) -> Settings:
    return load(settings_env)


def queue_json(fake: FakeOpener, payload: dict, status: int = 200) -> None:
    fake._responses.append(HttpResponse(status=status, body=json.dumps(payload).encode()))


@pytest.fixture
def fake_opener() -> FakeOpener:
    return FakeOpener()


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def app(settings, fake_opener, clock):
    return create_app(settings, opener=fake_opener, now=clock)


@pytest.fixture
def client(app):
    # `raise_server_exceptions=False`: without this, TestClient re-raises
    # any exception that reached the catch-all handler instead of letting
    # us observe the 500 response it produced.
    return TestClient(app, raise_server_exceptions=False)


def basic_auth_header(username: str, password: str) -> dict:
    token = base64.b64encode(f"{username}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def add_test_credential(app, username: str, password: str) -> None:
    """Insert a tenant credential straight into the store — bypasses
    `netnl-admin` (built in B6) so B4/B5 tests can authenticate.

    Round-1 fix (B1): there is no shared `app.state.conn` any more — every
    request opens and closes its own connection (`store.get_conn`). Test
    setup that needs to touch the database does the same: open its own
    short-lived connection to the same file.
    """
    conn = store.connect(app.state.settings.db)
    try:
        salt = auth.new_salt()
        store.add_credential(
            conn,
            username=username,
            password_hash=auth.hash_password(password, salt),
            salt=salt.hex(),
            created_at=store.utcnow_iso(app.state.now),
        )
    finally:
        conn.close()


@pytest.fixture
def conn(app):
    """A connection for tests to inspect/mutate the facade's database
    directly — distinct from (and closed independently of) any connection a
    request handler opens via `store.get_conn` (round-1 fix, B1).
    """
    c = store.connect(app.state.settings.db)
    yield c
    c.close()


@pytest.fixture
def tenant(app):
    """A ready-to-use tenant credential: (username, password, auth header)."""
    username, password = "tenant", "tenant-secret"
    add_test_credential(app, username, password)
    return {"username": username, "password": password, "headers": basic_auth_header(username, password)}


# --- demo (openspec/changes/add-demo-run) -----------------------------------

DEMO_ORIGIN = "https://demo.example.org"
DEMO_TENANT = "netnl-demo"


@pytest.fixture(autouse=True)
def _reset_demo_state():
    """`netnl.demo`'s per-IP-bucket and per-domain-cooldown structures are
    process-global, in-memory state (design.md, D4/D5) — not scoped to a
    single app/test, exactly like `netnl.auth`'s failed-authentication
    aggregator above.
    """
    from netnl import demo

    demo.reset_state()
    yield
    demo.reset_state()


@pytest.fixture
def demo_env(settings_env) -> dict:
    env = dict(settings_env)
    env["NETNL_DEMO_ENABLED"] = "1"
    env["NETNL_DEMO_ALLOWED_ORIGIN"] = DEMO_ORIGIN
    env["NETNL_DEMO_TENANT"] = DEMO_TENANT
    return env


@pytest.fixture
def demo_settings(demo_env) -> Settings:
    return load(demo_env)


@pytest.fixture
def demo_app(demo_settings, fake_opener, clock):
    """A facade with the demo family enabled and its borrowed credential
    row already issued — mirrors `netnl-admin user add` followed by
    throwing the printed password away (design.md, D3): no test ever
    authenticates as this credential.
    """
    app = create_app(demo_settings, opener=fake_opener, now=clock)
    add_test_credential(app, DEMO_TENANT, "thrown-away-password")
    return app


@pytest.fixture
def demo_client(demo_app):
    return TestClient(demo_app, raise_server_exceptions=False)


# --- supporter webhook bridge (openspec/changes/add-supporter-issuance) ----

SUPPORTER_SECRET = "test-webhook-secret-32-chars-long!!"


class RecordingSender:
    """A `netnl.mail.Sender` that appends every `Mail` it is asked to send
    to a list instead of touching a real SMTP server. `fail_next` (a set of
    zero-indexed call counts, or `True` for "every call") lets a test make
    a specific call raise `DeliveryError` without needing a real SMTP
    failure to reproduce.
    """

    def __init__(self) -> None:
        self.sent: list[Mail] = []
        self.fail_always = False
        self.fail_on_calls: set[int] = set()

    def __call__(self, mail_obj: Mail) -> None:
        from netnl.mail import DeliveryError

        call_index = len(self.sent)
        if self.fail_always or call_index in self.fail_on_calls:
            self.sent.append(mail_obj)  # still record the attempt
            raise DeliveryError("failed to deliver supporter credential mail")
        self.sent.append(mail_obj)


@pytest.fixture
def recording_sender() -> RecordingSender:
    return RecordingSender()


@pytest.fixture
def supporter_env(settings_env) -> dict:
    env = dict(settings_env)
    env["NETNL_BMC_WEBHOOK_SECRET"] = SUPPORTER_SECRET
    env["NETNL_PUBLIC_ENDPOINT"] = "https://facade.example.org"
    env["NETNL_SMTP_HOST"] = "smtp.example.org"
    env["NETNL_SMTP_FROM"] = "netnl@example.org"
    return env


@pytest.fixture
def supporter_settings(supporter_env) -> Settings:
    return load(supporter_env)


@pytest.fixture
def supporter_app(supporter_settings, fake_opener, clock, recording_sender):
    return create_app(
        supporter_settings, opener=fake_opener, now=clock, sender=recording_sender
    )


@pytest.fixture
def supporter_client(supporter_app):
    return TestClient(supporter_app, raise_server_exceptions=False)


def sign_body(body: bytes, secret: str = SUPPORTER_SECRET) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def bmc_payload(**overrides) -> dict:
    """A `donation.created` BMC webhook payload literal — derived from
    documented shape; replace with an owner-supplied real delivery once one
    is available (see `docs/how-to/supporter-webhook.md`).
    """
    base = {
        "type": "donation.created",
        "live_mode": True,
        "attempt": 1,
        "data": {
            "id": 12345,
            "amount": "5.00",
            "currency": "EUR",
            "transaction_id": "txn-abc123",
            "email": "supporter@example.org",
            "supporter_name": "Jane Doe",
            "support_note": "keep it up!",
        },
    }
    base["data"].update(overrides.pop("data", {}))
    base.update(overrides)
    return base


def post_webhook(client, payload: dict, *, secret: str = SUPPORTER_SECRET, header: str = "X-Signature-Sha256"):
    body = json.dumps(payload).encode()
    return client.post(
        "/webhooks/bmc",
        content=body,
        headers={"Content-Type": "application/json", header: sign_body(body, secret)},
    )
