"""Bazibo Extra purchases that arrive as "support created"
(`donation.created`) deliveries, and the BMC API poller
(openspec/changes/add-bazibo-support-created).
"""

from __future__ import annotations

import io
import json
import logging
from datetime import timedelta

import pytest
from starlette.testclient import TestClient

from conftest import post_webhook
from internetnl_cli.client import HttpResponse
from netnl import admin, bazibo, store
from netnl.api import create_app
from netnl.errors import SettingsError
from netnl.settings import load

from test_netnl_bazibo import (  # noqa: F401  (fixtures re-used by name)
    BUYER,
    TEST_PUB,
    TEST_SEED_B64,
    DEVICE_SALT,
    _code_from_mail,
    _counts,
    _db_dump,
)

EXTRA_ID = "603413"
TITLE = "Bazibo full version unlock!"
TOKEN = "bmc-test-token-abcdef0123456789"
API = "https://developers.buymeacoffee.com/api/v1"


@pytest.fixture
def env(supporter_env) -> dict:
    e = dict(supporter_env)
    e.update({
        "NETNL_BAZIBO_SIGNING_KEY": TEST_SEED_B64,
        "NETNL_BAZIBO_EXTRA_ID": EXTRA_ID,
        "NETNL_BAZIBO_DEVICE_SALT": DEVICE_SALT,
        "NETNL_BMC_API_TOKEN": TOKEN,
    })
    return e


@pytest.fixture
def app(env, fake_opener, clock, recording_sender):
    return create_app(load(env), opener=fake_opener, now=clock, sender=recording_sender)


@pytest.fixture
def client(app):
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def db(app):
    c = store.connect(app.state.settings.db)
    yield c
    c.close()


def support_created(*, purchase_id=777, txn="pi_sc_1", support_type="Extra", extras=None,
                    amount=1.0, note=None, email=BUYER):
    """A `donation.created` ("support created") delivery for an Extra purchase,
    with the ExtraData fields from BMC's published webhook spec."""
    data = {"id": purchase_id, "object": "payment", "transaction_id": txn, "status": "succeeded",
            "amount": amount, "currency": "EUR", "supporter_email": email,
            "support_note": note, "message": note or ""}
    if support_type is not None:
        data["support_type"] = support_type
    if extras is not None:
        data["extras"] = extras
    return {"event_id": 1, "type": "donation.created", "live_mode": True, "created": 1,
            "attempt": 1, "data": data}


def bazibo_item(**over):
    item = {"id": int(EXTRA_ID), "object": "extra", "title": TITLE, "amount": 1.0, "quantity": 1,
            "currency": "EUR"}
    item.update(over)
    return item


# --- routing of "support created" -------------------------------------------------------


def test_support_created_extra_mints_a_code_and_no_credential(client, db, recording_sender):
    resp = post_webhook(client, support_created(extras=[bazibo_item()]))
    assert resp.json() == {"status": "ok"}
    counts = _counts(db)
    assert counts["credentials"] == 0 and counts["supporter_issuance"] == 0
    assert counts["bazibo_issuance"] == 1
    assert bazibo.verify(TEST_PUB, _code_from_mail(recording_sender.sent[0])) is not None
    assert store.find_bazibo_issuance(db, "pi_sc_1")["state"] == "delivered"


def test_extras_without_support_type_still_counts_as_extra(client, db, recording_sender):
    assert post_webhook(client, support_created(support_type=None, extras=[bazibo_item()])).json() == {"status": "ok"}
    assert _counts(db)["credentials"] == 0 and len(recording_sender.sent) == 1


def test_title_match_on_support_created(env, fake_opener, clock, recording_sender):
    e = dict(env)
    e.pop("NETNL_BAZIBO_EXTRA_ID")
    e["NETNL_BAZIBO_EXTRA_TITLE"] = TITLE
    c = TestClient(create_app(load(e), opener=fake_opener, now=clock, sender=recording_sender),
                   raise_server_exceptions=False)
    assert post_webhook(c, support_created(extras=[bazibo_item(id=1)])).json() == {"status": "ok"}


