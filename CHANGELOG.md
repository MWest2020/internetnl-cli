# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[SemVer](https://semver.org/).

## [Unreleased]

### Added

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
