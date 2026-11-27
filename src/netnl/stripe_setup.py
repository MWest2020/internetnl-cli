"""`netnl-stripe-setup`: create (or find) everything Stripe needs for the
Bazibo web unlock (openspec/changes/add-bazibo-unlock D12), through the
Stripe API, with a restricted key in `STRIPE_API_KEY`:

- the Product "Bazibo volledige versie (web)";
- a one-time Price of EUR 1.00;
- a Payment Link for it, with a confirmation message (Checkout always asks
  for the buyer's email address);
- a Webhook Endpoint at https://api.westerweel.work/webhooks/stripe for
  `checkout.session.completed` and `checkout.session.async_payment_succeeded`.

Idempotent: every object carries the metadata `bazibo=web-unlock`, and an
object that exists is reused. Stripe shows a webhook endpoint's secret only
when it is created; for an endpoint that exists the secret is not printed
(`--new-webhook-secret` deletes and recreates it).

Prints the payment link's URL, `NETNL_STRIPE_PAYMENT_LINK_ID` and, for a new
endpoint, `NETNL_STRIPE_WEBHOOK_SECRET`, for the operator to put in the
`netnl-bridge` Secret. Nothing else is printed or logged: never the key, and
the secret only in that one line.

Restricted key permissions: Products, Prices, Payment Links and Webhook
Endpoints, each "Write". Test mode first: a test key makes test objects.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import IO, Callable

API = "https://api.stripe.com"
TAG_KEY, TAG_VALUE = "bazibo", "web-unlock"
PRODUCT_NAME = "Bazibo volledige versie (web)"
UNIT_AMOUNT, CURRENCY = 100, "eur"
CONFIRMATION = "Bedankt! Je code komt binnen een paar minuten per mail. / Thanks! Your code arrives by email within minutes."
ENDPOINT = "https://api.westerweel.work/webhooks/stripe"
EVENTS = ("checkout.session.completed", "checkout.session.async_payment_succeeded")

Requester = Callable[[str, str, list[tuple[str, str]]], dict]


class StripeError(Exception):
    pass


def urllib_requester(key: str, timeout: int = 30) -> Requester:
    """`(method, path, params) -> JSON`; form-encoded, as Stripe's API wants."""

    def request(method: str, path: str, params: list[tuple[str, str]]) -> dict:
        data = urllib.parse.urlencode(params).encode() if method == "POST" else None
        url = API + path + (("?" + urllib.parse.urlencode(params)) if method == "GET" and params else "")
        req = urllib.request.Request(url, data=data, method=method, headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/x-www-form-urlencoded",
            "Stripe-Version": "2024-06-20",
        })
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - fixed https host
                return json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            try:
                message = json.loads(exc.read()).get("error", {}).get("message", "")
            except Exception:  # noqa: BLE001
                message = ""
            raise StripeError(f"Stripe answered {exc.code} on {method} {path}: {message[:200]}") from None
        except urllib.error.URLError as exc:
            raise StripeError(f"Stripe not reachable ({type(exc.reason).__name__})") from None

    return request


def _tagged(obj: dict) -> bool:
    return (obj.get("metadata") or {}).get(TAG_KEY) == TAG_VALUE


def _list(request: Requester, path: str, params: list[tuple[str, str]]) -> list[dict]:
    out: list[dict] = []
    after = None
    for _ in range(20):  # at most 2000 objects
        page = request("GET", path, params + [("limit", "100")] + ([("starting_after", after)] if after else []))
        data = page.get("data") or []
        out.extend(data)
        if not page.get("has_more") or not data:
            break
        after = data[-1]["id"]
    return out


