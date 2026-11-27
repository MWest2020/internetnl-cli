"""`netnl-admin bazibo watch` (Mark, 27 Nov 2026: "geen zin in ad hoc
oplossingen"): one alert mail per transaction that qualified for a Bazibo
code and has none delivered after NETNL_BAZIBO_ALERT_AFTER_MINUTES (15),
also for failed, parked, crashed and undeliverable issuance rows. Alert
once; never the buyer's address or the code.
"""

from __future__ import annotations

import io
from datetime import timedelta

import pytest
from starlette.testclient import TestClient

from conftest import post_webhook
from netnl import admin, bazibo, store
from netnl.api import create_app
from netnl.errors import SettingsError
from netnl.settings import load

from test_netnl_bazibo import _reset_rate_limit, bazibo_env, extra_payload  # noqa: F401
from test_netnl_bazibo_donations import DONOR, donation

ALERT_TO = "mark@example.org"


@pytest.fixture
def w_env(bazibo_env) -> dict:
    return dict(bazibo_env, NETNL_BAZIBO_FROM_DONATIONS="1", NETNL_BAZIBO_ALERT_TO=ALERT_TO)


@pytest.fixture
def w_app(w_env, fake_opener, clock, recording_sender):
    return create_app(load(w_env), opener=fake_opener, now=clock, sender=recording_sender)


@pytest.fixture
def w_client(w_app):
    return TestClient(w_app, raise_server_exceptions=False)


@pytest.fixture
def w_conn(w_app):
    c = store.connect(w_app.state.settings.db)
    yield c
    c.close()


def watch(app, conn, now, sender):
    out, err = io.StringIO(), io.StringIO()
    rc = admin._bazibo_watch(conn, app.state.settings, now, out, err, sender=sender)
    return rc, out.getvalue(), err.getvalue()


def alerts(sender):
    return [m for m in sender.sent if m.to == ALERT_TO]


def test_delivered_code_never_alerts(w_app, w_client, w_conn, clock, recording_sender):
    post_webhook(w_client, donation(amount=1.0))
    rc, out, _ = watch(w_app, w_conn, clock() + timedelta(hours=2), recording_sender)
    assert rc == 0 and "alerted=0" in out and alerts(recording_sender) == []


def test_failed_mail_alerts_once_after_15_minutes(w_app, w_client, w_conn, clock, recording_sender):
    recording_sender.fail_on_calls = {0}
    assert post_webhook(w_client, donation(txn="pi_fail", amount=1.0)).status_code == 503
    # within the window: nothing yet (BMC may still retry)
    watch(w_app, w_conn, clock() + timedelta(minutes=14), recording_sender)
    assert alerts(recording_sender) == []
    rc, out, _ = watch(w_app, w_conn, clock() + timedelta(minutes=16), recording_sender)
    assert rc == 0 and "alerted=1" in out
    [m] = alerts(recording_sender)
    assert m.subject == "Bazibo-code niet verstuurd voor transactie pi_fail — controleer"
    assert "failed" in m.body and "--ref pi_fail" in m.body
    assert DONOR not in m.body and DONOR not in m.subject and "BZB1." not in m.body
    # once per transaction
    watch(w_app, w_conn, clock() + timedelta(hours=3), recording_sender)
    assert len(alerts(recording_sender)) == 1


def test_no_issuance_row_at_all_still_alerts(w_env, fake_opener, clock, recording_sender):
    # the hourly cap answers 503 before any issuance row is written
    env = dict(w_env, NETNL_SUPPORTER_MAX_PER_HOUR="1")
    app = create_app(load(env), opener=fake_opener, now=clock, sender=recording_sender)
    client = TestClient(app, raise_server_exceptions=False)
    conn = store.connect(app.state.settings.db)
    try:
        post_webhook(client, donation(txn="t1", amount=1.0))
        assert post_webhook(client, donation(txn="t2", amount=1.0)).status_code == 503
        assert store.find_bazibo_issuance(conn, "t2") is None
        watch(app, conn, clock() + timedelta(minutes=20), recording_sender)
        [m] = alerts(recording_sender)
        assert "t2" in m.subject and "geen uitgifte-rij" in m.body
    finally:
        conn.close()


