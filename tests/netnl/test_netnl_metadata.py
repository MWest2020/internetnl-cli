from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from starlette.testclient import TestClient

from fakes import METADATA_REPLY, FakeOpener
from netnl.api import create_app

from conftest import queue_json


class _Clock:
    def __init__(self, start: datetime) -> None:
        self._now = start

    def __call__(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += timedelta(seconds=seconds)


@pytest.fixture
def clock() -> _Clock:
    return _Clock(datetime(2026, 1, 1, tzinfo=timezone.utc))


@pytest.fixture
def cached_client(settings, fake_opener, clock):
    app = create_app(settings, opener=fake_opener, now=clock)
    return TestClient(app, raise_server_exceptions=False)


def test_metadata_passthrough_unchanged(cached_client, fake_opener):
    queue_json(fake_opener, METADATA_REPLY)
    resp = cached_client.get("/metadata/report")
    assert resp.status_code == 200
    assert resp.json() == METADATA_REPLY
    assert len(fake_opener.calls) == 1


def test_metadata_cached_within_ttl(cached_client, fake_opener, clock):
    queue_json(fake_opener, METADATA_REPLY)
    first = cached_client.get("/metadata/report")
    assert first.status_code == 200

    clock.advance(10)  # well within the default 3600s TTL
    second = cached_client.get("/metadata/report")
    assert second.status_code == 200
    assert second.json() == METADATA_REPLY
    assert len(fake_opener.calls) == 1


def test_metadata_refetched_after_ttl(cached_client, fake_opener, clock, settings):
    queue_json(fake_opener, METADATA_REPLY)
    queue_json(fake_opener, METADATA_REPLY)

    first = cached_client.get("/metadata/report")
    assert first.status_code == 200

    clock.advance(settings.metadata_ttl + 1)
    second = cached_client.get("/metadata/report")
    assert second.status_code == 200
    assert len(fake_opener.calls) == 2