def ensure(request: Requester, *, endpoint: str = ENDPOINT, new_secret: bool = False) -> dict:
    """Find or create the four objects. Returns what to print."""
    tag = [(f"metadata[{TAG_KEY}]", TAG_VALUE)]
    made: list[str] = []

    product = next((p for p in _list(request, "/v1/products", [("active", "true")]) if _tagged(p)), None)
    if product is None:
        product = request("POST", "/v1/products", [("name", PRODUCT_NAME)] + tag)
        made.append("product")

    price = next((
        p for p in _list(request, "/v1/prices", [("product", product["id"]), ("active", "true")])
        if p.get("unit_amount") == UNIT_AMOUNT and p.get("currency") == CURRENCY and p.get("type") == "one_time"
    ), None)
    if price is None:
        price = request("POST", "/v1/prices", [
            ("product", product["id"]), ("unit_amount", str(UNIT_AMOUNT)), ("currency", CURRENCY),
        ] + tag)
        made.append("price")

    link = next((pl for pl in _list(request, "/v1/payment_links", [("active", "true")]) if _tagged(pl)), None)
    if link is None:
        link = request("POST", "/v1/payment_links", [
            ("line_items[0][price]", price["id"]), ("line_items[0][quantity]", "1"),
            ("after_completion[type]", "hosted_confirmation"),
            ("after_completion[hosted_confirmation][custom_message]", CONFIRMATION),
        ] + tag)
        made.append("payment link")

    secret = None
    hooks = [h for h in _list(request, "/v1/webhook_endpoints", []) if h.get("url") == endpoint and _tagged(h)]
    if hooks and new_secret:
        for h in hooks:
            request("DELETE", f"/v1/webhook_endpoints/{h['id']}", [])
        hooks = []
    if not hooks:
        hook = request("POST", "/v1/webhook_endpoints", [("url", endpoint)] + [
            (f"enabled_events[{i}]", e) for i, e in enumerate(EVENTS)
        ] + tag)
        secret = hook.get("secret")
        made.append("webhook endpoint")

    return {
        "made": made,
        "livemode": bool(link.get("livemode")),
        "url": link.get("url"),
        "link_id": link["id"],
        "secret": secret,
    }


def main(argv: list[str] | None = None, *, stdout: IO[str] | None = None, stderr: IO[str] | None = None,
         env=None, requester: Requester | None = None) -> int:
    stdout = stdout or sys.stdout
    stderr = stderr or sys.stderr
    env = os.environ if env is None else env
    parser = argparse.ArgumentParser(prog="netnl-stripe-setup", description=__doc__.split("\n\n")[0])
    parser.add_argument("--endpoint", default=ENDPOINT, help=f"webhook URL (default {ENDPOINT})")
    parser.add_argument("--new-webhook-secret", action="store_true",
                        help="delete and recreate the webhook endpoint, to get a new secret")
    args = parser.parse_args(argv)
    if not args.endpoint.startswith("https://"):
        print("error: --endpoint must be an https:// URL", file=stderr)
        return 1
    if requester is None:
        key = (env.get("STRIPE_API_KEY") or "").strip()
        if not key.startswith(("rk_", "sk_")):
            print("error: set STRIPE_API_KEY to a restricted key (rk_test_... first)", file=stderr)
            return 1
        requester = urllib_requester(key)
    try:
        r = ensure(requester, endpoint=args.endpoint, new_secret=args.new_webhook_secret)
    except StripeError as exc:
        print(f"error: {exc}", file=stderr)
        return 1
    print(f"mode: {'LIVE' if r['livemode'] else 'test'}", file=stdout)
    print(f"created: {', '.join(r['made']) or 'nothing (all found by metadata bazibo=web-unlock)'}", file=stdout)
    print(f"payment link: {r['url']}", file=stdout)
    print(f"NETNL_STRIPE_PAYMENT_LINK_ID={r['link_id']}", file=stdout)
    if r["secret"]:
        print(f"NETNL_STRIPE_WEBHOOK_SECRET={r['secret']}", file=stdout)
    else:
        print("NETNL_STRIPE_WEBHOOK_SECRET: unchanged (the endpoint exists; Stripe shows a secret only "
              "at creation; --new-webhook-secret makes a new one)", file=stdout)
    if not r["livemode"]:
        print("test mode: set NETNL_STRIPE_ALLOW_TEST_MODE=1 for these sessions to mint codes", file=stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