def test_a_crash_after_persist_alerts(w_app, w_client, w_conn, clock, recording_sender, monkeypatch):
    monkeypatch.setattr(bazibo, "mint", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    post_webhook(w_client, donation(txn="pi_crash", amount=1.0))
    watch(w_app, w_conn, clock() + timedelta(minutes=16), recording_sender)
    [m] = alerts(recording_sender)
    assert "pending" in m.body


def test_undeliverable_and_parked_rows_alert(w_app, w_client, w_conn, clock, recording_sender):
    post_webhook(w_client, donation(txn="pi_nomail", amount=1.0, email_addr=None))
    recording_sender.fail_always = True
    for _ in range(3):
        post_webhook(w_client, extra_payload(txn="pi_parked"))
    recording_sender.fail_always = False
    watch(w_app, w_conn, clock() + timedelta(minutes=16), recording_sender)
    subjects = sorted(m.subject for m in alerts(recording_sender))
    assert len(subjects) == 2
    assert any("pi_nomail" in s for s in subjects) and any("pi_parked" in s for s in subjects)


def test_an_alert_that_fails_to_send_is_retried(w_app, w_client, w_conn, clock, recording_sender):
    recording_sender.fail_on_calls = {0, 1}  # the code mail, then the first alert
    post_webhook(w_client, donation(txn="pi_r", amount=1.0))
    rc, out, err = watch(w_app, w_conn, clock() + timedelta(minutes=16), recording_sender)
    assert rc == 1 and "failed=1" in out and "DeliveryError" in err
    rc, out, _ = watch(w_app, w_conn, clock() + timedelta(minutes=21), recording_sender)
    assert rc == 0 and "alerted=1" in out


def test_test_mode_and_below_minimum_are_not_watched(w_app, w_client, w_conn, clock, recording_sender):
    post_webhook(w_client, donation(txn="a", amount=5.0, live=False))
    post_webhook(w_client, donation(txn="b", amount=0.5))
    watch(w_app, w_conn, clock() + timedelta(hours=1), recording_sender)
    assert alerts(recording_sender) == []


def test_a_hostile_transaction_id_cannot_reach_a_header(w_app, w_conn, clock, recording_sender):
    store.watch_bazibo(w_conn, "pi_x\r\nBcc: evil@example.org", store.utcnow_iso(clock))
    watch(w_app, w_conn, clock() + timedelta(minutes=16), recording_sender)
    [m] = alerts(recording_sender)
    assert "\r" not in m.subject and "\n" not in m.subject and "@" not in m.subject
    assert m.subject.startswith("Bazibo-code niet verstuurd voor transactie pi_x??Bcc:?evil?example.org")


def test_off_without_a_recipient(bazibo_env, fake_opener, clock, recording_sender):
    app = create_app(load(bazibo_env), opener=fake_opener, now=clock, sender=recording_sender)
    conn = store.connect(app.state.settings.db)
    try:
        rc, out, _ = watch(app, conn, clock(), recording_sender)
        assert rc == 0 and "watch off" in out
    finally:
        conn.close()


@pytest.mark.parametrize("value", ["not-an-address", "a@b.c, d@e.f", "x@example.org\r\nBcc: y@z"])
def test_a_bad_recipient_is_refused(w_env, value):
    with pytest.raises(SettingsError):
        load(dict(w_env, NETNL_BAZIBO_ALERT_TO=value))


def test_window_is_configurable(w_env, fake_opener, clock, recording_sender):
    env = dict(w_env, NETNL_BAZIBO_ALERT_AFTER_MINUTES="60")
    app = create_app(load(env), opener=fake_opener, now=clock, sender=recording_sender)
    client = TestClient(app, raise_server_exceptions=False)
    conn = store.connect(app.state.settings.db)
    try:
        recording_sender.fail_on_calls = {0}
        post_webhook(client, donation(txn="w", amount=1.0))
        watch(app, conn, clock() + timedelta(minutes=30), recording_sender)
        assert alerts(recording_sender) == []
        watch(app, conn, clock() + timedelta(minutes=61), recording_sender)
        assert len(alerts(recording_sender)) == 1
    finally:
        conn.close()


def test_watch_table_holds_no_email(w_conn):
    cols = [r["name"] for r in w_conn.execute("PRAGMA table_info(bazibo_watch)")]
    assert cols == ["txn_id", "seen_at", "alerted_at"]


def test_cli_entry_point(w_env, capsys):
    rc = admin.main(["bazibo", "watch"], env=dict(w_env, NETNL_BAZIBO_ALERT_TO=""))
    assert rc == 0
