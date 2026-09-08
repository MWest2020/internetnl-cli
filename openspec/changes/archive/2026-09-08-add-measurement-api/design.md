# Design: add-measurement-api (netnl)

Pins the buildable surface for sections 1–3 of `tasks.md` — the facade code
and its tests, which need no live instance (the upstream client is faked at
the same opener seam the CLI uses). Sections 0, 4 and 5 needed real
infrastructure; 0, 4.1 and 4.2 have since been met (2026-08-30/31), 4.3
and 5 stay open. The public hostname is decided (2026-08-31,
owner Mark): `https://api.westerweel.work`, a Cloudflare Tunnel, primary,
with the Tailscale Funnel hostname kept up in parallel as a fallback — see
"Two supported topologies" below. Final retention and limit values are
still pending and are represented here as environment-tunable defaults, so
building now commits to nothing on those.

## Placement and stack

- Package `src/netnl/` in this repo. The CLI package stays untouched except
  where explicitly reused.
- Dependencies live in an **optional extra `api`**: `fastapi`, `pydantic>=2`,
  `uvicorn`. The core install stays stdlib-only; `uv sync --all-extras`
  brings the facade. CI installs all extras and runs one suite.
- **Reuse, don't rewrite:** the facade talks to the upstream instance through
  `internetnl_cli.client.BatchClient` — inheriting the injectable opener,
  the redirect refusal, the request-id validation and the leak-free error
  discipline. The facade constructs the CLI's `Config` from its own settings.
- Storage is SQLite (stdlib `sqlite3`), single file, WAL mode. PostgreSQL
  only if reality demands it later.

## Configuration (environment only, prefix `NETNL_`)

| Variable | Default | Meaning |
|---|---|---|
| `NETNL_UPSTREAM_ENDPOINT` | — (required) | Private batch instance base URL |
| `NETNL_UPSTREAM_USERNAME` | — (required) | Upstream batch credential (server-side only) |
| `NETNL_UPSTREAM_PASSWORD` | — (required) | idem |
| `NETNL_DB` | — (required) | SQLite database path |
| `NETNL_INSTANCE` | `netnl` | Provenance name, sent as `X-Netnl-Instance` header |
| `NETNL_RATE_LIMIT` | `10` | Submissions per credential per hour |
| `NETNL_MAX_DOMAINS` | `500` | Max domains per request |
| `NETNL_MAX_CONCURRENT` | `2` | Max non-terminal runs per credential |
| `NETNL_RESULT_RETENTION_DAYS` | `7` | Days a request stays retrievable |
| `NETNL_AUDIT_RETENTION_DAYS` | `90` | Days audit records are kept |
| `NETNL_METADATA_TTL` | `3600` | Seconds the metadata/report passthrough is cached |
| `NETNL_TIMEOUT` | `30` | Upstream HTTP timeout, seconds |
| `NETNL_SECURITY_CONTACT` | unset | Opt-in: when set, serves RFC 9116 `security.txt` at `GET /.well-known/security.txt` with `Contact: <value>` (e.g. `mailto:security@example.org`); unset means the path answers the ordinary 501 `not-implemented` catch-all, same as any other unmapped path |

Missing required variable → refuse to start, naming the variable. No
default endpoint anywhere; upstream must satisfy the CLI config rules
(https, or explicit `INTERNETNL_ALLOW_HTTP`-equivalent `NETNL_ALLOW_HTTP=1`
for the internal hop).

## HTTP surface (batch API v2 subset, HTTP Basic auth)

- `POST /requests` — validate v2 body (`type` web|mail, `domains`, optional
  `name`); enforce limits; submit upstream; issue facade id
  (`secrets.token_hex(16)` → matches `^[a-f0-9]{32}$`); audit; reply in
  upstream `RequestReply` shape with the facade id substituted.
- `GET /requests/{id}` — tenant check; upstream status; passthrough with id
  substituted.
- `GET /requests/{id}/results` — tenant check; passthrough, `domains`
  structurally unmodified (equal under canonical JSON — the reply passes a
  JSON parse/serialise, so "byte-identical" is pinned as: no key added,
  removed or rewritten); id substituted in the `request` object.
- `GET /metadata/report` — **authenticated** (HTTP Basic, same as every
  other route); passthrough, cached `NETNL_METADATA_TTL` seconds. No route
  in the v2 subset is anonymous.
