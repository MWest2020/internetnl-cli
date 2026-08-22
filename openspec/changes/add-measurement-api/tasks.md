# Tasks: add-measurement-api

## 0. Prerequisites — the facade fronts nothing until these hold

- [ ] 0.1 Self-hosted batch instance deployed (`add-internetnl-cli` task 4.2)
      on a machine with a fixed public IPv4 + IPv6
- [ ] 0.2 CLI acceptance test against that instance passes unchanged
      (`add-internetnl-cli` task 4.4)
- [ ] 0.3 Owner decisions recorded: service name — **`netnl`** (decided
      2026-08-22; keeps the reference to the mission: an opinion on how the
      internet should work, according to NL); still open: public hostname,
      retention periods, initial limits (rate, max domains, max concurrent
      runs)

## 1. Skeleton

- [x] 1.1 Facade package in this repo (own `uv` dependency group: FastAPI,
      pydantic v2), console entry point for the server
- [x] 1.2 Settings from the environment only — upstream endpoint + credential,
      limits, retention, SQLite path; no defaults pointing anywhere
- [ ] 1.3 SQLite schema: credentials, id-map (facade id ↔ upstream id ↔
      credential), audit (append-only — enforce via triggers or
      insert-only data layer)
- [ ] 1.4 Test harness: no network in tests (upstream client behind the same
      injectable-opener seam as the CLI), `$HOME`-isolation fixture, CI job
      alongside the existing suite

## 2. v2 surface

- [ ] 2.1 `POST /requests`: validate, enforce limits, submit upstream with
      the server-side credential, issue a facade id (`^[a-f0-9]{32}$`),
      audit, reply in v2 shape
- [ ] 2.2 `GET /requests/{id}` and `GET /requests/{id}/results`: tenant
      check (foreign/unknown id → 404), upstream fetch, passthrough with
      facade id substituted
- [ ] 2.3 `GET /metadata/report` passthrough (cacheable)
- [ ] 2.4 v2-shaped error bodies everywhere, incl. unimplemented paths;
      provenance header on every reply
- [ ] 2.5 Leak test: upstream credential (and its base64 form) greppable in
      no reply, error, or captured log line

## 3. Tenancy, limits, audit

- [ ] 3.1 Credential issuance + revocation as operator CLI/runbook
      (revocation effective immediately)
- [ ] 3.2 Rate limit, max domains, max concurrent runs — env-tunable, tested
      at the boundaries, 429/400 in v2 shape
- [ ] 3.3 Audit records on submit and credential lifecycle; test proves
      append-only (no UPDATE/DELETE path)
- [ ] 3.4 Retention job for result bodies and expired audit data, per the
      documented periods

## 4. Deploy and beta

- [ ] 4.1 Compose unit next to the batch instance: facade public, instance
      internal-only; TLS at the edge
- [ ] 4.2 Acceptance test: `internetnl` CLI against the facade, unchanged,
      green — and the instance unreachable from outside
- [ ] 4.3 Private beta with issued credentials; capacity observations fed
      back into the default limits

## 5. Community opening

- [ ] 5.1 Docs page: endpoint, terms (only measure hosts you operate),
      limits, retention, batch-vs-website differences, no-SLA statement
- [ ] 5.2 Credential-request runbook published
- [ ] 5.3 Handover package: deploy recipe + issuance runbook complete enough
      to run without us; revisit repo split at this point
