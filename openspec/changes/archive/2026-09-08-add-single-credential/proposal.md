# Change: add-single-credential

## Why

Owner decision (2026-09-04), after watching a real Action run: two secrets
(`INTERNETNL_USERNAME` + `INTERNETNL_PASSWORD`) for one credential is one
too many — "that really should be a single token." The facade's own wire
protocol stays HTTP Basic (v2-compat is sacred); only the CLI/action-facing
credential UX collapses from two secrets to one.

## What Changes

- **CLI**: a new `INTERNETNL_CREDENTIAL` environment variable,
  `username:password`, split on the *first* `:` (a password may contain
  one; per RFC 7617 a Basic userid never can). Setting it together with
  `INTERNETNL_USERNAME`/`INTERNETNL_PASSWORD` is a `ConfigError` — no
  silent precedence. A missing `:`, or a split that yields an empty
  username or password, is also a `ConfigError` that never echoes the
  value; an empty username in particular must never reach `client.py`,
  which skips the `Authorization` header entirely for one, turning a typo
  into a silent anonymous request.
- **`action.yml`**: a new `credential` input (preferred path), with
  `username`/`password` kept as a supported alternative — never mixed.
  The "Validate inputs" step fails closed on anything but exactly one
  form, including a colon-less `credential` before the install step ever
  runs.
- **`netnl-admin`**: `user add`/`user reissue` refuse a username
  containing `:` — such a name could never authenticate over HTTP Basic
  in the first place (RFC 7617; the facade's own `auth._parse_basic_auth`
  partitions on the first `:`), so issuance now fails loudly instead of
  minting a permanently unusable credential.
- **Docs**: `docs/how-to/ci.md`, `README.md`, `docs/how-to/beta.md`
  updated for the new credential form; the exit-code-2 section notes that
  a third-party endpoint's own error body can echo a username unmasked
  into a CI log (the `netnl` facade does not).

## Non-goals

- **No change to the facade's own wire protocol.** `netnl` (and any other
  batch API v2 endpoint) still authenticates via HTTP Basic; this change
  is entirely about how a human or a CI pipeline hands a credential to the
  CLI/action, not how it travels on the wire.
- **No retroactive validation of existing `netnl` usernames.** A username
  containing `:` created before this change (if any ever were, despite
  never having been able to authenticate) is not migrated or purged here.

## Impact

- `src/internetnl_cli/config.py`, `tests/test_config.py`
- `action.yml`, `.github/workflows/action-smoke.yml`
- `src/netnl/admin.py`, `tests/netnl/test_netnl_admin.py`
- `docs/how-to/ci.md`, `README.md`, `docs/how-to/beta.md`, `CHANGELOG.md`
- `openspec/changes/add-internetnl-cli/design.md` (configuration table)
- Risk: low. Additive on the CLI/action side (existing
  `INTERNETNL_USERNAME`/`INTERNETNL_PASSWORD` behaviour is unchanged
  byte-for-byte when `INTERNETNL_CREDENTIAL` is unset); the `netnl-admin`
  guard only closes off names that could never have worked anyway.
