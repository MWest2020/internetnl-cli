from __future__ import annotations

import copy

import pytest

from fakes import REGISTER_REPLY, STATUS_DONE, STATUS_RUNNING

from conftest import queue_json


def _submit(client, fake_opener, headers, domains, queue_reply=True, reply=REGISTER_REPLY):
    if queue_reply:
        queue_json(fake_opener, reply)
    return client.post("/requests", json={"type": "web", "domains": domains}, headers=headers)


def _done_register_reply() -> dict:
    """A submit reply that lands straight on `done` — used where a test
    cares about one limit in isolation and must not trigger the
    concurrency check's non-terminal-run refresh as a side effect.
    """
    reply = copy.deepcopy(REGISTER_REPLY)
    reply["request"]["status"] = "done"
    reply["request"]["finished_date"] = "2026-01-01T00:00:00+00:00"
    return reply


def test_exactly_at_max_domains_succeeds(settings_env, tmp_path):
    from netnl.api import create_app
    from starlette.testclient import TestClient
    from conftest import add_test_credential, basic_auth_header
    from fakes import FakeOpener
    from netnl.settings import load

    env = dict(settings_env)
    env["NETNL_MAX_DOMAINS"] = "3"
    env["NETNL_DB"] = str(tmp_path / "size.sqlite3")
    settings = load(env)
    fake_opener = FakeOpener()
    app = create_app(settings, opener=fake_opener)
    add_test_credential(app, "tenant", "secret")
    client = TestClient(app, raise_server_exceptions=False)
    headers = basic_auth_header("tenant", "secret")

    resp = _submit(client, fake_opener, headers, ["a.nl", "b.nl", "c.nl"])
    assert resp.status_code == 200
    assert len(fake_opener.calls) == 1


