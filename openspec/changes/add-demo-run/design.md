# Design: add-demo-run

Pins the buildable surface for `tasks.md` T1–T8. Builds on
`openspec/changes/add-measurement-api/design.md` (the facade this adds a
front door to) without changing anything it already pins. The
BMC-bridge/supporter-key issuance flow that would turn a demo visitor into a
tenant does not exist yet and is out of scope — the "anonymous demo runs"
requirement added below is written as a self-contained family, not numbered
against something that is not built.

## Pinned decisions (D1–D15)

- **D1 — surface.** Routes outside the v2 subset: `POST /demo/requests`
  with body **exactly** `{"domain": "example.nl"}` (pydantic `extra=
  "forbid"` — a list or a `type` field is structurally impossible, not a
  runtime rejection), `GET /demo/requests/{id}`, `GET
  /demo/requests/{id}/results`, plus explicit `OPTIONS` routes on all three
  paths.
- **D2 — opt-in, fail-closed.** `NETNL_DEMO_ENABLED=1` turns the family on;
  once on, `NETNL_DEMO_ALLOWED_ORIGIN` and `NETNL_DEMO_TENANT` are required
  (`SettingsError` naming the missing variable). Off (the default): the
  routes do not exist — the ordinary 501 `not-implemented` catch-all, same
  as any other unmapped path.
- **D3 — borrowed credential, kill switch, one rate mechanism.** The demo
  runs under an ordinary `credentials` row (`NETNL_DEMO_TENANT`), issued
  with `netnl-admin user add` and its printed password thrown away — no one
  ever authenticates as it. If that row is missing or revoked, every demo
  request answers 503 `demo-unavailable`: that is the entire kill switch.
  Limits go through `limits.reserve_submission` unchanged, called with
  `dataclasses.replace(settings, rate_limit=demo.max_per_hour,
  max_concurrent=demo.max_concurrent, max_domains=1)` — the global hourly
  cap this produces **is** the demo tenant's rate limit; there is no second
  counter to keep in sync with it.
- **D4 — per-IP bucket, in-memory, bounded by construction.** Client IP
  comes from the header named by `NETNL_DEMO_CLIENT_IP_HEADER` (default
  `CF-Connecting-IP`), first comma-separated token, parsed with
  `ipaddress`; the bucket key is that address generalised to `/32` (IPv4) or
  `/64` (IPv6). A missing or unparseable header value falls into one shared
  `"unattributed"` bucket rather than being dropped or given its own
  identity. Only an **accepted** run adds an entry — never a rejected one —
  which is what keeps the structure bounded: total entries across all
  buckets can never exceed the number of runs the global per-hour cap (D3)
  has ever let through in the trailing hour. Every call sweeps out entries
  older than an hour before counting, so the structure never accumulates
  indefinitely even at zero traffic afterwards. This mirrors
  `netnl.auth`'s own in-memory failed-auth aggregator (sweep-on-every-call,
  bounded by construction rather than by a hard cap) — see that module's
  docstring for the pattern this borrows.
- **D5 — per-domain cooldown, in-memory.** `NETNL_DEMO_DOMAIN_COOLDOWN_
  SECONDS` (default 900) blocks a repeat run against the same normalised
  domain until the window elapses. A cooldown hit is a plain rejection: it
  **never** returns an existing `request_id` — there is nothing here for a
  visitor to poll into someone else's still-running (or since-finished)
  demo run.