@pytest.mark.parametrize("payload", [
    support_created(extras=None, amount=5.0),                        # Extra marker, no cart
    support_created(extras=[], amount=5.0),                          # Extra marker, empty cart
    support_created(extras=[bazibo_item(id=1, title="Sticker")], amount=5.0),  # another Extra
    support_created(support_type="extra", extras=[{"title": "x"}], amount=5.0),
])
def test_unidentified_or_other_extra_never_mints_anything(client, db, recording_sender, payload):
    resp = post_webhook(client, payload)
    assert resp.status_code == 200 and resp.json() == {"status": "ignored"}
    assert _counts(db) == {"credentials": 0, "supporter_issuance": 0, "bazibo_issuance": 0, "bazibo_device": 0}
    assert recording_sender.sent == []


def test_plain_donation_mentioning_bazibo_keeps_the_donation_path(client, db, recording_sender):
    payload = support_created(support_type="Supporter", extras=None, amount=5.0,
                              note="Bazibo full version unlock! 603413 extras Extra")
    assert post_webhook(client, payload).json() == {"status": "ok"}
    counts = _counts(db)
    assert counts["credentials"] == 1 and counts["bazibo_issuance"] == 0
    assert "BZB1." not in recording_sender.sent[0].body


def test_plain_donation_with_empty_extras_list_keeps_the_donation_path(client, db):
    post_webhook(client, support_created(support_type="Supporter", extras=[], amount=5.0))
    assert _counts(db)["credentials"] == 1


def test_extra_marked_support_created_is_ignored_while_bazibo_is_dormant(
    env, fake_opener, clock, recording_sender
):
    e = dict(env)
    e.pop("NETNL_BAZIBO_EXTRA_ID")
    c = TestClient(create_app(load(e), opener=fake_opener, now=clock, sender=recording_sender),
                   raise_server_exceptions=False)
    resp = post_webhook(c, support_created(extras=[bazibo_item()], amount=5.0))
    assert resp.json() == {"status": "ignored"}
    conn = store.connect(load(e).db)
    assert _counts(conn)["credentials"] == 0
    conn.close()
    assert recording_sender.sent == []


def test_both_webhook_events_for_one_purchase_mail_once(client, db, recording_sender):
    from test_netnl_bazibo import extra_payload

    p1 = extra_payload(extra_id=int(EXTRA_ID), purchase_id=777, txn="pi_sc_1")
    p2 = support_created(extras=[bazibo_item()], purchase_id=777, txn="pi_sc_1")
    assert post_webhook(client, p1).json() == {"status": "ok"}
    assert post_webhook(client, p2).json() == {"status": "ok"}
    assert len(recording_sender.sent) == 1 and _counts(db)["bazibo_issuance"] == 1


def api_item(purchase_id=777, *, reward_id=int(EXTRA_ID), title=TITLE, amount="1.00",
             email=BUYER, revoked=0, when="2026-01-01 00:00:00"):
    return {"purchase_id": purchase_id, "purchased_on": when, "purchase_updated_on": when,
            "purchase_is_revoked": revoked, "purchase_amount": amount, "purchase_currency": "EUR",
            "purchase_question": "", "payer_email": email, "payer_name": "Alex",
            "total_paid_amount": amount,
            "extra": {"reward_id": reward_id, "reward_title": title, "reward_coffee_price": amount}}


class FakeApi:
    """Records every request; answers pages from a dict, or a fixed status."""

    def __init__(self, pages: dict[int, list], *, status: int = 200, next_url="https://evil.example/x"):
        self.pages = pages
        self.status = status
        self.next_url = next_url
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, method, url, body, headers, timeout):
        self.calls.append((url, dict(headers)))
        if self.status != 200:
            return HttpResponse(status=self.status, body=b'{"error": "nope"}')
        page = int(url.rsplit("page=", 1)[1])
        last = max(self.pages) if self.pages else 1
        body = {"current_page": page, "data": self.pages.get(page, []), "last_page": last,
                "next_page_url": self.next_url if page < last else None}
        return HttpResponse(status=200, body=json.dumps(body).encode())


