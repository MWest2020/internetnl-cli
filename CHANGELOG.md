# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[SemVer](https://semver.org/).

## [Unreleased]

### Fixed

- The CLI now sends a `User-Agent: internetnl-cli/<version>` header on every
  request instead of letting `urllib` fall back to its default
  `Python-urllib/x.y` string — Cloudflare's bot protection in front of the
  primary batch endpoint was blocking the CLI's default `urllib`
  `User-Agent` with an HTTP 403.

### Added

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