- Every reply (success and error) carries `X-Netnl-Instance` plus a
  `X-Netnl-Notice` header stating independence from internet.nl — including
  the catch-all 500. Because Starlette's `ServerErrorMiddleware` sits outside
  the user middleware stack, the provenance headers are added by the 500
  handler itself (or an outer wrapper), not only by the middleware, so no
  error path escapes unlabelled.
- Every reply also carries a fixed, browser-hardening set of security
  headers: `Content-Security-Policy: default-src 'none'; frame-ancestors
  'none'; base-uri 'none'; form-action 'none'`, `X-Content-Type-Options:
  nosniff`, `Referrer-Policy: no-referrer`, `X-Frame-Options: DENY` — this
  facade never serves HTML to a browser, so the policy is maximally strict.
  No `Strict-Transport-Security`: TLS terminates in front of this process
  (Funnel/Cloudflare/an operator's own edge), and HSTS is that hop's
  responsibility, not this one's.
- `GET /.well-known/security.txt` (RFC 9116) — anonymous, opt-in via
  `NETNL_SECURITY_CONTACT` (see the configuration table above); unset, the
  path is not registered and falls through to the ordinary 501
  `not-implemented` catch-all, the same "acts like it does not exist" stance
  taken for the v2 subset, though `/health` is unconditionally anonymous
  while this route is anonymous only once an operator opts in.
- Any other path or method → v2-shaped error body
  (`{"api_version", "error": {"label", "msg"}}`), labels
  `not-implemented` (501), `unknown-request` (404), `bad-request` (400),
  `rate-limited` (429), `unauthorised` (401), `overloaded` (503, round-2,
  finding 1b — the concurrent-scrypt cap is saturated; distinct from
  `rate-limited`, which is a per-credential quota, because this can hit a
  caller who has never made a request before).
- Errors from upstream pass through with their status, **except** upstream
  401/403: those are the operator's problem, not the tenant's, and map to
  502 `upstream-error` so a tenant never mistrusts their own facade
  credential. Upstream transport failure → 502 with label
  `upstream-unreachable`, naming the upstream host only — never the
  credential.

## Concurrency and storage (review round 1 — the load-bearing fix)

The facade is multi-threaded (FastAPI runs sync handlers in a threadpool) and
public, so storage must be correct under concurrent requests, not only
sequentially.

- **One SQLite connection per request, never shared across requests.** A
  FastAPI dependency (`store.get_conn`) opens a connection at request start
  and closes it at request end. A connection *shared and used concurrently
  across requests* with `check_same_thread=False` is forbidden — CPython's
  per-connection statement cache and cursor state interleave rows between
  concurrent requests, which leaked one tenant's row (and `upstream_id`) to
  another. WAL mode makes many short-lived connections cheap and is the
  intended pattern.
  - **Round-4 fix (N1, pre-existing, HIGH): the per-request connection
    itself needs `check_same_thread=False`.** `check_same_thread=True`
    (sqlite3's default) is about a single connection object being touched
    from more than one *thread*, not from more than one *request* — a
    distinct axis from the bullet above. FastAPI resolves `get_conn` (a
    sync generator dependency) by running its body up to `yield` inside
    one `run_in_threadpool` call; the sync route handler that receives the
    yielded connection (and any other sync `Depends`, e.g.
    `auth.authenticate`) runs inside a *separate* `run_in_threadpool` call;
    the generator's post-response cleanup runs inside a third. Each can
    land on a different real OS worker thread from anyio's threadpool.
    With the strict default this raised sqlite3's `ProgrammingError` —
    surfaced as a generic 500 — the instant two of those three steps
    landed on different threads: measured on a real uvicorn server (not
    `TestClient`, whose portal serialises this away and so never showed
    it), 0 failures at 2 concurrent requests, 14/24 at 4, 85/96 at 16, and
    only 3 of 16 concurrent auth failures reaching the audit trail.
    `store.connect` now takes `allow_cross_thread` (default `False`,
    i.e. unchanged for every other caller — `admin.py`, the schema
    migration, `prune`, tests); `get_conn` is the one caller that passes
    `True`. This is safe *specifically* because the connection, though
    used from different threads, is never used *concurrently* by two of
    them within one request — FastAPI always finishes one
    `run_in_threadpool` call before starting the next for the same
    request — which is a different shape from the forbidden
    shared-across-requests case above. See `store.connect`'s docstring for
    the full reasoning and `tests/netnl/test_netnl_real_server.py` for the
    real-uvicorn regression test (`TestClient` cannot exercise this).
- **Limit enforcement is atomic — reserve, then call upstream.** Rate,
  size and concurrency are check-then-act and must not race. Inside a single
  `BEGIN IMMEDIATE` transaction the handler: (1) counts submits in the window
  and non-terminal runs for the credential, (2) rejects with 429 if at or
  over a limit, (3) otherwise inserts the audit `submit` row **and** a
  `requests` row in state `reserving` (facade id issued, `upstream_id` still
  NULL), then commits. The write lock serialises concurrent submits so the
  counts cannot be read stale. Only **after** the commit does the handler
  call upstream (outside the lock, so a slow network call never blocks other
  tenants); it then updates the reserved row with the real `upstream_id` and
  status. Size is checked before the transaction (no state needed). Result:
  the audit row exists before upstream is contacted — the rate counter can
  never be under-reported by an in-flight submit.
- A reserved row whose upstream submit never completed (crash between commit
  and update) stays `reserving`; it counts toward concurrency until pruned,
  and is retrievable only by its owner, showing status `reserving`. `prune`
  clears stale `reserving` rows older than a short fixed grace.
- **Accepted restrisico (round-2, finding 5): an orphaned upstream run.** A
  row still `reserving` when `prune` finds it proves only that *this facade*
  never recorded an `upstream_id` — not that the upstream submit never
  happened. The crash window is between the upstream instance accepting the
  request (HTTP 200, a real run now exists upstream) and
  `finalize_reservation` committing that run's id to the `requests` row. If
  the process dies in that window, the reservation is stranded exactly as
  above, `prune` deletes it after the grace, and the run — if it exists —
  is now unreachable *through the facade* forever: no `upstream_id` was
  ever stored, so nothing is left to poll it, fetch its results, or cancel
  it by. It keeps running upstream, invisible to every facade-side view.
  The minimal fix shipped for this (not a full fix — there is no
  `upstream_id` to recover after the fact): `prune` audits every stranded
  row it deletes individually, with its facade id, owning credential,
  domain count and original `submitted_at` (event `reserving-pruned`, see
  "Audit" below) — enough for an operator who notices an unexplained run on
  the upstream instance's own dashboard to correlate it back to a tenant
  and a submission time and follow up manually. Actually reclaiming or
  cancelling such a run is out of scope for this build.
  **Round-3 fix (security-L3):** the main result-retention delete only
  ever removes rows with `upstream_id IS NOT NULL` — without that scope, a
  `reserving` row stranded *longer* than the (much longer) result-retention
  window would be deleted there before the stranded-reservation audit ever
  ran, silently losing the one thing that could reconstruct it; a
  `reserving` row is now only ever removed by the dedicated
  stranded-reservation delete, always preceded by its audit. The
  stranded-reservation lookup also `LEFT JOIN`s `credentials` with
  `COALESCE(..., '<unknown>')` (round-3, reviewer-m12), so a request row
  whose `credentials` row is somehow missing is still audited (as
  `<unknown>`) rather than silently skipped by an inner join.

## Tenancy and identity

- Table `credentials(id, username UNIQUE, password_hash, created_at,
  revoked_at NULL)`. Hashing: stdlib `hashlib.scrypt` with per-credential
  salt; comparison via `hmac.compare_digest`. Revocation = setting
  `revoked_at`; enforcement is immediate (checked per request). The DB file
  is created with mode `0600` (owner-only) — it holds password hashes, the
  id-map and the audit trail.
- **Authentication cost is bounded on two axes (round-2, finding 1).**
  scrypt (`n=2^14, r=8`, ~tens of milliseconds and ~16 MB per call) is
  deliberately expensive per verification to resist offline brute force,
  which means it must be paid *only* where it buys something:
  - **Per request:** an unknown username still costs one scrypt
    computation, against a fixed dummy salt, so "unknown user" and "wrong
    password" take the same time (defeats username enumeration via a
    timing side channel). A request whose `Authorization` header is
    missing or does not even parse as `Basic base64(user:pass)` carries no
    username to enumerate against, so it is rejected with **no** scrypt
    call at all — paying that cost there would only be a free CPU/memory
    amplifier for an unauthenticated caller, the exact cheap-DoS shape the
    review flagged.
  - **Across requests:** `netnl.auth` bounds how many scrypt verifications
    may run *concurrently* to a small cap derived from the host's CPU
    count (`max(4, min(8, os.cpu_count() or 4))` — floored at 4 so a
    single-core-ish container does not make every legitimate concurrent
    login queue, capped at 8), enforced with a semaphore. **Round-3 fix
    (security-M1):** the semaphore used to fail fast (non-blocking) the
    instant it saturated — measured, this produced 23 spurious 503s on
    this project's own legitimate concurrent-request tests, since a
    threadpool of ~40 sync handlers can easily have more requests reach
    `authenticate` at the same instant than the cap without anything
    being wrong. It now waits up to a short, fixed timeout (1.0s) for a
    slot before giving up. This bounded wait is safe *because* it is
    bounded: the only callers that can ever queue on it are requests
    already admitted into the ASGI server's own worker threadpool (tens of
    threads, not an unbounded number of TCP connections), so the worst
    case is every worker thread waiting ~1s, not unboundedly many callers
    piling up. 503 `overloaded` (`Retry-After: 1`; listed in
    `netnl.replies.LABEL_STATUS`) now means *sustained* saturation — the
    cap still full after the wait — not an ordinary burst that clears
    within it. This cap is a **single-process** quota: each `netnl-serve`
    process gets its own semaphore, so N worker processes/replicas
    multiply the effective cap by N — an operator sizing the edge rate
    limit (below) should account for that rather than treat this number as
    a cluster-wide ceiling. This is a backstop inside the process;
    sustained brute-force or volumetric abuse is expected to be absorbed
    at the edge before it reaches this bound — see
    `docs/how-to/deploy-facade.md`'s brute-force/rate-limiting note per
    topology, including the note that topology 1's Tailscale Funnel
    fallback is a second public ingress a Cloudflare edge rule does not
    cover.
  - The `Basic` auth-scheme token is matched case-insensitively
    (`basic`/`BASIC`/`Basic`/…), per RFC 7617 — only the scheme token is
    case-folded, not the credentials that follow it.
