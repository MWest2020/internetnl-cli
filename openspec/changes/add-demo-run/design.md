# Design: add-demo-run

Pins the buildable surface for `tasks.md` T1–T8. Builds on
`openspec/changes/add-measurement-api/design.md` (the facade this adds a
front door to) without changing anything it already pins. The
BMC-bridge/supporter-key issuance flow that would turn a demo visitor into a
tenant does not exist yet and is out of scope — the "anonymous demo runs"
requirement added below is written as a self-contained family, not numbered
against something that is not built.

## Pinned decisions (D1–D17)

Builder-review hardening pass (post T1–T8, two parallel adversarial/
security reviews): D3, D4, D5 and D13 below are amended in place; D16
(poll budget) and D17 (reissue) are new. See `tasks.md`'s T9 for the full
list and the measured findings each amendment closes.

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
  counter to keep in sync with it. **Builder-review fix (S2=B1):** `POST
  /demo/requests` now calls `limits.refresh_stale_non_terminal` immediately
  before this reservation, exactly like the tenant path's `submit` does —
  without it, a demo run whose upstream status went terminal without ever
  being polled kept occupying a concurrency slot until
  `NETNL_DEMO_RETENTION_HOURS` pruned it (24h by default), able to starve
  every later visitor with 429s in the meantime. **Builder-review fix
  (S6=B3):** the kill switch is two-directional — `netnl-admin user reissue
  <name>` (D17) re-enables a revoked `NETNL_DEMO_TENANT` row without a
  restart; `netnl-admin user add` alone cannot, since it refuses on an
  already-existing username whether that row is revoked or not.
