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
  byte-identical; id substituted in the `request` object.
- `GET /metadata/report` — passthrough, cached `NETNL_METADATA_TTL` seconds.
- Every reply (success and error) carries `X-Netnl-Instance` plus a
  `X-Netnl-Notice` header stating independence from internet.nl.
- Any other path or method → v2-shaped error body
  (`{"api_version", "error": {"label", "msg"}}`), labels
  `not-implemented` (501), `unknown-request` (404), `bad-request` (400),
  `rate-limited` (429), `unauthorised` (401).
- Errors from upstream pass through with their status; upstream transport
  failure → 502 with label `upstream-unreachable`, naming the upstream host
  only — never the credential.

## Tenancy and identity

- Table `credentials(id, username UNIQUE, password_hash, created_at,
  revoked_at NULL)`. Hashing: stdlib `hashlib.scrypt` with per-credential
  salt; comparison via `hmac.compare_digest`. Revocation = setting
  `revoked_at`; enforcement is immediate (checked per request).
- Table `requests(facade_id UNIQUE, upstream_id, credential_id, request_type,
  domain_count, submitted_at, last_status, finished_at NULL)`. Upstream ids
  never leave the server.
- Foreign or unknown facade id → 404 `unknown-request`, indistinguishable
  from nonexistent. Expired (past result retention) → the row is pruned, so
  the same 404.
- Operator CLI, same package: `netnl-admin user add <name>` (prints a
  generated password **once**, to stdout, never stored in plain),
  `user revoke <name>`, `user list`, and `prune` (applies both retention
  windows). Scheduling `prune` is the deployment's job (cron), not a thread.

## Limits

- Rate: submissions per credential in the past hour, counted from the audit
  table; at the limit → 429, upstream untouched.
- Size: `len(domains) > NETNL_MAX_DOMAINS` → 400 naming the limit.
- Concurrency: non-terminal rows (`last_status` not in done/error/cancelled)
  for this credential; before rejecting, refresh those rows' status from
  upstream (bounded by `NETNL_MAX_CONCURRENT`, so cheap) — at the limit →
  429 with label `rate-limited` and a msg naming concurrency.

## Audit

- Table `audit(id INTEGER PRIMARY KEY, at, credential, event, facade_id,
  domain_count)` — events: `submit`, `user-add`, `user-revoke`, `prune`.
- Append-only enforced in the schema: `BEFORE UPDATE` and `BEFORE DELETE`
  triggers that `RAISE(ABORT)`; `prune` removes only `requests` rows and
  audit rows older than `NETNL_AUDIT_RETENTION_DAYS` via a dedicated path
  that drops and recreates the triggers inside one transaction — and writes
  a `prune` audit record stating how many rows went.
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
