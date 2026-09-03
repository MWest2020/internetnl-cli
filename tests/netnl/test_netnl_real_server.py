"""Round-4 regression test for finding N1 (HIGH, pre-existing, also on
`main`): `store.get_conn` is a sync generator FastAPI dependency. FastAPI
resolves a sync generator dependency by running its body up to `yield`
inside one `run_in_threadpool` call, the sync route handler (and any other
sync `Depends` sharing that connection, e.g. `auth.authenticate`) inside a
*separate* `run_in_threadpool` call, and the generator's post-response
cleanup inside a third — each of which can land on a different real OS
worker thread from anyio's threadpool. With `check_same_thread=True`
(sqlite3's default, which `store.connect` used unconditionally before this
fix), using the connection from a thread other than the one that opened it
raised `sqlite3.ProgrammingError`, surfaced as a generic 500.

Measured on a real uvicorn server before the fix: 0 failures at 2 concurrent
requests, 14/24 at 4, 85/96 at 16 — and of 16 concurrent auth failures, only
3 made it into the audit trail (the rest were lost to a 500 before
`_record_auth_failure`'s write ever completed).

**Why this has to be a real uvicorn server, not `starlette.TestClient`:**
`TestClient` dispatches every call through a single blocking portal thread
running one event loop; empirically (this project's own concurrency suite,
`tests/netnl/test_netnl_concurrency.py`, all of it `TestClient`-based) that
never reproduces the cross-thread connection use above, no matter how many
Python threads drive it concurrently. Only a real ASGI server's own
threadpool scheduling exhibits it. This test therefore runs a genuine
`uvicorn.Server` on a background thread with its *own* asyncio event loop,
bound to a real TCP socket on localhost, and drives it with independent
client threads making real HTTP requests over that socket — the actual
production execution model design.md describes, not a stand-in for it.

The fix (`store.py`, `connect(..., allow_cross_thread=True)` used only by
`get_conn`) is exercised here at 16 genuinely concurrent requests. This test
has no `pytest-timeout` dependency (not in this project's dev group); it
bounds its own worst case instead via `urllib` per-call timeouts and
`Thread.join(timeout=...)`, so a hang here fails loudly rather than wedging
`sh scripts/verify.sh`.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import socket
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

import pytest
import uvicorn

from conftest import DEMO_ORIGIN, DEMO_TENANT, SUPPORTER_SECRET, Clock, add_test_credential, bmc_payload
from fakes import METADATA_REPLY, REGISTER_REPLY
from internetnl_cli.client import HttpResponse

from netnl import store
from netnl.api import create_app
from netnl.settings import load


def _free_port() -> int:
    # Bind-then-close: a small, accepted TOCTOU race (another process could
    # in theory grab the port before uvicorn binds it), but this project's
    # other real-socket tests (`scripts/acceptance.sh`) accept the same
    # trade-off, and it is far simpler than plumbing a pre-opened fd through
    # `uvicorn.Config`.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class _LockedMetadataOpener:
    """A thread-safe fake upstream that always answers the same metadata
    reply, however many callers hit it at once.

    `fakes.FakeOpener`'s queue is deliberately simple (pop-in-order, no
    lock) because every other test drives it from one thread at a time or
    through `TestClient`'s serialising portal; used from genuinely
    concurrent real threads here, its unguarded `list.pop(0)` could
    misreport a spurious "ran out of queued responses" `AssertionError` —
    a false failure with nothing to do with the N1 bug this test targets.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.calls = 0

    def __call__(self, method, url, body, headers, timeout) -> HttpResponse:
        with self._lock:
            self.calls += 1
        return HttpResponse(status=200, body=json.dumps(METADATA_REPLY).encode())


