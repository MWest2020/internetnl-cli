"""Privacy proof for the `/demo/*` family (openspec/changes/add-demo-run,
T6). Distinctive markers are injected as the client IP, the `Origin`
header and the submitted domain, and asserted absent from the raw SQLite
file's bytes and from `caplog` — not inferred from reading the code. A
rejected demo request (any reason) is also asserted to write zero audit
rows.
"""

from __future__ import annotations

import logging
import pathlib

import pytest

from fakes import REGISTER_REPLY, STATUS_RUNNING

from conftest import DEMO_ORIGIN, DEMO_TENANT, add_test_credential, queue_json
from netnl import store

FAKE_IP = "203.0.113.77"
FAKE_ORIGIN = "https://marker-origin.invalid"


def _headers(domain_marker_ip: str | None = FAKE_IP, origin: str = DEMO_ORIGIN) -> dict:
    headers = {"Origin": origin}
    if domain_marker_ip is not None:
        headers["CF-Connecting-IP"] = domain_marker_ip
    return headers


def _db_bytes(app) -> bytes:
    return pathlib.Path(app.state.settings.db).read_bytes()


def _audit_row_count(app) -> int:
    conn = store.connect(app.state.settings.db)
    try:
        return conn.execute("SELECT COUNT(*) AS n FROM audit").fetchone()["n"]
    finally:
        conn.close()


def _assert_no_markers(app, caplog, *markers: str):
    raw = _db_bytes(app)
    for marker in markers:
        assert marker.encode() not in raw, f"{marker!r} leaked into the database file"
        assert marker not in caplog.text, f"{marker!r} leaked into a log line"


@pytest.fixture(autouse=True)
def _capture_all_logs(caplog):
    caplog.set_level(logging.DEBUG)


def test_accepted_run_stores_no_ip_origin_or_domain_markers(demo_app, demo_client, fake_opener, caplog):
    domain = "marker-accepted-xyz.nl"
    queue_json(fake_opener, REGISTER_REPLY)
    resp = demo_client.post("/demo/requests", json={"domain": domain}, headers=_headers())
    assert resp.status_code == 200

    _assert_no_markers(demo_app, caplog, FAKE_IP, domain)


def test_origin_mismatch_rejection_stores_no_markers_and_no_audit_row(
    demo_app, demo_client, fake_opener, caplog
):
    domain = "marker-origin-mismatch.nl"
    before = _audit_row_count(demo_app)

    resp = demo_client.post(
        "/demo/requests", json={"domain": domain}, headers=_headers(origin=FAKE_ORIGIN)
    )
    assert resp.status_code == 403

    assert _audit_row_count(demo_app) == before
    assert len(fake_opener.calls) == 0
    _assert_no_markers(demo_app, caplog, FAKE_IP, FAKE_ORIGIN, domain)


def test_unavailable_rejection_stores_no_markers_and_no_audit_row(
    demo_app, demo_client, fake_opener, caplog
):
    conn = store.connect(demo_app.state.settings.db)
    try:
        store.revoke_credential(conn, DEMO_TENANT, store.utcnow_iso(demo_app.state.now))
    finally:
        conn.close()
    before = _audit_row_count(demo_app)

    domain = "marker-unavailable.nl"
    resp = demo_client.post("/demo/requests", json={"domain": domain}, headers=_headers())
    assert resp.status_code == 503

    assert _audit_row_count(demo_app) == before
    _assert_no_markers(demo_app, caplog, FAKE_IP, domain)


def test_cooldown_rejection_stores_no_ip_marker_and_no_extra_audit_row(
    demo_app, demo_client, fake_opener, caplog
):
    domain = "marker-cooldown.nl"
    queue_json(fake_opener, REGISTER_REPLY)
    first = demo_client.post(
        "/demo/requests", json={"domain": domain}, headers=_headers(domain_marker_ip=None)
    )
    assert first.status_code == 200
    after_accept = _audit_row_count(demo_app)

    second = demo_client.post("/demo/requests", json={"domain": domain}, headers=_headers())
    assert second.status_code == 429

    assert _audit_row_count(demo_app) == after_accept  # the rejection added nothing
    _assert_no_markers(demo_app, caplog, FAKE_IP, domain)


def test_bad_domain_shape_rejection_stores_no_markers_and_no_audit_row(
    demo_app, demo_client, fake_opener, caplog
):
    marker = "marker-bad-shape-xyz"
    bad_domain = f"https://{marker}.nl/path"
    before = _audit_row_count(demo_app)

    resp = demo_client.post("/demo/requests", json={"domain": bad_domain}, headers=_headers())
    assert resp.status_code == 400

    assert _audit_row_count(demo_app) == before
    assert len(fake_opener.calls) == 0
    _assert_no_markers(demo_app, caplog, FAKE_IP, marker)