def test_one_over_max_domains_fails_400_without_touching_upstream(settings_env, tmp_path):
    from netnl.api import create_app
    from starlette.testclient import TestClient
    from conftest import add_test_credential, basic_auth_header
    from fakes import FakeOpener
    from netnl.settings import load

    env = dict(settings_env)
    env["NETNL_MAX_DOMAINS"] = "3"
    env["NETNL_DB"] = str(tmp_path / "size.sqlite3")
    settings = load(env)
    fake_opener = FakeOpener()
    app = create_app(settings, opener=fake_opener)
    add_test_credential(app, "tenant", "secret")
    client = TestClient(app, raise_server_exceptions=False)
    headers = basic_auth_header("tenant", "secret")

    resp = _submit(
        client, fake_opener, headers, ["a.nl", "b.nl", "c.nl", "d.nl"], queue_reply=False
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body["error"]["label"] == "bad-request"
    assert "3" in body["error"]["msg"]
    assert len(fake_opener.calls) == 0
    assert resp.headers["X-Netnl-Instance"] == settings.instance


def test_rate_limit_does_not_touch_upstream(settings_env, tmp_path):
    from netnl.api import create_app
    from starlette.testclient import TestClient
    from conftest import Clock, add_test_credential, basic_auth_header
    from fakes import FakeOpener
    from netnl.settings import load

    env = dict(settings_env)
    env["NETNL_RATE_LIMIT"] = "2"
    env["NETNL_DB"] = str(tmp_path / "rate.sqlite3")
    settings = load(env)
    fake_opener = FakeOpener()
    clock = Clock()
    app = create_app(settings, opener=fake_opener, now=clock)
    add_test_credential(app, "tenant", "secret")
    client = TestClient(app, raise_server_exceptions=False)
    headers = basic_auth_header("tenant", "secret")

    done_reply = _done_register_reply()
    for _ in range(2):
        resp = _submit(client, fake_opener, headers, ["a.nl"], reply=done_reply)
        assert resp.status_code == 200

    third = _submit(client, fake_opener, headers, ["a.nl"], queue_reply=False)
    assert third.status_code == 429
    assert third.json()["error"]["label"] == "rate-limited"
    assert len(fake_opener.calls) == 2  # only the two accepted submits

    clock.advance(61 * 60)
    fourth = _submit(client, fake_opener, headers, ["a.nl"], reply=done_reply)
    assert fourth.status_code == 200
    assert len(fake_opener.calls) == 3


def test_max_concurrent_blocks_then_succeeds_after_refresh_marks_done(settings_env, tmp_path):
    from netnl.api import create_app
    from starlette.testclient import TestClient
    from conftest import add_test_credential, basic_auth_header
    from fakes import FakeOpener
    from netnl.settings import load

    env = dict(settings_env)
    env["NETNL_MAX_CONCURRENT"] = "1"
    env["NETNL_DB"] = str(tmp_path / "concurrency.sqlite3")
    settings = load(env)
    fake_opener = FakeOpener()
    app = create_app(settings, opener=fake_opener)
    add_test_credential(app, "tenant", "secret")
    client = TestClient(app, raise_server_exceptions=False)
    headers = basic_auth_header("tenant", "secret")

    first = _submit(client, fake_opener, headers, ["a.nl"])
    assert first.status_code == 200

    # Second submit: concurrency check refreshes the one non-terminal run
    # (still "registering" per REGISTER_REPLY) and rejects.
    queue_json(fake_opener, STATUS_RUNNING)
    second = _submit(client, fake_opener, headers, ["b.nl"], queue_reply=False)
    assert second.status_code == 429
    assert second.json()["error"]["label"] == "rate-limited"
    assert "1" in second.json()["error"]["msg"]

    # Third submit: the refresh this time reports the run as done, freeing
    # the slot, so the submit goes through.
    queue_json(fake_opener, STATUS_DONE)
    third = _submit(client, fake_opener, headers, ["c.nl"])
    assert third.status_code == 200


def test_domain_exceeding_max_length_is_400_without_touching_upstream(settings_env, tmp_path):
    """Round-1 fix (M6)."""
    from netnl.api import create_app
    from starlette.testclient import TestClient
    from conftest import add_test_credential, basic_auth_header
    from fakes import FakeOpener
    from netnl.settings import load

    env = dict(settings_env)
    env["NETNL_MAX_DOMAIN_LENGTH"] = "10"
    env["NETNL_DB"] = str(tmp_path / "domain-length.sqlite3")
    settings = load(env)
    fake_opener = FakeOpener()
    app = create_app(settings, opener=fake_opener)
    add_test_credential(app, "tenant", "secret")
    client = TestClient(app, raise_server_exceptions=False)
    headers = basic_auth_header("tenant", "secret")

    resp = _submit(client, fake_opener, headers, ["a" * 20 + ".example"], queue_reply=False)
    assert resp.status_code == 400
    assert resp.json()["error"]["label"] == "bad-request"
    assert len(fake_opener.calls) == 0


def test_domain_with_whitespace_is_400_without_touching_upstream(settings_env, tmp_path):
    """Round-1 fix (M6): whitespace could otherwise smuggle a URL/path
    through to the private upstream instance."""
    from netnl.api import create_app
    from starlette.testclient import TestClient
    from conftest import add_test_credential, basic_auth_header
    from fakes import FakeOpener
    from netnl.settings import load

    env = dict(settings_env)
    env["NETNL_DB"] = str(tmp_path / "domain-whitespace.sqlite3")
    settings = load(env)
    fake_opener = FakeOpener()
    app = create_app(settings, opener=fake_opener)
    add_test_credential(app, "tenant", "secret")
    client = TestClient(app, raise_server_exceptions=False)
    headers = basic_auth_header("tenant", "secret")

    resp = _submit(client, fake_opener, headers, ["example.nl and-more"], queue_reply=False)
    assert resp.status_code == 400
    assert resp.json()["error"]["label"] == "bad-request"
    assert len(fake_opener.calls) == 0


def test_domain_with_crlf_is_400_without_touching_upstream(settings_env, tmp_path):
    """Round-1 fix (M6): control characters (CR/LF header/log injection)
    must be rejected before anything is written or forwarded."""
    from netnl.api import create_app
    from starlette.testclient import TestClient
    from conftest import add_test_credential, basic_auth_header
    from fakes import FakeOpener
    from netnl.settings import load

    env = dict(settings_env)
    env["NETNL_DB"] = str(tmp_path / "domain-crlf.sqlite3")
    settings = load(env)
    fake_opener = FakeOpener()
    app = create_app(settings, opener=fake_opener)
    add_test_credential(app, "tenant", "secret")
    client = TestClient(app, raise_server_exceptions=False)
    headers = basic_auth_header("tenant", "secret")

    resp = _submit(client, fake_opener, headers, ["example.nl\r\nX-Injected: 1"], queue_reply=False)
    assert resp.status_code == 400
    assert resp.json()["error"]["label"] == "bad-request"
    assert len(fake_opener.calls) == 0


def test_oversized_request_body_is_400_without_touching_upstream(settings_env, tmp_path):
    """Round-1 fix (M6): a total request-body size cap, enforced before the
    body is parsed."""
    from netnl.api import create_app
    from starlette.testclient import TestClient
    from conftest import add_test_credential, basic_auth_header
    from fakes import FakeOpener
    from netnl.settings import load

    env = dict(settings_env)
    env["NETNL_MAX_BODY_BYTES"] = "1024"
    env["NETNL_DB"] = str(tmp_path / "body-size.sqlite3")
    settings = load(env)
    fake_opener = FakeOpener()
    app = create_app(settings, opener=fake_opener)
    add_test_credential(app, "tenant", "secret")
    client = TestClient(app, raise_server_exceptions=False)
    headers = basic_auth_header("tenant", "secret")

    huge_name = "x" * 5000
    resp = client.post(
        "/requests",
        json={"type": "web", "domains": ["example.nl"], "name": huge_name},
        headers=headers,
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["label"] == "bad-request"
    assert len(fake_opener.calls) == 0
    assert resp.headers["X-Netnl-Instance"] == settings.instance


@pytest.mark.parametrize(
    "domain",
    [
        "localhost",
        "10.0.0.5",
        "127.0.0.1",
        "169.254.169.254",
        "::1",
        "2130706433",
        "0300.0250.0.1",
        "0x7f.0.0.1",
        "foo.localhost",
        "app.localhost",
        "Foo.LOCALHOST",
        "something.local",
        "a.localdomain",
        "x.lan",
        "foo.internal",
        "instance.internal",
        "metadata.google.internal",
        "metadata",
        "db.corp",
        "server.home",
        "foo.localhost.",
    ],
)
def test_internal_or_ip_literal_target_is_400_without_touching_upstream(
    settings_env, tmp_path, domain
):
    """Round-2/3 fix (security-MEDIUM, anti-SSRF, pinned): the facade must
    refuse IP-address literals (every notation), single-label names, names
    under a reserved/internal-use suffix and well-known cloud-metadata
    hostnames — see design.md, "No internal targets (anti-SSRF)" and the
    spec scenario "Internal targets are refused". A trailing dot must not
    be usable to bypass the suffix check."""
    from netnl.api import create_app
    from starlette.testclient import TestClient
    from conftest import add_test_credential, basic_auth_header
    from fakes import FakeOpener
    from netnl.settings import load

    env = dict(settings_env)
    env["NETNL_DB"] = str(tmp_path / "ssrf.sqlite3")
    settings = load(env)
    fake_opener = FakeOpener()
    app = create_app(settings, opener=fake_opener)
    add_test_credential(app, "tenant", "secret")
    client = TestClient(app, raise_server_exceptions=False)
    headers = basic_auth_header("tenant", "secret")

    resp = _submit(client, fake_opener, headers, [domain], queue_reply=False)
    assert resp.status_code == 400
    assert resp.json()["error"]["label"] == "bad-request"
    assert len(fake_opener.calls) == 0


@pytest.mark.parametrize("domain", ["example.nl", "sub.example.co.uk"])
def test_public_multi_label_fqdn_is_accepted(settings_env, tmp_path, domain):
    from netnl.api import create_app
    from starlette.testclient import TestClient
    from conftest import add_test_credential, basic_auth_header
    from fakes import FakeOpener
    from netnl.settings import load

    env = dict(settings_env)
    env["NETNL_DB"] = str(tmp_path / "ssrf-ok.sqlite3")
    settings = load(env)
    fake_opener = FakeOpener()
    app = create_app(settings, opener=fake_opener)
    add_test_credential(app, "tenant", "secret")
    client = TestClient(app, raise_server_exceptions=False)
    headers = basic_auth_header("tenant", "secret")

    resp = _submit(client, fake_opener, headers, [domain])
    assert resp.status_code == 200
    assert len(fake_opener.calls) == 1


def test_provenance_headers_present_on_limit_errors(settings_env, tmp_path):
    from netnl.api import create_app
    from starlette.testclient import TestClient
    from conftest import add_test_credential, basic_auth_header
    from fakes import FakeOpener
    from netnl.settings import load

    env = dict(settings_env)
    env["NETNL_MAX_DOMAINS"] = "1"
    env["NETNL_DB"] = str(tmp_path / "headers.sqlite3")
    settings = load(env)
    fake_opener = FakeOpener()
    app = create_app(settings, opener=fake_opener)
    add_test_credential(app, "tenant", "secret")
    client = TestClient(app, raise_server_exceptions=False)
    headers = basic_auth_header("tenant", "secret")

    resp = _submit(client, fake_opener, headers, ["a.nl", "b.nl"], queue_reply=False)
    assert resp.status_code == 400
    assert resp.headers["X-Netnl-Instance"] == settings.instance
    assert "X-Netnl-Notice" in resp.headers
    body = resp.json()
    assert set(body.keys()) == {"api_version", "error"}
