# Tasks: facade-followups

## T1. OpenSpec change

- [x] 1.1 `proposal.md`, `design.md`, `tasks.md`
- [x] 1.2 Spec delta `specs/measurement-api/spec.md`: MODIFIED "Honest
      provenance" (User-Agent upstream) and "Append-only audit trail"
      (known-tenant reservation replaces residual risk N2)
- Verify: `openspec validate facade-followups --strict` and
  `openspec validate --all --strict`

## T2. Facade User-Agent (`netnl/upstream.py`)

- [x] 2.1 `_FACADE_USER_AGENT = f"netnl/{v} internetnl-cli/{v}"` with the
      CLI's version-or-`unknown` fallback (design D2)
- [x] 2.2 `_with_facade_user_agent(opener) -> Opener`: copies `headers`,
      sets `User-Agent`, delegates; `build_client` wraps `opener or
      urllib_opener` (design D1)
- [x] 2.3 Test: every `FakeOpener` call carries the facade UA and still the
      original `Authorization`/`Content-Type`/`Accept`
- [x] 2.4 CLI tests untouched and green; `src/internetnl_cli/**` untouched
- Verify: `uv run pytest tests/netnl/test_netnl_requests.py tests/ -q -k "opener or header or user_agent or client"`

## T3. Known-tenant reservation (`netnl/auth.py`)

- [x] 3.1 `_MAX_TENANT_BUCKETS = 256`; `_record_auth_failure(..., *,
      known_tenant: bool = False)`; two-tier cap check (design D3)
- [x] 3.2 Call sites: header-less → default; unknown user → default; wrong
      password or revoked → `known_tenant=True` (design D4)
- [x] 3.3 Block comment and docstrings updated: new bound is
      `_MAX_BUCKETS + _MAX_TENANT_BUCKETS + routes`
- [x] 3.4 Tests per design "Testing constraints": known tenant past the
      cap gets its own row; unknown past the cap still folds; tenant tier
      itself capped; revoked counts as known; existing cap test unchanged
- [x] 3.5 `test_netnl_leak.py`: no password in any audit row on the new paths
- Verify: `uv run pytest tests/netnl/test_netnl_auth.py tests/netnl/test_netnl_leak.py -q`

## T4. Docs

- [x] 4.1 `CHANGELOG.md`: `Added` — facade User-Agent; `Changed` — known
      tenants keep their own failure bucket past the cap (closes
      add-measurement-api residual risk N2)
- Verify: English throughout

## T5. Evidence

- [x] 5.1 `sh scripts/verify.sh` output in the run output
- [x] 5.2 One recorded `FakeOpener` header dict (with the credential value
      redacted) in the run output, showing the User-Agent
- Verify: `sh scripts/verify.sh`