class _LockedRegisterOpener:
    """Same shape as `_LockedMetadataOpener` above (a real lock around a
    shared call counter), answering every call with a register-shaped
    reply — good enough for both `client.submit` (the demo submit route)
    and `client.status` (`limits.refresh_stale_non_terminal`'s own
    upstream call), since this test only cares about how many *demo*
    submissions the facade itself accepts, not the shape of the upstream
    reply beyond "valid enough to parse".
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.calls = 0

    def __call__(self, method, url, body, headers, timeout) -> HttpResponse:
        with self._lock:
            self.calls += 1
        return HttpResponse(status=200, body=json.dumps(REGISTER_REPLY).encode())


def _basic_auth_header(username: str, password: str) -> str:
    token = base64.b64encode(f"{username}:{password}".encode()).decode()
    return f"Basic {token}"


def _post_json(port: int, path: str, body: dict, headers: dict) -> int:
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=data,
        headers={"Content-Type": "application/json", **headers},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code


def _get(port: int, path: str, auth_header: str) -> int:
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", headers={"Authorization": auth_header}
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code


def _wait_until_ready(port: int, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    last_exc: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1) as resp:
                if resp.status == 200:
                    return
        except OSError as exc:
            last_exc = exc
        time.sleep(0.05)
    raise RuntimeError(f"real uvicorn server on port {port} never became ready: {last_exc}")


class _RealServer:
    """A real `uvicorn.Server`, run on a background thread with its own
    asyncio event loop, torn down deterministically at the end of the
    `with` block.
    """

    def __init__(self, app) -> None:
        self.port = _free_port()
        config = uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="warning")
        self.server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        asyncio.run(self.server.serve())

    def __enter__(self) -> "_RealServer":
        self._thread.start()
        _wait_until_ready(self.port)
        return self

    def __exit__(self, *exc_info) -> None:
        self.server.should_exit = True
        self._thread.join(timeout=10)
        if self._thread.is_alive():
            pytest.fail("real uvicorn server thread did not shut down within 10s")


def test_real_uvicorn_handles_concurrent_requests_without_cross_thread_500s(
    settings_env, tmp_path
):
    env = dict(settings_env)
    env["NETNL_DB"] = str(tmp_path / "real-server.sqlite3")
    settings = load(env)

    clock = Clock()
    opener = _LockedMetadataOpener()
    app = create_app(settings, opener=opener, now=clock)
    add_test_credential(app, "tenant", "secret")

    good_auth = _basic_auth_header("tenant", "secret")
    bad_auth = _basic_auth_header("tenant", "wrong-password")

    n_valid = 8
    n_invalid = 8
    n_total = n_valid + n_invalid
    assert n_total >= 16

    with _RealServer(app) as running:
        # Real concurrency: a thread pool of real OS threads, each making a
        # real HTTP request over a real TCP socket — not `TestClient`,
        # which cannot reproduce the bug this guards against (see module
        # docstring).
        with ThreadPoolExecutor(max_workers=n_total) as pool:
            valid_futures = [
                pool.submit(_get, running.port, "/metadata/report", good_auth)
                for _ in range(n_valid)
            ]
            invalid_futures = [
                pool.submit(_get, running.port, "/metadata/report", bad_auth)
                for _ in range(n_invalid)
            ]
            valid_statuses = [f.result(timeout=15) for f in valid_futures]
            invalid_statuses = [f.result(timeout=15) for f in invalid_futures]

        statuses = valid_statuses + invalid_statuses
        # The N1 bug: `sqlite3.ProgrammingError` from cross-thread connection
        # use, surfaced as a generic 500. Zero, full stop.
        assert statuses.count(500) == 0, statuses
        # Every response is one of the shapes this route can legitimately
        # produce: 200 (authenticated ok), 401 (bad credential), or 503
        # (scrypt-concurrency cap saturated — a legitimate, documented
        # outcome at this concurrency, not a bug in itself; see
        # design.md's "Authentication cost is bounded on two axes").
        assert set(valid_statuses) <= {200, 503}, valid_statuses
        assert set(invalid_statuses) <= {401, 503}, invalid_statuses
        assert len(statuses) == n_total

        observed_401 = invalid_statuses.count(401)

        # --- flush trigger -------------------------------------------------
        # `netnl.auth`'s failed-auth aggregator only ever flushes a bucket
        # to `audit` once a *later* wall-clock minute's request arrives
        # (design.md, "Audit"). Advance the shared, injected clock past the
        # failures' own minute and fire one more authenticated request to
        # trigger that sweep — still over the same real server, still a
        # genuinely separate real HTTP request.
        clock.advance(61)
        flush_status = _get(running.port, "/metadata/report", good_auth)
        assert flush_status in (200, 503), flush_status

    # --- every auth failure survived into the audit trail -------------------
    # A single connection, opened fresh here from the (single) test thread,
    # default `check_same_thread=True` — this is not the per-request path
    # under test, just this test's own inspection connection (same pattern
    # as every other test's `conn` fixture).
    conn = store.connect(settings.db)
    try:
        rows = conn.execute(
            "SELECT detail FROM audit WHERE event = 'auth-failure' AND credential = 'tenant'"
        ).fetchall()
    finally:
        conn.close()

    if observed_401 == 0:
        # Every invalid-credential request happened to answer 503 instead
        # (scrypt cap saturated before any of them reached
        # `_record_auth_failure`) — there is nothing to have flushed. Rare
        # at this concurrency and CPU-count-derived cap, but not a failure
        # of the property under test.
        assert rows == []
    else:
        assert len(rows) == 1, rows
        assert rows[0]["detail"] == f"/metadata/report failures={observed_401}", rows[0]["detail"]


# --- builder-review fix (S1=M1): demo per-IP cap and per-domain cooldown,
# --- proven race-free under real concurrency, not just `TestClient` --------
#
# Both bounds used to check ("is this key already at/over its limit?") and
# act ("record this acceptance") across two *separate* lock acquisitions
# (`netnl.demo`'s previous `_ip_over_limit`/`_record_ip_accept` and
# `_domain_on_cooldown`/`_record_domain_cooldown`), leaving the same
# classic check-then-act race window `test_netnl_real_server.py`'s first
# test (above) already demonstrates a real uvicorn server's thread-pool
# scheduling can hit, that `TestClient` cannot reproduce (see the module
# docstring). Measured before the fix, on a real server: 12 parallel
# submits from one IP against a per-IP cap of 2 -> 12 accepted; 8 submits
# for the same domain from 8 different IPs (cooldown otherwise unbounded)
# -> 8 accepted. `netnl.demo._try_claim_ip_slot`/`_try_claim_domain` now do
# sweep+check+insert inside one lock hold each.


def test_real_server_demo_per_ip_cap_holds_under_concurrent_submits(settings_env, tmp_path):
    env = dict(settings_env)
    env["NETNL_DB"] = str(tmp_path / "real-server-demo-ip.sqlite3")
    env["NETNL_DEMO_ENABLED"] = "1"
    env["NETNL_DEMO_ALLOWED_ORIGIN"] = DEMO_ORIGIN
    env["NETNL_DEMO_TENANT"] = DEMO_TENANT
    per_ip_cap = 2
    env["NETNL_DEMO_PER_IP_PER_HOUR"] = str(per_ip_cap)
    # High enough that only the per-IP cap under test can reject anything.
    env["NETNL_DEMO_MAX_PER_HOUR"] = "1000"
    env["NETNL_DEMO_MAX_CONCURRENT"] = "1000"
    settings = load(env)

    opener = _LockedRegisterOpener()
    app = create_app(settings, opener=opener)
    add_test_credential(app, DEMO_TENANT, "thrown-away-password")

    n_requests = 12
    same_ip = {"Origin": DEMO_ORIGIN, "CF-Connecting-IP": "203.0.113.42"}

    with _RealServer(app) as running:
        with ThreadPoolExecutor(max_workers=n_requests) as pool:
            futures = [
                pool.submit(
                    _post_json,
                    running.port,
                    "/demo/requests",
                    {"domain": f"race-ip-{i}.example.nl"},
                    same_ip,
                )
                for i in range(n_requests)
            ]
            statuses = [f.result(timeout=15) for f in futures]

    assert set(statuses) <= {200, 429}, statuses
    accepted = statuses.count(200)
    # The whole point of the fix: never more, *and* never fewer (a vacuous
    # pass at 0 accepted would say nothing about the race), than the
    # configured cap, however many identical requests arrive at the exact
    # same instant (round-4 builder-review fix, N6).
    assert accepted == per_ip_cap, statuses


def test_real_server_demo_cooldown_holds_under_concurrent_submits(settings_env, tmp_path):
    env = dict(settings_env)
    env["NETNL_DB"] = str(tmp_path / "real-server-demo-cooldown.sqlite3")
    env["NETNL_DEMO_ENABLED"] = "1"
    env["NETNL_DEMO_ALLOWED_ORIGIN"] = DEMO_ORIGIN
    env["NETNL_DEMO_TENANT"] = DEMO_TENANT
    # High enough that only the per-domain cooldown under test can reject
    # anything — every request below comes from its own distinct address.
    env["NETNL_DEMO_PER_IP_PER_HOUR"] = "1000"
    env["NETNL_DEMO_MAX_PER_HOUR"] = "1000"
    env["NETNL_DEMO_MAX_CONCURRENT"] = "1000"
    settings = load(env)

    opener = _LockedRegisterOpener()
    app = create_app(settings, opener=opener)
    add_test_credential(app, DEMO_TENANT, "thrown-away-password")

    n_requests = 8

    with _RealServer(app) as running:
        with ThreadPoolExecutor(max_workers=n_requests) as pool:
            futures = [
                pool.submit(
                    _post_json,
                    running.port,
                    "/demo/requests",
                    {"domain": "race-cooldown.example.nl"},
                    {"Origin": DEMO_ORIGIN, "CF-Connecting-IP": f"198.51.100.{i}"},
                )
                for i in range(n_requests)
            ]
            statuses = [f.result(timeout=15) for f in futures]

    assert set(statuses) <= {200, 429}, statuses
    accepted = statuses.count(200)
    # Exactly one distinct-address submitter can ever win the cooldown for
    # the same domain at (near enough) the same instant — `== 1`, not
    # `<= 1`, so a vacuous 0-accepted pass cannot hide a broken claim
    # (round-4 builder-review fix, N6).
    assert accepted == 1, statuses


# --- security review round (post-147903c), B1/M2: the supporter webhook ----
# --- bridge's idempotency holds under genuine concurrency -------------------
#
# Measured before the B1 fix (`netnl.supporter._persist_and_mint`'s pending-
# lease check and `_record_delivery_outcome`'s conditional write — see that
# module's docstrings): 5 concurrent, identically-signed deliveries for the
# *same* BMC transaction id minted 5 credentials, each one's own takeover
# immediately revoking the previous call's still-in-flight credential, and
# whichever call's mail happened to finish last stamped the row with its own
# username — leaving at least one active credential no `supporter_issuance`
# row referenced (an orphan). `BEGIN IMMEDIATE` alone does not prevent this:
# mail is sent *outside* that transaction (D3), so the write lock is
# released between the persist-commit and the mail-outcome write, and a
# concurrent call landing in exactly that window is the race. Only a real
# uvicorn server's own threadpool scheduling reproduces this reliably (see
# the module docstring's "why this has to be a real uvicorn server" note);
# this test's `_SlowRecordingSender` deliberately widens that window (a
# short, real `time.sleep`) so the race is exercised on every run, not just
# when thread scheduling happens to land on it by chance.


class _SlowRecordingSender:
    """A thread-safe `netnl.mail.Sender` that records every `Mail` it is
    asked to send, after a short, real sleep — deliberately widening the
    window between `_persist_and_mint`'s commit and `_record_delivery_
    outcome`'s write (both quick otherwise) so a genuinely concurrent
    request reliably lands inside it, on a real server, every run.
    """

    def __init__(self, delay: float = 0.05) -> None:
        self._lock = threading.Lock()
        self._delay = delay
        self.sent: list = []

    def __call__(self, mail_obj) -> None:
        time.sleep(self._delay)
        with self._lock:
            self.sent.append(mail_obj)


def _post_webhook(port: int, payload: dict, secret: str = SUPPORTER_SECRET) -> int:
    body = json.dumps(payload).encode()
    signature = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/webhooks/bmc",
        data=body,
        headers={"Content-Type": "application/json", "X-Signature-Sha256": signature},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code


def test_real_server_same_txn_concurrent_deliveries_mint_one_credential(
    settings_env, tmp_path
):
    env = dict(settings_env)
    env["NETNL_DB"] = str(tmp_path / "real-server-supporter.sqlite3")
    env["NETNL_BMC_WEBHOOK_SECRET"] = SUPPORTER_SECRET
    env["NETNL_PUBLIC_ENDPOINT"] = "https://facade.example.org"
    env["NETNL_SMTP_HOST"] = "smtp.example.org"
    env["NETNL_SMTP_FROM"] = "netnl@example.org"
    # High enough that only the concurrency/idempotency fix under test can
    # reject anything — the hourly cap must not be what limits this test.
    env["NETNL_SUPPORTER_MAX_PER_HOUR"] = "1000"
    settings = load(env)

    sender = _SlowRecordingSender()
    app = create_app(settings, sender=sender)

    n_requests = 8
    payload = bmc_payload(data={"transaction_id": "real-server-race-txn"})

    with _RealServer(app) as running:
        with ThreadPoolExecutor(max_workers=n_requests) as pool:
            futures = [
                pool.submit(_post_webhook, running.port, payload) for _ in range(n_requests)
            ]
            statuses = [f.result(timeout=15) for f in futures]

    # Every response is either the (single) successful issuance/duplicate
    # (200) or the concurrency guard's own 503 — never a 500, never a 429.
    assert set(statuses) <= {200, 503}, statuses
    assert statuses.count(500) == 0, statuses

    # The load-bearing invariant, independent of exactly how the 200/503
    # split landed: exactly one mail was ever sent, and exactly one active
    # credential exists for this transaction, and it is the one the
    # issuance row itself references — never more (a duplicate mint) and
    # never an orphan (an active credential the row does not name).
    assert len(sender.sent) == 1, [m.to for m in sender.sent]

    conn = store.connect(settings.db)
    try:
        row = store.find_issuance(conn, "real-server-race-txn")
        assert row is not None
        assert row["state"] == "delivered"

        active = conn.execute(
            "SELECT username FROM credentials WHERE username LIKE 'supporter-%' "
            "AND revoked_at IS NULL"
        ).fetchall()
        assert [r["username"] for r in active] == [row["username"]], (
            "expected exactly one active supporter credential, referenced by "
            "the issuance row — any other active row is an orphan"
        )
    finally:
        conn.close()