def run_poll(env, clock, sender, api) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    settings = load(env)
    code = admin._bazibo_poll(settings, clock(), out, err, opener=api, sender=sender)
    return code, out.getvalue(), err.getvalue()


def test_poll_mints_once_per_purchase(env, app, clock, recording_sender, db):
    api = FakeApi({1: [api_item(777), api_item(778, reward_id=5, title="Sticker")]})
    code, out, _ = run_poll(env, clock, recording_sender, api)
    assert code == 0 and "issued=1" in out and "matched=1" in out
    assert len(recording_sender.sent) == 1 and recording_sender.sent[0].to == BUYER
    assert store.find_bazibo_issuance(db, "bmc-extra:777")["state"] == "delivered"
    code, out, _ = run_poll(env, clock, recording_sender, api)
    assert "duplicate=1" in out and len(recording_sender.sent) == 1
    assert _counts(db)["credentials"] == 0


@pytest.mark.parametrize("item", [
    api_item(revoked=1),
    api_item(when="2025-12-01 00:00:00"),                 # older than the 168h lookback
    api_item(reward_id=1, title="Something else"),
    {"purchase_id": "x"},
    "not a dict",
])
def test_poll_skips_what_it_must_not_mail(env, app, clock, recording_sender, db, item):
    code, _, _ = run_poll(env, clock, recording_sender, FakeApi({1: [item]}))
    assert code == 0 and recording_sender.sent == [] and _counts(db)["bazibo_issuance"] == 0


def test_poll_respects_the_bazibo_minimum(env, app, clock, recording_sender):
    e = dict(env, NETNL_BAZIBO_MIN_AMOUNT="1")
    code, out, _ = run_poll(e, clock, recording_sender, FakeApi({1: [api_item(amount="0.50")]}))
    assert "ignored=1" in out and recording_sender.sent == []


def test_poll_without_email_records_undeliverable(env, app, clock, recording_sender, db):
    run_poll(env, clock, recording_sender, FakeApi({1: [api_item(email=None)]}))
    assert store.find_bazibo_issuance(db, "bmc-extra:777")["state"] == "undeliverable"
    assert recording_sender.sent == []


def test_poll_pages_by_number_and_sends_the_token_only_to_the_api_base(env, app, clock, recording_sender):
    api = FakeApi({1: [api_item(1)], 2: [api_item(2)], 3: [api_item(3)]})
    run_poll(env, clock, recording_sender, api)
    assert [u for u, _ in api.calls] == [f"{API}/extras?page={n}" for n in (1, 2, 3)]
    assert all(h["Authorization"] == f"Bearer {TOKEN}" for _, h in api.calls)
    assert len(recording_sender.sent) == 3


def test_poll_stops_at_max_pages(env, app, clock, recording_sender):
    e = dict(env, NETNL_BMC_POLL_MAX_PAGES="2")
    api = FakeApi({n: [api_item(n)] for n in range(1, 6)})
    run_poll(e, clock, recording_sender, api)
    assert len(api.calls) == 2


def test_poll_api_error_exits_1_and_never_shows_the_token(env, app, clock, recording_sender, db, caplog):
    caplog.set_level(logging.DEBUG)
    code, out, err = run_poll(env, clock, recording_sender, FakeApi({}, status=401))
    assert code == 1 and "401" in err
    assert TOKEN not in err + out + caplog.text
    assert _counts(db)["bazibo_issuance"] == 0


def test_poll_mail_failure_is_retried_with_the_same_code(env, app, clock, recording_sender, db):
    recording_sender.fail_on_calls = {0}
    api = FakeApi({1: [api_item(777)]})
    _, out, _ = run_poll(env, clock, recording_sender, api)
    assert "failed=1" in out
    assert store.find_bazibo_issuance(db, "bmc-extra:777")["state"] == "failed"
    clock.advance(600)
    _, out, _ = run_poll(env, clock, recording_sender, api)
    assert "issued=1" in out
    assert _code_from_mail(recording_sender.sent[0]) == _code_from_mail(recording_sender.sent[1])