- Table `requests(facade_id UNIQUE, upstream_id NULL, credential_id,
  request_type, domain_count, submitted_at, last_status, finished_at NULL)`.
  `upstream_id` is NULL only while a row is `reserving` (see the atomic-limit
  flow above). Upstream ids never leave the server.
- Foreign or unknown facade id → 404 `unknown-request`, indistinguishable
  from nonexistent. Expired (past result retention) → the row is pruned, so
  the same 404.
- Operator CLI, same package: `netnl-admin user add <name>` (prints a
  generated password **once**, to stdout, never stored in plain),
  `user revoke <name>`, `user list`, and `prune` (applies both retention
  windows). Scheduling `prune` is the deployment's job (cron), not a thread.

## Limits

All three are enforced inside the single `BEGIN IMMEDIATE` reservation
transaction described under "Concurrency and storage", so parallel submits
from one credential cannot each read a stale count.

- Size: `len(domains) > NETNL_MAX_DOMAINS` → 400 naming the limit; also a
  per-domain length cap and a total request-body size cap, so a tenant cannot
  push arbitrarily large or malformed strings through to the private instance
  (a domain is length-bounded and must be a plausible hostname token — no
  whitespace, no control characters).
- **No internal targets (anti-SSRF).** The facade fronts a scanner that
  resolves and connects to whatever it is given, so a domain must be a
  public, multi-label FQDN: reject IP-address literals (v4/v6, and
  decimal/octal/hex integer forms), single-label names (`localhost`), names
  under a reserved or internal-use suffix (`.localhost`, `.local`,
  `.internal`, `.intranet`, `.corp`, `.home`, `.lan`, `.localdomain`), and
  the well-known cloud-metadata hostnames (`metadata.google.internal` and
  the like) — all matched case-insensitively per label. Any such target is
  out of scope for a hostname token — the CLI passes hostnames, not addresses. A rejected
  target → 400 `bad-request`, nothing submitted upstream. This stops a tenant
  using the facade as a pivot to probe the internal network (`10.0.0.0/8`,
  `127.0.0.0/8`, `169.254.169.254`, etc.).
