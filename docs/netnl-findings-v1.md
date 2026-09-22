---
status: current
last_reviewed: 2026-09-22
---

# `netnl-findings/v1`

The document `internetnl results <id> --format findings` writes. This is
the producer side of the contract: netnl writes this shape, and any
consumer (for example Wanderer, adopting internet.nl results as one of
its own dimensions) reads it without depending on netnl itself or on the
batch API's own quirks directly.

## Getting one

```sh
internetnl results <request-id> --format findings                       # to stdout
internetnl results <request-id> --format findings --findings-out out.json
```

Only a `done` batch produces output. A batch that is still `registering`,
`running` or `generating` — or one that ended `error`/`cancelled` — exits
non-zero and writes nothing: not an empty file, and if `--findings-out`
names an existing file, that file is left exactly as it was. A CI step
that trusts the exit code can never archive a partial or empty document
as a successful measurement.

## Shape

```json
{
  "schema": "netnl-findings/v1",
  "domains": [
    {
      "domain": "example.nl",
      "type": "web",
      "status": "ok",
      "measured_at": "2026-09-22T10:25:16.924927+00:00",
      "score_percent": 95,
      "report_url": "https://netnl.example/site/example.nl/485/",
      "results": [
        {
          "test": "web_dnssec_exist",
          "category": "web_dnssec",
          "status": "passed",
          "verdict": "good",
          "detail": null
        }
      ]
    }
  ]
}
```

`domains` is a list, sorted alphabetically by `domain`; each domain's
`results` list is sorted alphabetically by `test`. The same finished
batch always exports the same bytes — same order, same JSON formatting —
so two exports of one batch diff cleanly in an archive and a consumer can
recognise a re-import as already seen.

### Per-domain fields

| Field           | Meaning                                                                                   |
| ---------------- | ------------------------------------------------------------------------------------------ |
| `domain`         | The hostname submitted.                                                                    |
| `type`           | `"web"` or `"mail"` — the batch's own request type, same for every domain in one export.   |
| `status`         | The API's domain status (`"ok"` on success). A non-`"ok"` domain still appears — see below. |
| `measured_at`    | The batch's `request.finished_date`. The API gives no per-domain timestamp, so every domain in one export carries the same value. |
| `score_percent`  | The API's own published score. `null` when the domain has no scoring (a broken domain, e.g. `status: "error"`). Never recomputed here — a second score that disagrees with the public report destroys trust in both. |
| `report_url`     | `domains[d].report.url`, passed through unmodified. On a self-hosted instance this points at that instance, never at internet.nl. `null` when the domain has no report. |
| `results`        | The subtest list below. Empty (`[]`), not omitted, when the domain has no results (e.g. `status: "error"`). |

### A domain that failed to measure

A domain whose own `status` is not `"ok"` (for example `"error"`) still
gets a block, with that status and an empty `results` list — never
dropped from the document. A consumer that only sees domains with
results cannot tell "measured badly" from "never submitted"; this is
why the block always appears.

### Per-test fields

| Field      | Meaning                                                                                          |
| ---------- | ------------------------------------------------------------------------------------------------- |
| `test`     | The API's own subtest name, e.g. `web_https_tls_ciphers`.                                         |
| `category` | The category the instance's own `GET /metadata/report` hierarchy places `test` under: `report.hierarchy.<web\|mail>` lists, per category, its subtestgroups, and `test` belongs to the category whose subtestgroup name is the longest prefix of `test` (`web_dnssec_exist` → `web_dnssec`; `web_ns_rpki_exists` → `web_rpki`, since `web_ns_rpki` is a subtestgroup of `web_rpki` even though it isn't a prefix of `web_rpki`). `null` when no subtestgroup prefixes the test name — the test still appears; nothing is dropped or renamed to force a match. |
| `status`   | One of `passed`, `failed`, `warning`, `info`, `not_tested`, `error` — the API's own value, verbatim. `error` (a broken measurement) is distinct from `failed` (a measured, failing result). |
| `verdict`  | The API's own verdict word (`good`, `bad`, `warning`, `not-tested`, `recommendations`, `other`, …), verbatim, alongside `status` rather than instead of it. |
| `detail`   | Always `null` in v1. The batch API publishes no per-variant detail (that only exists in the HTML report, which this export does not scrape). The field exists so a later API version — or a later schema version — can fill it without a breaking change to the ones that don't. |

### When the metadata hierarchy is unavailable

`--format findings` fetches `GET /metadata/report` alongside the batch
results to place each test's `category`. When that fetch fails, or the
reply is structurally unusable (missing or malformed
`report`/`hierarchy`/`data`), the export still writes — it is a degraded
render, never a hard failure — but `category` falls back to the older,
weaker rule: the longest key in the batch's own `results.categories` that
prefixes the test name. That rule can't see a subtestgroup whose name
isn't itself a prefix of the test (it misses `web_ns_rpki_exists`, for
example), so every fallback is announced with a `warning:` line on
stderr rather than happening silently — a findings document that quietly
drops a real category reads to a consumer as "measured, nothing here to
flag."

## What this export deliberately does not do

- **No aggregation, re-weighting, or omission.** Every subtest the batch
  API returned for a domain appears exactly once.
- **No second score.** `score_percent` is the API's own; nothing here
  recomputes it.
- **No scraping.** `detail` stays `null` rather than being filled from the
  HTML report — that would tie this export to a page layout the API
  itself makes no promise about.
- **No breaking change without a version bump.** `"schema"` is
  `"netnl-findings/v1"` today; a future incompatible change ships as
  `netnl-findings/v2` rather than changing this document's meaning out
  from under an existing consumer.

## Existing output is untouched

`--format findings` is additive. `--json`, the plain-text table, and
`results` without `--format` behave exactly as before — a CI pipeline
that has not asked for `findings` sees no change.
