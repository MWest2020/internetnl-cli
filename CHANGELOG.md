# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[SemVer](https://semver.org/).

## [Unreleased]

### Added

- `INTERNETNL_CREDENTIAL`, a single `username:password` alternative to
  `INTERNETNL_USERNAME`/`INTERNETNL_PASSWORD` (split on the first `:`,
  so a password containing one still works); set either form, never
  both — a silent precedence would mask a misconfiguration. The
  composite action gained a matching `credential` input (preferred
  path; `username`/`password` remain a supported alternative), with
  the "Validate inputs" step failing closed unless exactly one form is
  given. The facade's own HTTP Basic wire protocol is unchanged; only
  the CLI/action-facing credential UX collapsed from two secrets to
  one.
- An opt-in, anonymous demo route family on the `netnl` facade
  (`openspec/changes/add-demo-run`): `POST /demo/requests` accepts exactly
  `{"domain": "example.nl"}` (pydantic `extra="forbid"` makes a list or a
  `type` field structurally impossible), `GET /demo/requests/{id}` and
  `.../results` mirror the authenticated shape, owner-scoped to one
  operator-issued credential row (`NETNL_DEMO_TENANT`) that nobody ever
  authenticates as — revoking or never issuing it is the entire kill
  switch (503 `demo-unavailable`). Never touches authentication: no
  `Authorization` header is read and no password-hashing computation is
  ever invoked on this path. Bounded three ways: the demo tenant's own
  rate/concurrency limit via the facade's existing atomic
  `limits.reserve_submission` (so there is exactly one rate-limiting
  mechanism, not two), a per-IP-bucket hourly cap (client IP from a
  configurable header, `/32`/`/64`-generalised, one shared bucket for
  anything unparseable), and a per-domain cooldown that never returns an
  existing `request_id`. CORS is scoped to exactly one configured origin
  (never echoed, never combined with credentials), with explicit `OPTIONS`
  routes answering 204 so a browser preflight does not hit the 501
  catch-all. A successful demo submission writes exactly one audit row,
  shaped identically to a tenant submission (`event=submit`,
  `credential=<demo tenant>`, `domain_count=1`) — no visitor IP, `Origin`,
  or submitted domain is ever written to disk or a log line, on any path,
  accepted or rejected, proven by grepping the raw database file and
  captured logs across every rejection reason. Its own retention window
  (`NETNL_DEMO_RETENTION_HOURS`, default 24h) is applied on the existing
  `netnl-admin prune` pass, reported separately from the tenant retention
  counters. New docs:
  [`docs/how-to/demo-run.md`](docs/how-to/demo-run.md) (enabling,
  issuing/discarding the borrowed credential, the smoke check, the kill
  switch) and
  [`docs/reference/demo-api.md`](docs/reference/demo-api.md) (the page
  contract the dark-launched demo page relies on). The BMC-bridge that
  would turn a demo visitor into a real tenant is a separate, later
  change and is not built or stubbed here. Post-review hardening pass on
  the same (still unreleased) demo family: the per-IP and per-domain-
  cooldown bounds are now claimed atomically (proven race-free under real
  concurrency, not just `TestClient`); a non-polled run whose upstream
  status went terminal is refreshed before the next reservation, instead
  of occupying a concurrency slot until the retention window prunes it;
  every upstream-originated error and every aggregate-cap 429 reaching a
  demo reply is now a fixed, host-free, tenant-number-free visitor
  literal; a new per-IP poll budget (`NETNL_DEMO_POLLS_PER_IP_PER_HOUR`)
  bounds anonymous status/results polling, and a status poll of an
  already-terminal row is answered from the store with no upstream call;
  `netnl-admin user reissue <name>` re-keys an existing credential row in
  place (revoked or not), the kill switch's previously-missing "turn it
  back on" half.