- Rate: submissions per credential in the past hour, counted from the audit
  table inside the transaction; at the limit → 429, upstream untouched. The
  audit `submit` row is written at reservation time, **before** upstream is
  ever called (see "Concurrency and storage" above), so the rate counter
  includes every reservation attempt regardless of whether its upstream call
  later succeeds or fails — accepted (round-2 review note): this protects
  the upstream instance itself (an unreachable/erroring upstream must not
  turn into an unlimited retry hammer), at the cost that a tenant who
  submits repeatedly against a dead upstream burns through their own hourly
  rate limit and is blocked for the remainder of the window — a real but
  bounded and self-inflicted cost, not one the facade can shift onto anyone
  else.
- Concurrency: non-terminal rows (`last_status` not in
  done/error/cancelled, and `reserving` counts as in-progress) for this
  credential; at the limit → 429 with label `rate-limited` and a msg naming
  concurrency. Refreshing stale non-terminal rows from upstream is a separate
  concern kept out of the write transaction.
  - **Round-2 fix, finding 3, and its upper bound (round-3, reviewer-
    M5/M6):** a single row's upstream status-refresh call failing must not
    fail the whole submit with 502 — the row is skipped (conservatively
    still counted as non-terminal) and the submit proceeds. Stated
    explicitly: if refresh fails *permanently* for a credential (a
    sustained upstream outage, or upstream that never marks these rows
    terminal), every one of that credential's `max_concurrent` rows stays
    non-terminal indefinitely from this facade's point of view, and every
    further submit from that credential answers 429 — never a crash, never
    a silent bypass of the limit. That block's *maximum* duration is
    bounded, not open-ended: the tenant is unblocked once those rows fall
    out of non-terminal either because upstream itself later reports a
    terminal status, or because `prune` removes them once they pass
    `NETNL_RESULT_RETENTION_DAYS` — so the deploy's retention window and
    prune cadence is the real upper bound on how long a persistent upstream
    problem can block one tenant.