def test_poll_is_off_without_token_or_without_bazibo(env, app, clock, recording_sender):
    e = dict(env)
    e.pop("NETNL_BMC_API_TOKEN")
    api = FakeApi({1: [api_item()]})
    code, out, _ = run_poll(e, clock, recording_sender, api)
    assert code == 0 and "NETNL_BMC_API_TOKEN" in out and api.calls == []
    e = dict(env)
    e.pop("NETNL_BAZIBO_EXTRA_ID")
    code, out, _ = run_poll(e, clock, recording_sender, api)
    assert code == 0 and "not enabled" in out and api.calls == []


def test_poll_logs_and_db_hold_no_token_or_email(env, app, clock, recording_sender, caplog):
    caplog.set_level(logging.DEBUG)
    run_poll(env, clock, recording_sender, FakeApi({1: [api_item(777)]}))
    dump = _db_dump(load(env).db)
    for secret in (TOKEN, BUYER, "Alex"):
        assert secret not in caplog.text and secret not in dump


@pytest.mark.parametrize("over, needle", [
    ({"NETNL_BMC_API_TOKEN": "has space"}, "NETNL_BMC_API_TOKEN"),
    ({"NETNL_BMC_API_BASE": "http://developers.buymeacoffee.com/api/v1"}, "NETNL_BMC_API_BASE"),
    ({"NETNL_BMC_POLL_MAX_PAGES": "0"}, "NETNL_BMC_POLL_MAX_PAGES"),
])
def test_poll_settings_refused(env, over, needle):
    with pytest.raises(SettingsError) as exc:
        load(dict(env, **over))
    assert needle in str(exc.value) and "has space" not in str(exc.value)


def test_settings_repr_hides_the_token(env):
    assert TOKEN not in repr(load(env))


def test_poll_lookback_uses_the_run_time(env, app, clock, recording_sender):
    clock.advance(timedelta(days=30).total_seconds())  # clock now 2026-01-31
    _, out, _ = run_poll(env, clock, recording_sender, FakeApi({1: [api_item(when="2026-01-01 00:00:00")]}))
    assert "matched=0" in out


# --- shape logging and manual issue ---------------------------------------------------------


def test_support_created_shape_is_logged_without_personal_data(client, caplog):
    caplog.set_level(logging.INFO, logger="netnl")
    post_webhook(client, support_created(extras=[bazibo_item(), bazibo_item(id=5, title="Sticker")],
                                         note="Groetjes van Alex"))
    line = next(r.getMessage() for r in caplog.records if "event=bmc-shape" in r.getMessage())
    assert "type=donation.created" in line and "support_type=Extra" in line
    assert "extras=list(2)" in line and f"extra_ids={EXTRA_ID},5" in line
    for secret in (BUYER, "Alex", "Groetjes", TITLE, "Sticker", "1.0", "EUR"):
        assert secret not in caplog.text


def test_plain_donation_shape_is_logged_too(client, caplog):
    caplog.set_level(logging.INFO, logger="netnl")
    post_webhook(client, support_created(support_type=None, extras=None, amount=5.0))
    assert "support_type=absent extras=absent extra_ids=-" in caplog.text


def test_odd_support_type_is_not_echoed(client, caplog):
    caplog.set_level(logging.INFO, logger="netnl")
    post_webhook(client, support_created(support_type="mail me at x@y.z", extras=None, amount=5.0))
    assert "x@y.z" not in caplog.text and "support_type=other" in caplog.text


def _mint_mail(env, clock, sender, *args):
    out, err = io.StringIO(), io.StringIO()
    ns = admin._build_parser().parse_args(["bazibo", "mint", *args])
    code = admin._bazibo_mint_mail(load(env), ns, clock(), out, err, sender=sender)
    return code, out.getvalue(), err.getvalue()