- `action.yml`: a composite GitHub Action wrapping `internetnl submit`,
  installed from the same ref as the action itself (`uses:
  MWest2020/internetnl-cli@<ref>`). Inputs cover `hosts`/`file`, `type`,
  `fail-on-scored`, `name`, `allowlist` and the `INTERNETNL_*` credential
  trio; the password only ever reaches the process via an environment
  variable, never interpolated into a `run:` line. `fail-on-scored`
  fails closed on anything other than exactly `true`/`false`, and a `--`
  separator keeps a hostname that starts with `-` from being parsed as a
  flag. The CLI's own exit code decides the step's outcome, and the
  `--json` output path is exposed as the `results-path` output. See
  [`docs/how-to/ci.md`](docs/how-to/ci.md) for the GitHub Actions and
  plain-CLI (GitLab CI et al.) recipes and the gate's exit-code
  semantics.
- `.github/workflows/action-smoke.yml`: a smoke workflow exercising the
  action's own input-validation failure paths (no `hosts`/`file`, an
  invalid `fail-on-scored` value) without any real internet.nl-compatible
  API measurement. Each job asserts both that the step failed and that
  `internetnl` never made it onto `PATH`, i.e. that the run never
  reached "Install uv"/the install step — evidence that the failure is
  in input validation, not a later network/transport path.