## Audit

- Table `audit(id INTEGER PRIMARY KEY, at, credential, event, facade_id,
  domain_count, detail)` — events: `submit`, `user-add`, `user-revoke`,
  `prune`, `auth-failure` (round-2, finding 2), `reserving-pruned`
  (round-2, finding 5). `detail` is free-form, event-specific context (the
  HTTP route plus the aggregated failure count, `"<route> failures=<n>"`,
  for `auth-failure` — round-3, see below; the original `submitted_at` for
  `reserving-pruned`) added in round 2 rather than a new column per event
  type; it never holds a password or any other credential secret. A
  database created before round 2 gets the column added in place by
  `store.migrate` (`ALTER TABLE ... ADD COLUMN`), tolerant of a
  concurrent-startup race on that same `ALTER` (round-3, security-L2), so
  upgrading needs no manual migration step.
- Append-only enforced in the schema: `BEFORE UPDATE` and `BEFORE DELETE`
  triggers that `RAISE(ABORT)`; `prune` removes (a) `requests` rows past
  `NETNL_RESULT_RETENTION_DAYS`, (b) **stale `reserving` rows** older than a
  short fixed grace (`NETNL_RESERVING_GRACE_SECONDS`, default 300) — a
  reservation whose upstream submit never completed must not pin a
  concurrency slot for days, audited individually per row as
  `reserving-pruned` before deletion (see "Concurrency and storage", the
  finding-5 restrisico note) — and (c) audit rows older than
  `NETNL_AUDIT_RETENTION_DAYS`, via a dedicated path that drops and recreates
  the append-only triggers inside one transaction, and writes a `prune` audit
  record stating how many rows went.
