# Tasks: add-single-credential

## T1. OpenSpec change

- [x] 1.1 `proposal.md` — why, what changes, non-goals, impact
- [x] 1.2 `tasks.md` — this file
- [x] 1.3 Spec delta `specs/batch-measurement/spec.md`: MODIFIED
      "Configurable endpoint" (adds `INTERNETNL_CREDENTIAL`, the
      not-both-forms rule, and the never-empty-after-split rule)
- [x] 1.4 `openspec/changes/add-internetnl-cli/design.md`'s pinned
      configuration table gains a row for `INTERNETNL_CREDENTIAL`
- Verify: `openspec validate add-single-credential --strict`;
  `openspec validate --all --strict`

## T2. CLI

- [x] 2.1 `INTERNETNL_CREDENTIAL` resolution in
      `internetnl_cli.config._resolve_credential`: split on the first
      `:`; conflict with `INTERNETNL_USERNAME`/`INTERNETNL_PASSWORD` is a
      `ConfigError` naming both; a missing `:` is a `ConfigError` naming
      the required format without echoing the value; a split yielding an
      empty username or empty password is also a `ConfigError` (never a
      silent anonymous request via `client.py`'s empty-username skip of
      the `Authorization` header)
- [x] 2.2 `credential` added to `config._FORBIDDEN_KEYS` (config file),
      consistent with the existing `username`/`password` treatment there
- [x] 2.3 Tests: split, password-with-colon kept whole, conflict with
      username, conflict with password, format error (no echo), each
      degenerate split (`:secret`, `alice:`, `:`, empty string), unset
      falls back unchanged, ini-file rejection
- Verify: `uv run pytest tests/test_config.py -q`

## T3. `action.yml`

- [x] 3.1 New `credential` input (preferred), `username`/`password`
      optional alternatives; descriptions carry no `${{ }}` syntax
- [x] 3.2 "Validate inputs": exactly one of `credential` / (`username` +
      `password`); a `credential` without a `:` also fails here, before
      the install step
- [x] 3.3 "Run internetnl submit": exports exactly one of
      `INTERNETNL_CREDENTIAL` or `INTERNETNL_USERNAME`+
      `INTERNETNL_PASSWORD`, `unset`-ing the other form's names first so
      an inherited job/workflow-level env value cannot leak through
      alongside the one actually given
- [x] 3.4 `astral-sh/setup-uv` gets `ignore-empty-workdir: true` (no
      checkout happens in this action) and `enable-cache: false`
      (nothing here has a lockfile to key a cache on)
- [x] 3.5 `action-smoke.yml`: jobs for credential+username/password both
      set, neither set, and a colon-less credential — each asserting the
      step failed before `internetnl` reached `PATH`
- Verify: YAML parses; `shellcheck` on every embedded `run:` script;
  grep confirms no `${{` in any input description; manual extraction and
  execution of the "Validate inputs"/"Run internetnl submit" scripts
  against all credential-form combinations

## T4. `netnl-admin`

- [x] 4.1 `_reject_colon_in_username` guard, used by `_user_add` and
      `_user_reissue`: a username containing `:` can never authenticate
      over HTTP Basic (RFC 7617; `auth._parse_basic_auth` partitions on
      the first `:`), so issuance refuses it instead of minting an
      unusable credential
- [x] 4.2 Tests: `user add`/`user reissue` with a `:` in the name both
      fail, print no password, and create no row
- Verify: `uv run pytest tests/netnl/test_netnl_admin.py -q`

## T5. Docs

- [x] 5.1 `docs/how-to/ci.md`: minimal example uses `credential`; the
      RFC 7617 grounding for the first-colon split (replacing the
      unfounded "username charset" claim); input table; one sentence on
      exit-code-2 potentially echoing a username via a third-party
      endpoint's own error body
- [x] 5.2 `README.md`: quickstart and CI snippet updated
- [x] 5.3 `docs/how-to/beta.md`: notes the single-string handoff option
- [x] 5.4 `CHANGELOG.md`: `[Unreleased]` entry, top of the Added list
- Verify: manual read-through; `grep` for the retired "username charset"
  claim finds nothing

## Overall verification

- `sh scripts/verify.sh` green
- `openspec validate add-single-credential --strict` green
- `openspec validate --all --strict` green
- Working tree clean; branch not merged