- **D4 — per-IP bucket, in-memory, bounded, atomically claimed.** Client IP
  comes from the header named by `NETNL_DEMO_CLIENT_IP_HEADER` (default
  `CF-Connecting-IP`), first comma-separated token, parsed with
  `ipaddress`; the bucket key is that address generalised to `/32` (IPv4) or
  `/64` (IPv6). A missing or unparseable header value falls into one shared
  `"unattributed"` bucket rather than being dropped or given its own
  identity. **Builder-review fix (S1=M1):** the original shape — an
  `_ip_over_limit` read followed by a separate `_record_ip_accept` write,
  each under its own independent lock acquisition — left a check-then-act
  race a real ASGI server's thread-pool scheduling can hit: measured, 12
  parallel submits from one IP against a cap of 2 → 12 accepted.
  `_try_claim_ip_slot` now does sweep+check+insert inside one lock hold;
  `_release_ip_slot` undoes a claim when a later step (the reservation, or
  the upstream call) fails, so a claim that produced no accepted run never
  still costs the visitor a slot. `_MAX_BUCKETS` (4096, overflow-key shape
  mirroring `netnl.auth`'s own failed-auth aggregator) caps the number of
  distinct keys this structure will ever hold at once — claiming *before*
  the reservation that could still fail means this can no longer be
  described as "only ever grows on an outcome the global cap already
  gated", so an explicit hard ceiling replaces that argument rather than
  merely asserting it.
- **D5 — per-domain cooldown, in-memory, atomically claimed.**
  `NETNL_DEMO_DOMAIN_COOLDOWN_SECONDS` (default 900) blocks a repeat run
  against the same normalised domain until the window elapses. A cooldown
  hit is a plain rejection: it **never** returns an existing `request_id`
  — there is nothing here for a visitor to poll into someone else's
  still-running (or since-finished) demo run. **Builder-review fix
  (S1=M1):** same atomic claim-then-release shape as D4's per-IP bucket
  (`_try_claim_domain`/`_release_domain_claim`), for the same measured
  race (8 different IPs submitting the same domain concurrently → 8
  accepted, before the fix). **Builder-review fix (S5):** the cooldown
  rejection and the per-IP-cap rejection (D4) now share the exact same
  429 message, and the per-IP claim is attempted strictly before the
  domain claim — an already over-quota IP's request now never even
  touches the cooldown state for the domain it submitted, and even where
  both would reject, the identical wording gives an observer no way to
  tell which bound actually fired for someone else's request. Before this
  fix, distinct wording ("this domain was checked recently..." vs. "too
  many demo runs from this network...") let an over-quota IP learn whether
  an unrelated domain was on cooldown purely from which message it got
  back.
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
  "credential" or a "concurrency slot" is. **Builder-review fix (S4=B2):**
  this was previously incomplete — `POST /demo/requests` let
  `reserve_submission`'s own `NetnlHTTPError` (which *does* name the
  configured numbers) propagate to the reply verbatim. It is now caught
  and rewritten to one fixed literal (`"the demo is busy right now; please
  try again shortly"`), separate from the per-IP/cooldown literal.
  **Builder-review fix (M3):** every upstream-originated `NetnlHTTPError`
  reaching a demo route (`_translate_api_error`'s messages and
  `TransportError`'s own message both embed the upstream hostname) is
  rewritten by `netnl.demo._visitor_upstream_error` to one of two fixed,
  host-free outcomes: a `TransportError` (upstream unreachable at the
  network level) reuses the exact 503 `demo-unavailable` outcome D3's kill
  switch already uses; anything else `call_upstream` produced (a
  translated 401/403, a malformed 2xx, or a real upstream status passed
  through) becomes a fixed 502 `upstream-error`, `"the measurement
  instance is unreachable right now"` — the demo visitor never sees which
  real upstream status caused it. **Builder-review fix (S9):** a pydantic
  `RequestValidationError` on any `/demo/*` path (an extra field, a `type`
  field, a list `domain`, ...) is now flattened in `api.py`'s
  `handle_validation_error` to D14's own literal message rather than the
  tenant-facing "invalid request body: field, field, ..." shape, which
  would otherwise reflect raw pydantic field/`loc` paths back to an
  anonymous visitor.
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
- **D16 — poll budget, and terminal rows answered from the store
  (builder-review fix, M2).** Before this fix, `GET /demo/requests/{id}`
  and `.../{id}/results` had no bound at all beyond D3's aggregate cap on
  *submissions* — an anonymous caller could poll one id an unbounded
  number of times, each poll a real upstream call. Two changes: (a) a
  status poll (not a results fetch — those are passthrough and always ask
  upstream) whose *stored* `last_status` is already terminal
  (`store.TERMINAL_STATUSES`) is answered straight from the row, with no
  upstream call at all — a terminal status cannot change further; (b) a
  new per-IP bucket, `NETNL_DEMO_POLLS_PER_IP_PER_HOUR` (default 120,
  reusing the same atomic claim/sweep/`_MAX_BUCKETS`-overflow shape as
  D4), bounds status/results requests per client-IP bucket per trailing
  hour, independently of the per-submission bucket. 120 is sized against
  the page's own documented poll cadence (5s → 15s, ~10-minute give-up —
  roughly 45 polls for one run through to completion).
- **D17 — `netnl-admin user reissue` (builder-review fix, S6=B3).** A new
  admin subcommand, `netnl-admin user reissue <name>`, re-keys an
  *existing* credential row in place: a fresh generated password and salt,
  `revoked_at` cleared, printed once to stdout exactly like `user add`'s
  password (thrown away the same way for the demo tenant, which never
  authenticates as itself — D9). Unlike `user add`, it never refuses on
  "already exists" — it works whether the row is currently revoked or not,
  and refuses only when no row with that username exists at all. Audited
  as `user-reissue`, the same shape as `user-add`/`user-revoke`
  (`credential = <name>`, no other fields). This is D3's kill switch's
  other, previously-missing half: `revoke` turns a surface off, `reissue`
  turns it back on, both without a restart or a configuration change.

## Header trust and multiple replicas (builder-review fix, S3 — doc-only)

`NETNL_DEMO_CLIENT_IP_HEADER` (`CF-Connecting-IP` by default) is trusted
verbatim for the per-IP bucket (D4) and the poll budget (D16) — this
facade cannot itself verify who actually set it. That is a safe assumption
only when every path that can reach the facade is guaranteed to have that
header set (or overwritten) by a trusted edge; a caller that reaches the
facade some other way (design.md's own "Two supported topologies" —
concretely, topology 1's Tailscale Funnel fallback) can put anything at
all in that header and land in a fresh per-IP bucket on every request. The
per-IP bucket and poll budget are therefore a **dampener against casual
repeat use**, not the hard limit — D3's aggregate rate/concurrency cap
(backed by `limits.reserve_submission`'s atomic reservation transaction
against the shared SQLite file) is the one bound that holds regardless of
what any header claims, since it keys on nothing header-derived at all.
See `docs/how-to/demo-run.md`'s "Header trust and the client IP" section
for the operator-facing version of this, including the concrete options
for closing (or accepting) the Funnel-bypass gap.

Separately: the per-IP bucket, the per-domain cooldown and the poll budget
are all **in-process** state — one running `netnl-serve` instance, not
shared across replicas. Running more than one replica multiplies each of
these bounds' *effective* ceiling by the replica count; only D3's
aggregate cap (SQLite-backed, shared) still holds cluster-wide.

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
| `NETNL_DEMO_POLLS_PER_IP_PER_HOUR` | `120` | Status/results requests per client-IP bucket per trailing hour (D16) |

Missing `NETNL_DEMO_ALLOWED_ORIGIN`/`NETNL_DEMO_TENANT` while
`NETNL_DEMO_ENABLED=1` → refuse to start, naming the missing variable, same
fail-closed convention as the tenant-surface required variables.

## HTTP surface

- `POST /demo/requests` — body `{"domain": str}` only (`extra="forbid"`,
  D13's fix flattens any pydantic rejection to D14's literal). Check order
  (builder-review fix, S5 — see D5): origin check (D6) → demo-credential
  availability check (D3, 503 `demo-unavailable`) → normalise (D14) and
  `limits.check_domains` reused for shape/SSRF (400, D14's literal message
  on any failure) → atomic per-IP claim (D4, 429 on the shared literal) →
  atomic per-domain-cooldown claim (D5, 429 on the *same* shared literal,
  releasing the per-IP claim first) → `limits.refresh_stale_non_terminal`
  (D3's fix) → `limits.reserve_submission` with the `dataclasses.replace`d
  settings (D3, 429 rewritten to D13's fixed visitor literal on the demo
  tenant's own rate/concurrency limit, releasing both claims) → upstream
  call with `name="netnl-demo"` (D12), `_visitor_upstream_error`-wrapped
  (D13's fix), releasing both claims on failure. Only once every step
  above succeeds does the reply go out, in the same v2 `RequestReply`
  shape as the tenant surface, with the facade id substituted.
- `GET /demo/requests/{id}` — origin check → availability check → poll-
  budget claim (D16, 429 on its own literal) → owner-scoped lookup via
  `store.owned_request_or_404` → a `reserving` row answers from the store
  (unchanged); a row whose *stored* status is already terminal
  (`store.TERMINAL_STATUSES`) also answers from the store, no upstream
  call (D16); anything else calls upstream, `_visitor_upstream_error`-
  wrapped (D13's fix).
- `GET /demo/requests/{id}/results` — same origin/availability/poll-budget
  checks; always passthrough to upstream once past a `reserving` row (this
  facade never stores the `domains` payload, so there is no terminal-row
  store-answer here the way there is for status), `_visitor_upstream_error`-
  wrapped.
- `OPTIONS /demo/requests`, `OPTIONS /demo/requests/{id}`, `OPTIONS
  /demo/requests/{id}/results` — 204, unconditionally (D8); on an origin
  match (or no `Origin` at all), also `Access-Control-Allow-Methods: POST,
  GET, OPTIONS`, `Access-Control-Allow-Headers: content-type` and
  `Access-Control-Max-Age` (builder-review fix, S7 — previously missing
  entirely, which meant a browser's own preflight could never grant
  permission for the actual cross-origin `POST` with a JSON `Content-Type`
  the demo page needs to make).
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
- `limits.refresh_stale_non_terminal` — verbatim (builder-review fix,
  S2=B1), called with the same `dataclasses.replace`d settings, right
  before `reserve_submission`, exactly where the tenant path's own `submit`
  calls it.
- `store.TERMINAL_STATUSES` (builder-review fix, M2) — the same set the
  tenant path's `non_terminal_requests` already uses, made public so
  `demo.py` can check a stored row's status without duplicating that set.
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
tests via the existing `Clock` fixture. Module-level in-memory state in
`demo.py` (the per-IP, per-domain-cooldown and — builder-review fix, D16 —
per-poll structures) needs its own autouse reset fixture, mirroring
`tests/netnl/conftest.py`'s existing `_reset_auth_failure_aggregator` for
`netnl.auth`'s equivalent state. Builder-review fix (S1=M1): the atomic
per-IP/per-domain claim functions are additionally proven race-free under
*real* concurrency (a genuine `uvicorn.Server`, not `TestClient` — see
`tests/netnl/test_netnl_real_server.py`'s own module docstring for why
`TestClient` cannot reproduce this class of bug), the same pattern this
project already established for the pre-existing N1 fix in that same file.

## Exit criteria for this build

`sh scripts/verify.sh` green with the full suite; `openspec validate
add-demo-run --strict` and `openspec validate --all --strict` both green;
tasks T1–T9 ticked; owner-input placeholders (O1–O6 in `tasks.md`, if any
are added there) stay unticked — this build commits nothing on questions
only the owner can answer.
