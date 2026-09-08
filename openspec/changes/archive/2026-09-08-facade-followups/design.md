# Design: facade-followups

## Pinned decisions (D1–D5)

- **D1. User-Agent is set in `netnl.upstream`, by wrapping the opener — not
  by touching `internetnl_cli.client`.** `build_client(settings, opener)`
  becomes `BatchClient(build_config(settings), opener=_with_facade_user_agent(opener or urllib_opener))`.
  The wrapper has the exact `Opener` signature
  `(method, url, body, headers, timeout) -> HttpResponse`, copies `headers`
  (never mutates the dict `BatchClient._headers` built), sets
  `"User-Agent"`, and delegates. Because it wraps injected openers too, the
  existing `FakeOpener` in `tests/netnl/conftest.py` sees the header and
  tests can assert on it without a network.
- **D2. Header value.** `netnl/<v> internetnl-cli/<v>` where `<v>` is
  `importlib.metadata.version("internetnl-cli")`, falling back to
  `"unknown"` exactly like `internetnl_cli.client._package_version` (reuse
  that helper if it is importable without widening the CLI's public
  surface; otherwise duplicate its four lines in `netnl.upstream` with a
  comment naming the original). Two product tokens, space-separated, no
  comment parenthetical — the facade first, because that is the party the
  upstream operator sees connecting.
- **D3. Known-tenant reservation, two-tier cap.** In `netnl.auth`:

  ```python
  _MAX_BUCKETS = 512            # unchanged: unknown usernames + everything else
  _MAX_TENANT_BUCKETS = 256     # new: extra room reserved for known tenants

  def _record_auth_failure(conn, now, username, path, *, known_tenant: bool = False):
      ...
      limit = _MAX_BUCKETS + (_MAX_TENANT_BUCKETS if known_tenant else 0)
      if key not in _auth_failure_buckets and len(_auth_failure_buckets) >= limit:
          key = (_OVERFLOW_USERNAME, path)
  ```

  Reading: an unknown username can never push the dict past 512 entries;
  a known tenant can, up to 768. That reserves 256 slots only known
  tenants can ever occupy. It is deliberately *not* a separate dict or a
  per-tier count — one dict, one lock, one sweep, one flush path, all
  unchanged; the reservation is a single arithmetic difference in the cap
  check. Memory bound is now `_MAX_BUCKETS + _MAX_TENANT_BUCKETS + routes`
  instead of `_MAX_BUCKETS + routes`; update the block comment and
  `design.md`-style prose in the module accordingly.
- **D4. Where `known_tenant` comes from.** `authenticate` already calls
  `store.find_credential(conn, username)`. The three failure sites become:
  no/unparseable header → `known_tenant=False` (no username at all);
  `credential is None` → `False`; wrong password *or* revoked →
  `True`. No additional query, no timing change on the unknown-user path
  (the dummy scrypt call stays exactly where it is, before the record).
- **D5. Spec and prose.** `add-measurement-api/design.md`'s N2 paragraph is
  historical and is **not** edited. The spec delta in this change replaces
  the residual-risk sentence in "Append-only audit trail" with the
  reservation and adds one scenario; "Honest provenance" gains the
  upstream-facing identification. `add-measurement-api/tasks.md` is not
  touched (its follow-up note points to `design.md`, which now points here
  via the changelog entry).

## Testing constraints

- `tests/netnl/test_netnl_requests.py` (or wherever the fake opener's
  recorded calls are asserted today): every upstream call recorded by
  `FakeOpener` carries `User-Agent` starting with `netnl/` and containing
  `internetnl-cli/`; `Authorization`, `Content-Type`, `Accept` are still
  present and unchanged — proves the wrapper copies rather than replaces.
- The CLI's own tests are untouched and still pass (its header is still
  `internetnl-cli/<v>` alone when the CLI is used directly).
- `tests/netnl/test_netnl_auth.py`, next to
  `test_bucket_count_is_capped_regardless_of_unique_usernames`:
  - fill `_MAX_BUCKETS` distinct *unknown* usernames on one route in one
    minute; then one more unknown → folded into `<other>` (dict size
    unchanged); then a failure for an existing tenant (create it via the
    real `issue_credential`/`netnl-admin user add` path, wrong password)
    → a new bucket keyed on that sanitised username exists, and after the
    sweep an `auth-failure` audit row with `credential == <that username>`
    exists alongside the `<other>` row.
  - a revoked tenant behaves the same (known).
  - fill `_MAX_BUCKETS + _MAX_TENANT_BUCKETS` entries using known tenants
    for the top tier; the next known tenant folds into `<other>` — the
    reservation is itself capped.
  - existing cap test still passes unchanged: with only unknown usernames,
    `len(buckets) <= _MAX_BUCKETS + 1`.
- `tests/netnl/test_netnl_leak.py`: no password in any audit row across
  the new paths (extend the parametrisation if it enumerates paths).

## Reused, not reimplemented

- `Opener` protocol and `urllib_opener` from `internetnl_cli.client`.
- `store.find_credential` — the lookup that already happens.
- `_pop_stale_auth_failure_buckets_locked`, `_write_auth_failure_batch`,
  `_sweep_stale_auth_failure_buckets` — untouched.

## Exit criteria for this build

- `sh scripts/verify.sh` green.
- `openspec validate facade-followups --strict` and `--all --strict` pass.
- `git diff --stat` touches only `src/netnl/upstream.py`, `src/netnl/auth.py`,
  `tests/netnl/test_netnl_auth.py`, the facade request test file that
  asserts opener calls, `tests/netnl/test_netnl_leak.py` (if extended),
  `CHANGELOG.md`, and `openspec/changes/facade-followups/tasks.md`.
  `src/internetnl_cli/**` must be untouched.