def test_per_ip_cap_rejection_stores_no_domain_marker_and_no_extra_audit_row(
    settings_env, fake_opener, clock, caplog
):
    from starlette.testclient import TestClient
    from netnl.api import create_app
    from netnl.settings import load

    env = dict(settings_env)
    env["NETNL_DEMO_ENABLED"] = "1"
    env["NETNL_DEMO_ALLOWED_ORIGIN"] = DEMO_ORIGIN
    env["NETNL_DEMO_TENANT"] = DEMO_TENANT
    env["NETNL_DEMO_PER_IP_PER_HOUR"] = "1"
    settings = load(env)
    app = create_app(settings, opener=fake_opener, now=clock)
    add_test_credential(app, DEMO_TENANT, "thrown-away")
    client = TestClient(app, raise_server_exceptions=False)

    queue_json(fake_opener, REGISTER_REPLY)
    first = client.post(
        "/demo/requests", json={"domain": "marker-ipcap-first.nl"}, headers=_headers()
    )
    assert first.status_code == 200
    after_accept = _audit_row_count(app)

    rejected_domain = "marker-ipcap-rejected.nl"
    second = client.post(
        "/demo/requests", json={"domain": rejected_domain}, headers=_headers()
    )
    assert second.status_code == 429

    assert _audit_row_count(app) == after_accept
    _assert_no_markers(app, caplog, FAKE_IP, rejected_domain)


def test_tenant_cap_rejection_stores_no_domain_marker_and_no_extra_audit_row(
    settings_env, fake_opener, clock, caplog
):
    from starlette.testclient import TestClient
    from netnl.api import create_app
    from netnl.settings import load

    env = dict(settings_env)
    env["NETNL_DEMO_ENABLED"] = "1"
    env["NETNL_DEMO_ALLOWED_ORIGIN"] = DEMO_ORIGIN
    env["NETNL_DEMO_TENANT"] = DEMO_TENANT
    env["NETNL_DEMO_MAX_PER_HOUR"] = "1"
    env["NETNL_DEMO_PER_IP_PER_HOUR"] = "100"
    settings = load(env)
    app = create_app(settings, opener=fake_opener, now=clock)
    add_test_credential(app, DEMO_TENANT, "thrown-away")
    client = TestClient(app, raise_server_exceptions=False)

    queue_json(fake_opener, REGISTER_REPLY)
    first = client.post(
        "/demo/requests",
        json={"domain": "marker-tenantcap-first.nl"},
        headers=_headers(domain_marker_ip="198.51.100.1"),
    )
    assert first.status_code == 200
    after_accept = _audit_row_count(app)

    # Builder-review fix (S2=B1): the second submit's own
    # `refresh_stale_non_terminal` call refreshes the first (still
    # non-terminal) row against upstream before its own reservation
    # attempt is rejected by the hourly cap.
    queue_json(fake_opener, STATUS_RUNNING)
    rejected_domain = "marker-tenantcap-rejected.nl"
    second = client.post(
        "/demo/requests",
        json={"domain": rejected_domain},
        headers=_headers(domain_marker_ip="198.51.100.2"),
    )
    assert second.status_code == 429

    assert _audit_row_count(app) == after_accept
    _assert_no_markers(app, caplog, "198.51.100.2", rejected_domain)


# --- reviewer-minor: the generic-500 crash path is also privacy-checked --------


def test_generic_crash_rejection_stores_no_markers(settings_env, caplog):
    from starlette.testclient import TestClient
    from netnl.api import create_app
    from netnl.settings import load

    env = dict(settings_env)
    env["NETNL_DEMO_ENABLED"] = "1"
    env["NETNL_DEMO_ALLOWED_ORIGIN"] = DEMO_ORIGIN
    env["NETNL_DEMO_TENANT"] = DEMO_TENANT
    settings = load(env)

    def crashing_opener(method, url, body, headers, timeout):
        raise RuntimeError("boom")

    app = create_app(settings, opener=crashing_opener)
    add_test_credential(app, DEMO_TENANT, "thrown-away")
    client = TestClient(app, raise_server_exceptions=False)

    domain = "marker-crash-xyz.nl"
    resp = client.post("/demo/requests", json={"domain": domain}, headers=_headers())
    assert resp.status_code == 500
    assert resp.json()["error"]["label"] == "server-error"
    assert domain not in resp.text
    assert FAKE_IP not in resp.text
    _assert_no_markers(app, caplog, FAKE_IP, domain)
