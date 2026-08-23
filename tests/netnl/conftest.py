from __future__ import annotations

import base64
import json
import os
from datetime import datetime, timedelta, timezone

import pytest
from starlette.testclient import TestClient

from fakes import FakeOpener
from internetnl_cli.client import HttpResponse
from netnl import auth, store
from netnl.api import create_app
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
