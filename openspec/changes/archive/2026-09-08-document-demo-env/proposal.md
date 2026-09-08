# Change: document-demo-env

## Why

`deploy/.env.example` is the file an operator reads to find out what the
facade can be configured with. It documents the upstream connection, the
limits, retention, the security contact and the whole supporter-webhook
family — but not a single one of the ten `NETNL_DEMO_*` variables that
`add-demo-run` introduced.

That is a real gap, not a cosmetic one. The demo family is opt-in and
off by default, so an operator who never learns the variable names never
learns the route exists; and someone who does enable it has no documented
place to discover the four bounds that protect the instance
(`NETNL_DEMO_MAX_PER_HOUR`, `NETNL_DEMO_PER_IP_PER_HOUR`,
`NETNL_DEMO_POLLS_PER_IP_PER_HOUR`, `NETNL_DEMO_DOMAIN_COOLDOWN_SECONDS`)
or the kill switch (`NETNL_DEMO_TENANT`). The behaviour is specified in
`add-demo-run` and explained in `docs/how-to/demo-run.md`; only the
example file was never brought in step.

Measured: `settings.py` reads ten `NETNL_DEMO_*` names, `.env.example`
mentions none of them.

## What Changes

A `NETNL_DEMO_*` block in `deploy/.env.example`, in the same shape as the
blocks already there: commented-out lines carrying the real default, one
short comment per variable saying what it bounds, and the opt-in variable
first so the reader sees that the whole family does nothing until it is
set.

The block covers all ten names, with `NETNL_DEMO_ENABLED`,
`NETNL_DEMO_ALLOWED_ORIGIN` and `NETNL_DEMO_TENANT` marked as the three an
operator must set together for the route to exist at all, and the
remaining seven as tunable bounds with their defaults.

## Non-goals

- **No behaviour change.** Nothing in `src/` is touched. This change adds
  documentation of settings that already exist and already have defaults.
- **No new variable, no changed default.** If a default in `settings.py`
  and the value written in the example ever disagree, `settings.py` wins
  and the example is wrong — the example is documentation, never a source
  of truth.
- **No change to `docs/how-to/demo-run.md`**, which already explains the
  mechanism; this only makes the variables discoverable from the file
  operators actually copy.

## Impact

- `deploy/.env.example` only. No source file, no test, no spec behaviour.
- Two variables in `.env.example` that do not appear in `settings.py` —
  `NETNL_PUBLIC_HOST` and `NETNL_UPSTREAM_NETWORK` — are deliberately left
  alone: they are consumed by `deploy/compose.yaml` and `deploy/Caddyfile`,
  not by the application.
