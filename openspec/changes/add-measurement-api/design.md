# Design: add-measurement-api (netnl)

Pins the buildable surface for sections 1–3 of `tasks.md` — the facade code
and its tests, which need no live instance (the upstream client is faked at
the same opener seam the CLI uses). Sections 0, 4 and 5 need real
infrastructure and stay open. Owner decisions still pending (hostname, final
retention and limit values) are represented here as environment-tunable
defaults, so building now commits to nothing.

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
- Any other path or method → v2-shaped error body
  (`{"api_version", "error": {"label", "msg"}}`), labels
  `not-implemented` (501), `unknown-request` (404), `bad-request` (400),
  `rate-limited` (429), `unauthorised` (401).
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

- **One SQLite connection per request, never shared across threads.** A
  FastAPI dependency opens a connection at request start and closes it at
  request end; `check_same_thread` stays at its safe default. A shared
  connection with `check_same_thread=False` is forbidden — CPython's
  per-connection statement cache and cursor state interleave rows between
  concurrent threads, which leaked one tenant's row (and `upstream_id`) to
  another. WAL mode makes many short-lived connections cheap and is the
  intended pattern.
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

## Tenancy and identity

- Table `credentials(id, username UNIQUE, password_hash, created_at,
  revoked_at NULL)`. Hashing: stdlib `hashlib.scrypt` with per-credential
  salt; comparison via `hmac.compare_digest`. Revocation = setting
  `revoked_at`; enforcement is immediate (checked per request). The DB file
  is created with mode `0600` (owner-only) — it holds password hashes, the
  id-map and the audit trail.
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
  table inside the transaction; at the limit → 429, upstream untouched.
- Concurrency: non-terminal rows (`last_status` not in
  done/error/cancelled, and `reserving` counts as in-progress) for this
  credential; at the limit → 429 with label `rate-limited` and a msg naming
  concurrency. Refreshing stale non-terminal rows from upstream is a separate
  concern kept out of the write transaction.

## Audit

- Table `audit(id INTEGER PRIMARY KEY, at, credential, event, facade_id,
  domain_count)` — events: `submit`, `user-add`, `user-revoke`, `prune`.
- Append-only enforced in the schema: `BEFORE UPDATE` and `BEFORE DELETE`
  triggers that `RAISE(ABORT)`; `prune` removes (a) `requests` rows past
  `NETNL_RESULT_RETENTION_DAYS`, (b) **stale `reserving` rows** older than a
  short fixed grace (`NETNL_RESERVING_GRACE_SECONDS`, default 300) — a
  reservation whose upstream submit never completed must not pin a
  concurrency slot for days — and (c) audit rows older than
  `NETNL_AUDIT_RETENTION_DAYS`, via a dedicated path that drops and recreates
  the append-only triggers inside one transaction, and writes a `prune` audit
  record stating how many rows went.
- Domain **lists** are not stored anywhere in the facade; only counts.

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
