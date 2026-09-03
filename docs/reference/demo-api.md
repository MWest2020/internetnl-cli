---
status: current
last_reviewed: 2026-09-03
---

# The demo API

This is the contract the dark-launched demo page (in the
`internetnl-cli-demo` repo) relies on for the anonymous `/demo/*` route
family. It exists so that page and this facade never drift apart silently —
if you change anything below, update the demo page in the same change. See
`openspec/changes/add-demo-run/design.md` for the pinned decisions (D1–D15)
behind every rule here, and [how-to/demo-run.md](../how-to/demo-run.md) for
enabling and operating the demo.

The demo is **anonymous** (no `Authorization` header, ever) and
**strictly bounded** — it is not the authenticated batch-v2 surface
documented for tenants; see [deploy-facade.md](../how-to/deploy-facade.md)
and `openspec/changes/add-measurement-api/design.md` for that.

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/demo/requests` | Submit one domain |
| `GET` | `/demo/requests/{id}` | Poll status |
| `GET` | `/demo/requests/{id}/results` | Fetch results once `status` is `done` |
| `OPTIONS` | all three of the above | CORS preflight — always 204 |

There is no `/demo/metadata/report` and no bulk/list endpoint. One domain,
one run, one shape.

## Submitting

```
POST /demo/requests
Content-Type: application/json
Origin: https://your-demo-page.example