def test_manual_mint_mails_once_per_ref(env, app, clock, recording_sender, db):
    code, out, _ = _mint_mail(env, clock, recording_sender, "--mail", BUYER, "--ref", "pi_manual_1")
    assert code == 0 and "mailed" in out and BUYER not in out
    assert len(recording_sender.sent) == 1 and recording_sender.sent[0].to == BUYER
    code_text = _code_from_mail(recording_sender.sent[0])
    assert code_text not in out
    assert store.find_bazibo_issuance(db, "pi_manual_1")["state"] == "delivered"
    code, out, _ = _mint_mail(env, clock, recording_sender, "--mail", BUYER, "--ref", "pi_manual_1")
    assert code == 0 and "already delivered" in out and len(recording_sender.sent) == 1


def test_manual_mint_then_webhook_for_the_same_transaction_sends_once(env, client, clock, recording_sender):
    _mint_mail(env, clock, recording_sender, "--mail", BUYER, "--ref", "pi_sc_1")
    assert post_webhook(client, support_created(extras=[bazibo_item()], txn="pi_sc_1")).json() == {"status": "ok"}
    assert len(recording_sender.sent) == 1


def test_webhook_then_manual_mint_sends_once(env, client, clock, recording_sender):
    post_webhook(client, support_created(extras=[bazibo_item()], txn="pi_sc_1"))
    code, out, _ = _mint_mail(env, clock, recording_sender, "--mail", BUYER, "--ref", "pi_sc_1")
    assert "already delivered" in out and len(recording_sender.sent) == 1


def test_manual_mint_takes_over_an_undeliverable_webhook_row(env, client, clock, recording_sender, db):
    post_webhook(client, support_created(extras=[bazibo_item()], txn="pi_sc_1", email=None))
    assert store.find_bazibo_issuance(db, "pi_sc_1")["state"] == "undeliverable"
    code, _, _ = _mint_mail(env, clock, recording_sender, "--mail", BUYER, "--ref", "pi_sc_1")
    assert code == 0 and len(recording_sender.sent) == 1
    row = store.find_bazibo_issuance(db, "pi_sc_1")
    assert row["state"] == "delivered" and len(row["code_id"]) == 20
    assert bazibo.verify(TEST_PUB, _code_from_mail(recording_sender.sent[0])).code_id_hex == row["code_id"]


def test_manual_mint_failure_then_retry_sends_the_same_code(env, app, clock, recording_sender):
    recording_sender.fail_on_calls = {0}
    code, _, err = _mint_mail(env, clock, recording_sender, "--mail", BUYER, "--ref", "pi_m2")
    assert code == 1 and "again" in err
    code, _, _ = _mint_mail(env, clock, recording_sender, "--mail", BUYER, "--ref", "pi_m2")
    assert code == 0
    assert _code_from_mail(recording_sender.sent[0]) == _code_from_mail(recording_sender.sent[1])


@pytest.mark.parametrize("args, needle", [
    (["--mail", BUYER], "together"),
    (["--ref", "pi_x"], "together"),
    (["--mail", "not an address", "--ref", "pi_x"], "--mail"),
    (["--mail", BUYER, "--ref", "has space"], "--ref"),
])
def test_manual_mint_refuses_bad_input(env, app, clock, recording_sender, args, needle):
    code, _, err = _mint_mail(env, clock, recording_sender, *args)
    assert code == 1 and needle in err and recording_sender.sent == []


def test_manual_mint_works_while_dormant(env, fake_opener, clock, recording_sender):
    e = dict(env)
    e.pop("NETNL_BAZIBO_EXTRA_ID")
    create_app(load(e), opener=fake_opener, now=clock, sender=recording_sender)  # migrates the DB
    code, _, _ = _mint_mail(e, clock, recording_sender, "--mail", BUYER, "--ref", "pi_dormant")
    assert code == 0 and len(recording_sender.sent) == 1


def test_manual_mint_through_main_reaches_the_mail_path(env, monkeypatch, recording_sender):
    from netnl import mail as mail_mod

    monkeypatch.setattr(mail_mod, "smtp_sender", lambda **k: recording_sender)
    out = io.StringIO()
    assert admin.main(["bazibo", "mint", "--mail", BUYER, "--ref", "pi_main"], stdout=out, env=env) == 0
    assert "mailed: ref pi_main" in out.getvalue() and len(recording_sender.sent) == 1