- `.github/FUNDING.yml`: a Buy Me a Coffee sponsor button.
- [`docs/how-to/supporter-key.md`](docs/how-to/supporter-key.md): the
  issuance model for a lifetime `netnl` tenant credential after a small
  donation — beta, best-effort, no SLA, with the existing per-tenant
  rate limit as the fair-use mechanism. The donation link
  (<https://buymeacoffee.com/mark.westerweel>) is now live; issuance
  itself is still a manual, out-of-band `netnl-admin` process.

### Fixed

- The CLI now sends a `User-Agent: internetnl-cli/<version>` header on every
  request instead of letting `urllib` fall back to its default
  `Python-urllib/x.y` string — Cloudflare's bot protection in front of the
  primary batch endpoint was blocking the CLI's default `urllib`
  `User-Agent` with an HTTP 403.
- `action.yml`: the `password` input's description no longer contains a
  `${{ secrets.INTERNETNL_PASSWORD }}` example. GitHub's manifest
  validator parses `${{ }}` expressions inside description strings too
  when a remote action is loaded (`uses: MWest2020/internetnl-cli@<ref>`),
  and `secrets` is not a valid context there — this made the action fail
  to load entirely with "Unrecognized named-value: 'secrets'". Loading
  the action locally via `uses: ./` does not exercise this validation
  path, which is why the smoke workflow missed it.
- `netnl` facade hardening from two rounds of post-build review:
  - Unauthenticated scrypt DoS: a missing/unparseable `Authorization`
    header now fails fast with 401 and never touches scrypt; concurrent
    scrypt verifications are capped (`max(4, min(8, cpu_count))`,
    process-local) by a semaphore that waits briefly for a slot before
    answering 503 `overloaded` (with `Retry-After: 1`) on *sustained*
    saturation — a non-blocking version of this cap measured 23 spurious
    503s on the project's own legitimate concurrent-request tests, since
    fixed. `overloaded` is now listed in `netnl.replies.LABEL_STATUS`.
  - Failed authentication is audited (sanitised username, never the
    password, plus the route), aggregated per minute per
    (username, route) with a hard cap (512 buckets, plus one overflow
    bucket per route beyond that) so neither the in-memory aggregator nor
    the audit table it flushes to can grow past a bounded size regardless
    of how many distinct usernames an attacker cycles through. The flush
    itself is a single transaction per sweep (not one autocommit `INSERT`
    per bucket — measured ~5.5s of auth-processing stall for 10k buckets
    before this fix, <100ms after), never raises (a failing flush is
    logged and the window's tally is dropped, never turned into a 500 on
    a legitimate request), and timestamps each row with the failure
    window's own time rather than whenever the flush happened to run. The
    failure count now lives in `detail` (`"<route> failures=<n>"`), not
    `domain_count`. A database created before the `audit.detail` column
    existed is upgraded in place by `store.migrate`'s
    `ALTER TABLE audit ADD COLUMN detail TEXT`, now tolerant of a
    concurrent-startup race on that same `ALTER`.
  - `refresh_stale_non_terminal` no longer fails a submit with 502 when a
    row's upstream status call errors; a credential whose refreshes fail
    *permanently* is blocked with 429 (never a crash) for at most as long
    as `NETNL_RESULT_RETENTION_DAYS` and the deploy's prune cadence allow.
  - `retention.prune`'s stranded-reservation audit now runs against rows
    the main retention delete can no longer have already removed out from
    under it, and tolerates a missing `credentials` row instead of
    silently dropping that entry.
  - The `Basic` auth scheme is matched case-insensitively per RFC 7617.
  - `docs/how-to/deploy-facade.md` and `docs/how-to/beta.md` document the
    503 `overloaded` status and note that topology 1's Tailscale Funnel
    fallback is a second public ingress a Cloudflare edge rate-limiting
    rule does not cover.

### Added

- The `netnl` facade now sends a fixed set of security headers on every
  reply (success and error alike) — `Content-Security-Policy`,
  `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`,
  `X-Frame-Options: DENY` — and, opt-in via the new `NETNL_SECURITY_CONTACT`
  variable, an RFC 9116 `security.txt` at `GET /.well-known/security.txt`.
  Prompted by a live Internet.nl webtest against the facade flagging
  `web_appsecpriv_csp`, `web_appsecpriv_x_content_type_options` and
  `web_appsecpriv_securitytxt` as "bad".
- `netnl`, an independent batch API v2 facade (`src/netnl/`, console
  scripts `netnl-serve`/`netnl-admin`) fronting a private batch instance:
  tenant credentials with immediate revocation, per-credential rate/size/
  concurrency limits, an append-only SQLite audit trail, and a
  `netnl-admin` CLI for credential issuance and retention. Facade ids never
  reveal the upstream instance's own ids; every reply carries a provenance
  header naming the facade as an independent instance, affiliated with
  neither internet.nl nor Platform Internetstandaarden. The existing
  `internetnl` CLI works against it unchanged (only its `INTERNETNL_*`
  variables differ). Hardened through the review chain: one SQLite connection
  per request (no cross-tenant row bleed under concurrency), atomic
  reserve-then-submit limit enforcement, anti-SSRF domain validation
  (IP-literals and reserved/internal-use names refused so the facade cannot
  be used to probe the internal network), and a `0600` database file.
- The `internetnl` CLI: `submit`/`poll`/`results` subcommands against the
  batch API v2, with `--json`, `--fail-on-scored` and an allowlist file.
- Hardening out of the review chain: HTTP redirects are refused (Basic
  credentials can never travel to another host), `https` is required unless
  `INTERNETNL_ALLOW_HTTP=1`, request ids are validated before touching a URL,
  unknown-detection uses the instance's own `/metadata/report` test list,
  terminal output is control-character-sanitised, and CI actions are pinned
  to commit SHAs.
- MIT licence, README, and docs (Diátaxis-light: `docs/index.md` plus the
  self-hosting reference page with requirements, addressing caveat and
  batch-vs-website differences).
- Habitat onboarding: role definitions (`.claude/agents/`), role skills
  (`.claude/skills/`) and the builder Stop-gate `scripts/verify.sh` — the
  CLI itself is built through the habitat agent chain.
- `openspec/changes/add-internetnl-cli/design.md` pinning the CLI surface:
  environment variables, commands, exit codes, gating semantics and output
  shape.
- OpenSpec change `add-internetnl-cli` (proposal, tasks, spec deltas) — the
  initial commit.
