# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[SemVer](https://semver.org/).

## [Unreleased]

### Added

- The `internetnl` CLI: `submit`/`poll`/`results` subcommands against the
  batch API v2, with `--json`, `--fail-on-scored` and an allowlist file.
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
