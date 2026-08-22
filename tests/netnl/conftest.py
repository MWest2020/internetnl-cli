from __future__ import annotations

import json
import os

import pytest
from starlette.testclient import TestClient

from fakes import FakeOpener
from internetnl_cli.client import HttpResponse
from netnl.api import create_app
from netnl.settings import Settings, load


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
def app(settings, fake_opener):
    return create_app(settings, opener=fake_opener)


@pytest.fixture
def client(app):
    # `raise_server_exceptions=False`: without this, TestClient re-raises
    # any exception that reached the catch-all handler instead of letting
    # us observe the 500 response it produced.
    return TestClient(app, raise_server_exceptions=False)