- **D6 — CORS, exactly one origin.** The demo answers with `Access-Control-
  Allow-Origin` set to the literal configured `NETNL_DEMO_ALLOWED_ORIGIN` —
  never an echo of the request's `Origin`, never paired with `Access-
  Control-Allow-Credentials`. Every `/demo/*` reply also carries `Vary:
  Origin` and `Access-Control-Expose-Headers: X-Netnl-Instance, X-Netnl-
  Notice`. `NETNL_DEMO_ALLOWED_ORIGIN` is validated at settings load against
  `^https://[A-Za-z0-9.-]+(:[0-9]{1,5})?$`; a bare `http://localhost[:port]`
  form is accepted only when `NETNL_ALLOW_HTTP=1` (the same escape hatch the
  upstream-endpoint check already uses, reused rather than duplicated for a
  second variable). A request carrying a **present but non-matching**
  `Origin` on an actual demo route (not `OPTIONS`) is refused outright: 403
  `forbidden-origin`. No `Origin` header at all is allowed through — plenty
  of legitimate non-browser callers (curl, the acceptance script, a health
  probe) never send one.
- **D7 — header placement.** A `demo_headers` middleware is registered
  **last** in `create_app`, which — per Starlette's middleware-stacking
  order (last `@app.middleware("http")` registered becomes the outermost
  layer, short of `ServerErrorMiddleware` itself) — makes it wrap every
  other middleware, including the `enforce_body_size` short-circuit, so a
  demo request that never reaches a route handler still gets its demo
  headers. `ServerErrorMiddleware` (the generic-`Exception` handler,
  `handle_unexpected`) sits *outside* the entire user middleware stack, the
  same reason `provenance_headers`/`security_headers` are added there by
  hand already — `handle_unexpected` now also calls the same header-
  computation helper `demo_headers` uses, conditioned on the failing
  request's path starting with `/demo/`, so a 500 on a demo path still
  carries them. Every demo reply carries `Cache-Control: no-store`.
- **D8 — preflight.** Explicit `OPTIONS` handlers on all three demo paths
  return 204 unconditionally — without them the 501 catch-all would answer
  a browser's preflight and break every demo call from a real browser. An
  `OPTIONS` request never gets the 403 `forbidden-origin` check applied (D6
  is about actual method routes); when its `Origin` does not match, it
  still gets 204, just without the CORS headers the `demo_headers`
  middleware would otherwise add on a match — the browser is left to enforce
  the resulting block itself, which is exactly what a CORS preflight is for.
- **D9 — auth is untouched, provably.** No demo route declares `Depends
  (auth.authenticate)`; the `Authorization` header, if present, is read
  nowhere on this path. A monkeypatch of `auth.hash_password` to raise is a
  pinned test: the demo must keep working, because it never calls it.
  Nothing on the demo path writes an `auth-failure` audit row — there is no
  authentication attempt to fail.
- **D10 — audit shape.** A successful demo submission writes exactly one
  audit row, indistinguishable in shape from an ordinary tenant submission:
  event `submit`, `credential = NETNL_DEMO_TENANT`, `domain_count = 1`. No
  visitor IP, `Origin`, or the submitted domain itself appears in that row,
  anywhere else on disk, or in any log line — under any code path,
  including every rejection (403, 429, cooldown, size/shape, unavailable).
  A rejected demo request writes **no** audit row at all.
- **D11 — retention.** `NETNL_DEMO_RETENTION_HOURS` (default 24) is applied
  by `retention.prune`, as a demo-scoped delete added **after** the existing
  reserving-audit step (so a stranded demo reservation is still audited
  under the pre-existing path before this newer, demo-specific delete ever
  runs), reporting its own `demo_deleted` count. `netnl-admin`'s `_prune`
  output prints that count only when a demo configuration is present —
  an operator who never opted in sees output byte-identical to before this
  change.
- **D12 — upstream name.** Every demo submission is sent upstream with
  `name` fixed to the literal `"netnl-demo"` — never visitor-influenced,
  so an operator reading the upstream instance's own dashboard can
  distinguish demo traffic from tenant traffic without needing this
  facade's own audit trail.
- **D13 — labels.** `netnl.replies.LABEL_STATUS` gains `"demo-unavailable":
  503` and `"forbidden-origin": 403`. `"overloaded"` is already present
  from the facade-hardening round and is not duplicated. The 429 from
  `limits.reserve_submission` (and the per-IP/cooldown 429s raised in
  `demo.py` itself) are given visitor-appropriate, plain-language messages
  distinct from the tenant-facing "rate limit of N submissions per hour
  reached" wording — a demo visitor is not expected to know what a
  "credential" or a "concurrency slot" is.
- **D14 — input normalisation.** The only normalisation applied to the
  submitted `domain` string is `str.strip()` then `str.lower()` — no other
  rewriting. Any failure past that (empty, too long, wrong shape, an
  IP-literal or internal-use suffix caught by the reused `limits.
  check_domains`) answers 400 `bad-request` with the single literal,
  directly-showable message `"enter a bare domain like example.nl, not a
  URL"` — chosen so the demo page can render it to a visitor unmodified,
  unlike the tenant-facing messages in `limits.py`, which are written for
  an API consumer, not a person filling in a form.
- **D15 — injectable time.** Every demo-side use of "now" — the reservation
  timestamp, the per-IP sweep, the per-domain cooldown check — goes through
  `app.state.now()`, the same injectable clock the rest of the facade
  already uses, so a fixed `Clock` fixture can advance past a cooldown or an
  hourly window deterministically in tests.

## Configuration (environment only, prefix `NETNL_DEMO_`)

| Variable | Default | Meaning |
|---|---|---|
| `NETNL_DEMO_ENABLED` | unset (off) | `1` turns the `/demo/*` route family on |
| `NETNL_DEMO_ALLOWED_ORIGIN` | — (required when on) | The single origin the demo answers to on the wire |
| `NETNL_DEMO_TENANT` | — (required when on) | Username of the borrowed credential row the demo runs under |
| `NETNL_DEMO_MAX_PER_HOUR` | `6` | The demo tenant's own hourly submit cap (via `limits.reserve_submission`) |
| `NETNL_DEMO_MAX_CONCURRENT` | `2` | The demo tenant's own concurrency cap |
| `NETNL_DEMO_PER_IP_PER_HOUR` | `2` | Accepted runs per client-IP bucket per trailing hour |
| `NETNL_DEMO_CLIENT_IP_HEADER` | `CF-Connecting-IP` | Header read for the per-IP bucket key |
| `NETNL_DEMO_DOMAIN_COOLDOWN_SECONDS` | `900` | Seconds a domain is blocked from a repeat run after an accepted one |
| `NETNL_DEMO_RETENTION_HOURS` | `24` | Hours a demo-owned request row is retrievable before `prune` removes it |

Missing `NETNL_DEMO_ALLOWED_ORIGIN`/`NETNL_DEMO_TENANT` while
`NETNL_DEMO_ENABLED=1` → refuse to start, naming the missing variable, same
fail-closed convention as the tenant-surface required variables.

## HTTP surface

- `POST /demo/requests` — body `{"domain": str}` only (`extra="forbid"`);
  normalise (D14); origin check (D6); demo-credential availability check
  (D3, 503 `demo-unavailable`); cooldown check (D5, 429); per-IP check (D4,
  429); `limits.check_domains` reused for shape/SSRF (400, D14's literal
  message on any failure); `limits.reserve_submission` with the
  `dataclasses.replace`d settings (D3, 429 on the demo tenant's own
  rate/concurrency limit); only on a successful reservation, record the
  per-IP and per-domain-cooldown entries and call upstream with `name=
  "netnl-demo"` (D12); reply in the same v2 `RequestReply` shape as the
  tenant surface, with the facade id substituted.
- `GET /demo/requests/{id}` / `GET /demo/requests/{id}/results` — owner-
  scoped to the demo credential via the shared `store.owned_request_or_404`
  helper (lifted out of `api.py`'s former `_owned_request_or_404` verbatim,
  parameterised on a credential id rather than a whole credential row — no
  behaviour change for the tenant surface that already calls it);
  passthrough identical to the tenant routes otherwise.
- `OPTIONS /demo/requests`, `OPTIONS /demo/requests/{id}`, `OPTIONS
  /demo/requests/{id}/results` — 204, unconditionally (D8).
- Every demo reply (success or error, including the generic 500) carries
  the D6/D7 CORS headers plus `Cache-Control: no-store`, added by
  `demo.demo_response_headers`, called from both the `demo_headers`
  middleware and `handle_unexpected`.

## Reused, not reimplemented

- `limits.check_domains` — the exact same shape/anti-SSRF checks the
  tenant surface uses, called with the single-element `[domain]` list; a
  demo-side wrapper translates *any* failure from it into D14's one literal
  message, since the tenant-facing wording ("the facade only accepts a
  public, multi-label hostname...") is not meant for an anonymous visitor.
- `limits.reserve_submission` — verbatim, with a `dataclasses.replace`d
  `Settings` (D3) so the demo tenant's rate/concurrency numbers, not the
  operator's own tenant defaults, gate it; this is also what makes the
  demo's audit row (D10) identical in shape to an ordinary tenant submit —
  it is the same code path.
- `store.owned_request_or_404` — lifted verbatim out of `api.py`'s private
  `_owned_request_or_404` (see "HTTP surface" above); every existing
  tenant-surface call site is updated to call the same shared function, with
  no behaviour change (same 404 shape, same "foreign id is indistinguishable
  from unknown" guarantee).
- `netnl.replies.LABEL_STATUS`, `netnl.errors.NetnlHTTPError`,
  `retention.prune`'s existing per-pass transaction and audit-then-delete
  ordering.

## Privacy (D10, stated as an invariant, not a hope)

Nothing about a demo request — its client IP, its `Origin`, or the domain it
asked about — is written to the SQLite file or emitted through the standard
logging module, on **any** path: accepted, rejected for any reason (origin
mismatch, unavailable credential, cooldown, per-IP cap, tenant-style
rate/concurrency cap, shape/SSRF failure), or a crash reaching the generic
500 handler. The only two facts recorded anywhere are the fixed literal
`"submit"` event with `domain_count = 1` against the fixed `NETNL_DEMO_
TENANT` credential (on acceptance only) and, via the ordinary retention
path, `"demo-pruned"`-flavoured bookkeeping with no visitor-identifying
content beyond what the pre-existing `reserving-pruned` audit shape already
carries (facade id, credential, domain count, timestamp — no domain
string). T6's tests assert this by grepping the raw database file bytes and
`caplog` for injected fake IP/origin/domain markers across every one of
those paths, not by reading the code and trusting it.

## Testing constraints

Same as `add-measurement-api`'s ("Testing constraints"): no network I/O,
`TestClient` + `FakeOpener`, `$HOME`-isolation fixture unaffected,
`app.state.now()` (D15) makes the per-IP/cooldown windows deterministic in
tests via the existing `Clock` fixture. New module-level in-memory state in
`demo.py` (the per-IP and per-domain-cooldown structures) needs its own
autouse reset fixture, mirroring `tests/netnl/conftest.py`'s existing
`_reset_auth_failure_aggregator` for `netnl.auth`'s equivalent state.

## Exit criteria for this build

`sh scripts/verify.sh` green with the full suite; `openspec validate
add-demo-run --strict` and `openspec validate --all --strict` both green;
tasks T1–T8 ticked; owner-input placeholders (O1–O6 in `tasks.md`, if any
are added there) stay unticked — this build commits nothing on questions
only the owner can answer.
