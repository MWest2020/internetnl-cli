from __future__ import annotations

import os

import pytest

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
