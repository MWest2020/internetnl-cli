"""`netnl-stripe-setup`: idempotent creation of the Stripe objects, against a
fake Stripe API (no network)."""

from __future__ import annotations

import io

from netnl import stripe_setup


class FakeStripe:
    def __init__(self, live=False):
        self.objects: dict[str, list[dict]] = {k: [] for k in ("products", "prices", "payment_links", "webhook_endpoints")}
        self.calls: list[tuple[str, str]] = []
        self.live = live
        self.n = 0

    def __call__(self, method, path, params):
        self.calls.append((method, path))
        p = dict(params)
        kind = path.split("/")[2]
        if method == "GET":
            data = [o for o in self.objects[kind] if o.get("active", True)]
            if kind == "prices":
                data = [o for o in data if o["product"] == p.get("product")]
            return {"data": data, "has_more": False}
        if method == "DELETE":
            oid = path.split("/")[3]
            self.objects[kind] = [o for o in self.objects[kind] if o["id"] != oid]
            return {"id": oid, "deleted": True}
        self.n += 1
        meta = {k[9:-1]: v for k, v in p.items() if k.startswith("metadata[")}
        prefix = {"products": "prod", "prices": "price", "payment_links": "plink", "webhook_endpoints": "we"}[kind]
        obj = {"id": f"{prefix}_{self.n}", "metadata": meta, "livemode": self.live, "active": True}
        if kind == "prices":
            obj.update(product=p["product"], unit_amount=int(p["unit_amount"]), currency=p["currency"], type="one_time")
        if kind == "payment_links":
            obj.update(url=f"https://buy.stripe.com/test_{self.n}", params=p)
        if kind == "webhook_endpoints":
            obj.update(url=p["url"], secret=f"whsec_new{self.n}", api_version=p.get("api_version"),
                       events=[v for k, v in p.items() if k.startswith("enabled_events")])
        self.objects[kind].append(obj)
        return obj


def run(fake, *args):
    out, err = io.StringIO(), io.StringIO()
    rc = stripe_setup.main(list(args), stdout=out, stderr=err, requester=fake)
    return rc, out.getvalue(), err.getvalue()


def test_first_run_creates_all_four_and_prints_what_the_secret_needs():
    fake = FakeStripe()
    rc, out, _ = run(fake)
    assert rc == 0
    assert "created: product, price, payment link, webhook endpoint" in out
    assert "payment link: https://buy.stripe.com/test_3" in out
    assert "NETNL_STRIPE_PAYMENT_LINK_ID=plink_3" in out
    assert "NETNL_STRIPE_WEBHOOK_SECRET=whsec_new4" in out
    assert "mode: test" in out and "NETNL_STRIPE_ALLOW_TEST_MODE=1" in out
    [product] = fake.objects["products"]
    assert product["metadata"] == {"bazibo": "web-unlock"}
    [price] = fake.objects["prices"]
    assert price["unit_amount"] == 100 and price["currency"] == "eur"
    [link] = fake.objects["payment_links"]
    assert link["params"]["after_completion[hosted_confirmation][custom_message]"] == stripe_setup.CONFIRMATION
    assert link["params"]["line_items[0][price]"] == price["id"]
    [hook] = fake.objects["webhook_endpoints"]
    assert hook["url"] == "https://api.westerweel.work/webhooks/stripe"
    assert hook["events"] == ["checkout.session.completed", "checkout.session.async_payment_succeeded"]
    assert hook["api_version"] == "2024-06-20"


def test_second_run_reuses_everything_and_prints_no_secret():
    fake = FakeStripe()
    run(fake)
    posts = sum(1 for m, _ in fake.calls if m == "POST")
    rc, out, _ = run(fake)
    assert rc == 0 and "created: nothing" in out
    assert sum(1 for m, _ in fake.calls if m == "POST") == posts
    assert "whsec_" not in out and "NETNL_STRIPE_PAYMENT_LINK_ID=plink_3" in out


def test_new_webhook_secret_recreates_only_the_endpoint():
    fake = FakeStripe()
    run(fake)
    rc, out, _ = run(fake, "--new-webhook-secret")
    assert rc == 0 and "created: webhook endpoint" in out and "NETNL_STRIPE_WEBHOOK_SECRET=whsec_new5" in out
    assert len(fake.objects["webhook_endpoints"]) == 1
    assert len(fake.objects["products"]) == 1 and len(fake.objects["payment_links"]) == 1


def test_untagged_objects_of_the_account_are_left_alone():
    fake = FakeStripe()
    fake.objects["products"].append({"id": "prod_other", "metadata": {}, "active": True})
    fake.objects["payment_links"].append({"id": "plink_other", "metadata": {}, "active": True})
    run(fake)
    assert len(fake.objects["products"]) == 2 and len(fake.objects["payment_links"]) == 2


def test_an_untagged_endpoint_on_the_same_url_is_not_duplicated():
    fake = FakeStripe()
    fake.objects["webhook_endpoints"].append(
        {"id": "we_hand", "url": stripe_setup.ENDPOINT, "metadata": {}, "active": True})
    rc, _, err = run(fake)
    assert rc == 1 and "we_hand" in err
    assert len(fake.objects["webhook_endpoints"]) == 1


def test_new_secret_creates_before_it_deletes():
    fake = FakeStripe()
    run(fake)
    run(fake, "--new-webhook-secret")
    hook_calls = [(m, p) for m, p in fake.calls if "webhook_endpoints" in p and m != "GET"]
    assert [m for m, _ in hook_calls] == ["POST", "POST", "DELETE"]


def test_live_mode_is_said_plainly():
    rc, out, _ = run(FakeStripe(live=True))
    assert "mode: LIVE" in out and "ALLOW_TEST_MODE" not in out


def test_no_key_no_requests(capsys):
    err = io.StringIO()
    assert stripe_setup.main([], stdout=io.StringIO(), stderr=err, env={}) == 1
    assert "STRIPE_API_KEY" in err.getvalue()
    assert stripe_setup.main([], stdout=io.StringIO(), stderr=err, env={"STRIPE_API_KEY": "pk_live_x"}) == 1


def test_a_stripe_error_is_reported_without_the_key():
    def failing(method, path, params):
        raise stripe_setup.StripeError("Stripe answered 403 on GET /v1/products: no permission")
    rc, _, err = run(failing)
    assert rc == 1 and "403" in err and "rk_" not in err
