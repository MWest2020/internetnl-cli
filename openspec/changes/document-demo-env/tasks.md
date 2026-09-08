# Tasks: document-demo-env

## T1. OpenSpec

- [x] 1.1 `proposal.md`, `tasks.md`
- Verify: `openspec validate document-demo-env --strict`

## T2. The block

- [x] 2.1 Add a `NETNL_DEMO_*` block to `deploy/.env.example`, following the
      shape of the blocks already in that file: a short header comment, then
      commented-out `NAME=default` lines.
- [x] 2.2 Cover all ten names read by `netnl/settings.py`:
      `NETNL_DEMO_ENABLED`, `NETNL_DEMO_ALLOWED_ORIGIN`, `NETNL_DEMO_TENANT`,
      `NETNL_DEMO_MAX_PER_HOUR`, `NETNL_DEMO_PER_IP_PER_HOUR`,
      `NETNL_DEMO_POLLS_PER_IP_PER_HOUR`,
      `NETNL_DEMO_DOMAIN_COOLDOWN_SECONDS`, `NETNL_DEMO_MAX_CONCURRENT`,
      `NETNL_DEMO_RETENTION_HOURS`, `NETNL_DEMO_CLIENT_IP_HEADER`.
- [x] 2.3 Every default written in the file MUST match the default in
      `src/netnl/settings.py`. Read them from the source; do not guess.
- [x] 2.4 Say in the header comment that the family is opt-in and that
      `NETNL_DEMO_ENABLED`, `NETNL_DEMO_ALLOWED_ORIGIN` and
      `NETNL_DEMO_TENANT` are the three needed for the route to exist.
- [x] 2.5 Leave `NETNL_PUBLIC_HOST` and `NETNL_UPSTREAM_NETWORK` untouched —
      they belong to `deploy/compose.yaml` and `deploy/Caddyfile`.
- Verify: for each of the ten names, the value in `.env.example` matches the
  default in `settings.py`; `git diff` touches only `deploy/.env.example`
  and this `tasks.md`.

## T3. Evidence

- [x] 3.1 `sh scripts/verify.sh` in the run output (nothing should change,
      but the gate must be seen to pass)
- [x] 3.2 The rendered diff of `deploy/.env.example` in the run output
- Verify: `sh scripts/verify.sh`