{"domain": "example.nl"}
```

The body is **exactly** this one field — an extra field, a `type` field, or
a list `domain` is rejected (400) before anything else runs; there is no
way to submit more than one domain through this endpoint. The only
normalisation applied to the domain is trimming whitespace and
lowercasing it — nothing else.

A successful submission replies 200 with the same shape the authenticated
`POST /requests` uses:

```json
{
  "api_version": "2.6.0",
  "request": {
    "request_id": "…32 lowercase hex characters…",
    "name": null,
    "request_type": "web",
    "status": "registering",
    "submit_date": "2026-09-03T12:00:00+00:00",
    "finished_date": null
  }
}
```

`request_id` is facade-issued (D1) — never trust or construct one
client-side.

## Polling

```
GET /demo/requests/{id}
```

Poll on a **5-second interval, backing off to 15 seconds** after the first
minute or so of polling, and **give up around 10 minutes** if `status`
never reaches a terminal value (`done`, `error`, `cancelled`) — a run that
has not finished by then should be treated as failed by the page, not
polled forever. This mirrors the batch instance's own realistic run time
for a single domain; there is no server-sent push notification. At that
cadence, one run polled through to completion is roughly 45 requests to
this endpoint — well inside the per-IP poll budget below.

A poll of an id whose *status is already terminal* answers instantly from
the facade's own store, with no call to the upstream instance at all —
polling a finished id in a loop (e.g. a page that keeps a completed run's
tab open) costs nothing beyond the first poll that observed the terminal
status. `GET .../{id}/results` is different: it always fetches from
upstream (the facade does not keep a copy of the `domains` payload), so
repeat calls to it always cost one upstream call each — fetch results once
per finished run, not in the same poll loop as the status check.

`NETNL_DEMO_POLLS_PER_IP_PER_HOUR` (default 120) bounds how many status/
results requests one client-IP bucket may make per trailing hour, on top
of (not instead of) the per-submission bounds above — exceeding it answers
429 the same way an over-quota submission does (see the error table
below). Sized well above the ~45-poll cost of one run polled to
completion; a page following the cadence above will not hit it under
ordinary use.

The all-zero id `00000000000000000000000000000000` is reserved as a smoke
probe: it always answers 404 when the demo is enabled (there is no request
with that id) and 501 when it is not (see
[how-to/demo-run.md](../how-to/demo-run.md#smoke-check)).

## Fetching results

```
GET /demo/requests/{id}/results
```

Only once `status` is `done`. The `domains` object is passed through from
the upstream batch instance unmodified — structurally identical under
canonical JSON (no key added, removed, reordered or rewritten), the same
guarantee the authenticated surface makes.

## Id hygiene

Only ever poll or fetch results for a `request_id` this facade itself
returned from a `POST /demo/requests` call in the current session. An id
from anywhere else (guessed, reused from an old page load, copied from
another visitor) is either a 404 (not owned by the demo credential, or
unknown) or, at best, someone else's still-running or finished demo run —
the facade will not tell you which, by design (D1's ownership check is the
same "foreign or unknown id is indistinguishable" guarantee the
authenticated surface gives).

## CORS

Every `/demo/*` reply — success or error — carries:

- `Access-Control-Allow-Origin`: the one origin configured via
  `NETNL_DEMO_ALLOWED_ORIGIN`, and only that one — never an echo of a
  different `Origin`, never paired with `Access-Control-Allow-Credentials`.
- `Vary: Origin`
- `Access-Control-Expose-Headers: X-Netnl-Instance, X-Netnl-Notice`
- `Cache-Control: no-store`

The demo page must be served from exactly the origin the operator
configured — a mismatch answers 403 `forbidden-origin` on an actual request
and a CORS-header-free 204 on the preflight (the browser then blocks the
request itself; the page will simply see it fail as a network/CORS error,
not a structured API error body).

## Errors

Every error reply is shaped like the rest of the facade:

```json
{"api_version": "2.6.0", "error": {"label": "…", "msg": "…"}}
```

| Status | Label | When | `msg` (shown as-is; written for a visitor) |
|---|---|---|---|
| 400 | `bad-request` | The body has an extra/`type` field, `domain` is not a plain string, or the domain fails the shape/anti-SSRF check | `enter a bare domain like example.nl, not a URL` (one literal for every one of these — pydantic's own field-level errors are never reflected back) |
| 403 | `forbidden-origin` | The request's `Origin` is present and does not match the configured one | `this origin is not allowed to use the demo` |
| 404 | `unknown-request` | The id does not exist, is malformed, or belongs to a different credential (including a tenant's own id) | `this request_id does not exist for the user` |
| 429 | `rate-limited` | The submitted domain was checked too recently (cooldown), **or** too many accepted runs from this network recently (per-IP cap) | `too many demo runs recently from this network; please try again later` — the same text for both, deliberately (see "Cooldown and the per-IP cap" below) |
| 429 | `rate-limited` | Too many status/results requests from this network recently (the poll budget, `NETNL_DEMO_POLLS_PER_IP_PER_HOUR`) | `too many status checks from this network recently; please try again later` |
| 429 | `rate-limited` | The demo's own hourly or concurrency cap is at its limit | `the demo is busy right now; please try again shortly` |
| 502 | `upstream-error` | The upstream instance answered, but not with something usable (any non-2xx status, or a malformed 2xx) | `the measurement instance is unreachable right now` — never the upstream's own status or hostname |
| 503 | `demo-unavailable` | The borrowed demo credential is missing or revoked (the kill switch), **or** the upstream instance could not be reached at the network level at all | `the live demo is temporarily unavailable; please try again shortly` — both causes share this one outcome; the page cannot (and does not need to) tell them apart |
| 501 | `not-implemented` | The demo is not enabled at all | `this batch API v2 path is not proxied by this instance` |
| 500 | `server-error` | Something broke inside the facade itself, unrelated to the upstream instance (a bug, not an upstream failure — those are the 502/503 rows above) | `an unexpected error occurred` |

A `429`/`502`/`503` reply never returns a `request_id` — polling into an id
from before the rejection is never the right response to any of these; the
page should surface the `msg` and let the visitor retry later.

### Cooldown and the per-IP cap

The cooldown and per-IP-cap rows above deliberately return the *exact
same* `msg`. An earlier version of this facade distinguished them, which
let an already over-quota visitor learn whether an unrelated domain was on
cooldown just by reading which message came back — a small but real
information leak with nothing to do with either bound's own purpose. The
per-IP cap is also checked strictly before the domain cooldown is ever
touched, so an over-quota visitor's request never even reaches (and thus
never affects) the cooldown state for whatever domain they happened to
submit.

## Provenance

Every reply — success or error — carries `X-Netnl-Instance` (the operator's
configured instance name) and `X-Netnl-Notice` (a fixed statement that this
is an independent instance, not internet.nl and not Platform
Internetstandaarden). The demo page should surface this, the same as the
authenticated surface's own consumers are expected to.