- Domain **lists** are not stored anywhere in the facade; only counts.
- **Failed authentication is audited (round-2, finding 2; hardened
  round-3, security-H1).** Every rejected `Authorization` —
  missing/unparseable header, unknown username, wrong password, or a
  revoked credential — is a detection signal worth keeping, but writing
  one row per failed attempt would make `audit` itself an unbounded write
  sink for the exact kind of flood finding 1 bounds on the CPU/memory
  side. `netnl.auth` aggregates failures **in memory**, keyed on
  (sanitised username or `NULL`, route) per wall-clock minute, and writes
  **one summarising row** (`event = auth-failure`, `detail` =
  `"<route> failures=<n>"`, `domain_count` left `NULL`) per key per
  minute, regardless of how many failures actually occurred in that
  window. The count intentionally lives in `detail`, not `domain_count` —
  that column means "domains in a submission", an unrelated concept an
  `auth-failure` row must not repurpose (round-3, reviewer-L4).
  - **The aggregator's own size is genuinely bounded (round-3, security-
    H1a — the round-2 version was not):** a bucket-per-(username, route)
    scheme with no cap on the number of *distinct* keys grows exactly as
    fast as an attacker can invent unique, throwaway usernames — measured
    at ~5.5M audit rows/day for that pattern, the opposite of "bounded". A
    hard cap (`_MAX_BUCKETS = 512`) limits the number of live buckets; once
    reached, a *new* key for a route that already has buckets collapses
    into a single per-route overflow bucket (`credential = "<other>"`)
    with its own running count, instead of minting another bucket. Both
    the in-memory dict and the audit rows a single minute can ever produce
    are therefore bounded by `_MAX_BUCKETS` plus this facade's (small,
    fixed) number of routes — never by how many distinct usernames an
    attacker tries. The sweep that flushes stale buckets still runs on
    every authenticated request (success or failure), so its size is also
    capped at "distinct keys seen in the current minute", now doubly
    bounded by the cap above.
    - **Accepted residual risk (round-4, N2, Low): the overflow bucket is
      itself an evasion route for a *targeted* attacker, distinct from the
      volumetric flood it was built to bound.** Once the 512-bucket cap is
      reached for a minute, every *further* distinct username collapses
      into the shared per-route `"<other>"` bucket — including one an
      attacker deliberately aims at a specific real tenant's username. A
      caller who first burns through 512 disposable, never-reused usernames
      against a route in the same minute (well under a minute of traffic at
      any realistic rate) can then brute-force that one real tenant's
      credential on the same route; every one of those targeted attempts is
      still audited, but only as an indistinguishable count inside
      `"<other>"`, not attributed back to the targeted username the way an
      attack that stayed under the cap would be. Total *volume* per route
      per minute is still visible and correct (that is what the cap
      protects) — what is lost is *which* username a targeted burst of
      failures, once folded into the overflow bucket, was actually aimed
      at. Accepted for this build: the cap's job is bounding the
      aggregator's own memory/write cost against an unbounded flood of
      distinct usernames, not attributing every failure to its real target
      under adversarial conditions; an operator relying on `auth-failure`
      rows to attribute a targeted brute-force to a specific username
      should not treat `"<other>"` as "nothing of note happened" — see the
      attribution caveat above, which already says an `auth-failure` row's
      `credential` (`"<other>"` included) is never trustworthy tenant
      attribution.
      - **Noted follow-up, not built here:** a per-route *sub-quota*
        inside the overflow bucket — e.g. reserving a handful of the 512
        slots per route for usernames that have *already* accrued failures
        this minute (so a target being actively brute-forced keeps its own
        bucket even after the cap is otherwise reached), or keying the
        overflow itself on a coarser signal (e.g. source IP) instead of
        collapsing every over-cap username into one shared bucket — is
        worth a future round if targeted-evasion detection becomes a real
        requirement. Not built now: it adds real complexity (a second
        eviction policy layered on the first) for a Low-severity residual
        risk against a facade with no operational history yet of being
        targeted this way.
  - **The flush is one transaction, never held under the aggregator's own
    lock, and never fails a request (round-3, security-H1b/H1d):** the
    round-2 version ran one fsync'd, autocommit `INSERT` per stale bucket
    *while holding the aggregator's lock* — measured at ~5.5s of total
    auth-processing stall for 10k buckets, repeatable every minute the
    lock was held that long. The lock now guards only the (cheap) pop of
    stale entries from the dict; every popped entry is written in a single
    `BEGIN IMMEDIATE` + `executemany` transaction outside the lock
    (measured: <100ms for 10k rows), wrapped in `try`/`except` — a failing
    write (e.g. racing `prune`'s own `BEGIN IMMEDIATE`) is logged with the
    aggregated counts and the window's tally is dropped, never re-raised,
    since this runs unconditionally on every request including a
    perfectly valid, successfully-authenticated one.
  - **The timestamp is the failure window's own time, not the flush's
    (round-3, security-H1c):** a bucket flushed minutes or hours late
    (only a later failure or authenticated call triggers a flush)
    previously back-dated its `at` to whenever that trigger happened; it
    is now `datetime.fromtimestamp(minute * 60, timezone.utc)`, the
    bucket's own minute.
  - The username is sanitised (printable characters only, capped length)
    before it is used as a key or written anywhere — never trusted
    verbatim from an attacker-controlled header; the same sanitizer/cap
    also applies to the route-path fallback used when routing metadata is
    unavailable (round-3, security-L1), since that fallback is just as
    attacker-influenced. The password is never recorded, in any form.
  - **Attribution caveat, worth restating for whoever reads this table:**
    an `auth-failure` row's `credential` value — including the literal
    `"<other>"` overflow marker — is attacker-supplied input from a
    request that, by definition, failed to authenticate. An operator must
    never read it as trustworthy tenant behaviour; it is a detection
    signal about the *route*, not a verified claim about *who* sent it.

## Deployment (section 4.1)

A small compose unit that runs the facade and an edge proxy alongside — but
not inside — the upstream batch instance's own stack. It lives in `deploy/`.

- **Files:** `deploy/Dockerfile` (facade image), `deploy/compose.yaml`,
  `deploy/Caddyfile` (edge TLS + reverse proxy), `deploy/.env.example`, and
  `docs/how-to/deploy-facade.md`. `deploy/.env` is gitignored — the real
  upstream credential never enters the repo.
- **Facade image:** `python:3.12-slim`, dependencies installed with `uv`
  (`.[api]` extra), runs as a **non-root** user, launches `netnl-serve`
  bound to `0.0.0.0` *inside the container only*. It publishes **no** host
  port — it is reachable solely on the compose networks.
- **Base images are pinned to their multi-arch index digest** (facade base,
  the uv source image, and Caddy), with the tag kept in a comment; a bump
  moves both together. This makes the build reproducible; refreshing a digest
  is a deliberate, reviewable change.
- **Edge:** Caddy, the only service with published ports (80/443), obtains
  and renews TLS automatically for `NETNL_PUBLIC_HOST` (owner supplies the
  hostname via env — no hostname is hardcoded, consistent with "endpoint is
  configuration"), and reverse-proxies to `netnl:8000`.
- **The batch instance is not in this compose.** It runs its own upstream
  stack. The facade joins an **external** docker network the instance shares
  (`NETNL_UPSTREAM_NETWORK`, e.g. the instance's internal bridge); the
  instance must publish no public API port, so the only public path to it is
  through the facade. `NETNL_UPSTREAM_ENDPOINT` points at the instance over
  that internal network.
- **State:** a named volume holds the SQLite DB (`NETNL_DB` inside the
  container). Created `0600` by the app.
- **Secrets:** `env_file: .env` for the run; the file carries
  `NETNL_UPSTREAM_PASSWORD` and is never committed. Docker/compose secrets
  are noted in the how-to as the hardening step for a real deployment.
- **prune:** a `prune` compose *profile* service that runs `netnl-admin
  prune` once and exits, plus a documented host-cron / systemd-timer line
  (`docker compose run --rm prune`) — cadence bounds the retention grace
  (task 4.1 note). No in-process scheduler.
- This is a homelab-grade recipe, stated plainly; it is not an SLA.

### Two supported topologies

The compose recipe above co-locates facade and instance. The deployment we
actually run separates them, because the batch instance needs a fixed public
IPv4+IPv6 (it measures from its own address) which a NAT'd homelab cannot
give:

1. **Instance on a VPS, facade in the homelab K8s cluster.** The upstream
   batch instance runs its own Compose stack on a VPS with a fixed public
   IPv4+IPv6 (see `docs/how-to/deploy-instance-vps.md`). The facade runs in
   the homelab as an ArgoCD app and reaches the instance over the tailnet
   (`NETNL_UPSTREAM_ENDPOINT` = the instance's tailnet address; the VPS does
   not publish its batch API publicly). The facade is exposed publicly via
   **two parallel paths**: a **Cloudflare Tunnel** on a branded hostname
   (ours: `https://api.westerweel.work`) as the primary path, and the
   **Tailscale Funnel** `*.ts.net` hostname kept up in parallel as a
   fallback. Both terminate TLS themselves, so no Caddy edge is needed in
   K8s either way. The compose `Caddyfile`/`edge` service belongs to
   topology (2) only.
2. **Co-located** (the compose recipe): facade + instance on one host with a
   public IP, Caddy at the edge. Simpler, but needs a public-IP host that
   also runs the full instance stack.

### Facade image and liveness (for the K8s topology)

- **Image:** `deploy/Dockerfile` is built and pushed to
  `ghcr.io/mwest2020/internetnl-cli` by a CI workflow, tagged `sha-<short>`
  (and `latest` on `main`), matching the ecosystem's per-SHA image
  convention. The homelab manifests pin a specific `sha-` tag.
- **Liveness:** the facade exposes `GET /health`, an anonymous, static
  `{"status": "ok"}` that touches neither the upstream instance nor the DB
  and discloses no version/host/credential — it exists so K8s
  liveness/readiness probes have a target without credentials. It is not part
  of the v2 measurement subset.

### VPS provisioning (Hetzner) — for topology 1

The batch instance needs a fixed public IPv4 + IPv6 (it measures from its own
address), which the NAT'd homelab cannot give — confirmed empirically (no
working public IPv6, shared NAT IPv4). So the instance runs on a Hetzner Cloud
VPS. Provisioning lives in `deploy/vps/`:

- **`deploy/vps/cloud-init.yaml`** — Hetzner user-data run on first boot:
  system update, Docker CE + compose plugin, Tailscale; a non-root user; SSH
  hardening (no root login, key-only); a host firewall that allows SSH
  (restricted), the Tailscale UDP port, and a clearly-marked slot for the
  Internet.nl-required public ports (notably authoritative DNS) per upstream's
  batch guide — the batch **API** itself is never opened publicly, only
  reachable over the tailnet. Joins the tailnet with `${TS_AUTHKEY}` (an
  ephemeral, tagged, pre-auth key — injected at create time, never committed).
- **`deploy/vps/create-vps.sh`** — `hcloud` script: creates the server
  (type/image/location configurable, IPv6 on, a Hetzner firewall, the operator's
  SSH key) with the cloud-init as user-data; reads `HCLOUD_TOKEN` and
  `TS_AUTHKEY` from the environment; prints the public IPv4/IPv6 and the
  tailnet name. No secret is hardcoded.
- **`docs/how-to/deploy-instance-vps.md`** carries the runbook, extended with:
  create the VPS, then the Internet.nl-specific config that only the operator
  can do (upstream batch deployment guide, the instance `.env`, DNS delegation,
  a batch user via `user_manage.sh`), then wire the facade — create the
  `netnl-upstream` Secret from that batch user and set `NETNL_UPSTREAM_ENDPOINT`
  to the VPS's tailnet address — and finally run `scripts/acceptance.sh`.
- The script provisions the host and lays out the stack scaffold; the
  Internet.nl stack config (domain, DNS, batch user) stays upstream's guide,
  not reproduced here. Creating billable cloud resources is the operator's
  action (their `HCLOUD_TOKEN`), never automatic.

## Testing constraints

- No network I/O: FastAPI's in-process `TestClient`; upstream faked with the
  existing `tests/fakes.FakeOpener` through the `BatchClient` seam.
- The `$HOME`-isolation autouse fixture covers the new tests unchanged;
  SQLite files live under `tmp_path`.
- Leak test: upstream credential and its base64 form appear in no response
  body, header, or captured log across a 401-from-upstream and a transport
  failure.
- Tenancy test: credential B on credential A's id → 404 with the
  `unknown-request` label; response identical to a random unknown id.
- Append-only test: direct `UPDATE`/`DELETE` on audit raises; `prune`
  works and audits itself.
- Sanitisation: facade replies are JSON-only (no terminal rendering), so the
  CLI's sanitiser is not duplicated here; the provenance headers are fixed
  ASCII from configuration.

## Exit criteria for this build (sections 1–3)

`sh scripts/verify.sh` green with the full suite (CLI + facade);
`uv run --all-extras pytest -q` green; the CLI's own tests untouched and
passing; tasks 1.x, 2.x, 3.x ticked. Sections 0, 4, 5 remain open — the
facade is done-in-code, not yet deployed.
