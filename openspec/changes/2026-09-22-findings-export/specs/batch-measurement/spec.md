## ADDED Requirements

### Requirement: A findings export another tool can read

The CLI SHALL offer an additional output format, `--format findings`,
that writes a versioned document (`"schema": "netnl-findings/v1"`)
carrying every subtest of a completed batch verbatim: the API's
`status` (`passed` / `failed` / `warning` / `info` / `not_tested` /
`error`) and its `verdict` word, per domain, with the domain's
`report_url` and `score_percent`.

The export SHALL NOT aggregate, re-weight, or omit subtests. It SHALL
NOT compute a score of its own — the batch API already publishes one,
and a second number that disagrees with the public report destroys
trust in both.

Category names SHALL be carried verbatim from the API
(`web_dnssec`, `mail_starttls`, …), derived for each test by the
longest category key that prefixes the test name. A test matching no
category SHALL be exported with a null category rather than dropped.

The `detail` field SHALL be null in v1. Per-variant results are not
published by the batch API; the export SHALL NOT obtain them by
scraping the HTML report.

#### Scenario: Every subtest survives the export

- **GIVEN** a completed web batch whose results contain 38 subtests
  across five categories
- **WHEN** the run is exported with `--format findings`
- **THEN** the document contains 38 result entries, each with the
  API's own status and verdict, and each category name carries its
  `web_` prefix

#### Scenario: A broken subtest is not a bad score

- **GIVEN** a completed mail batch in which
  `mail_starttls_tls_available` came back with status `error`
- **WHEN** the run is exported
- **THEN** that entry appears with status `error`, distinguishable
  from `failed`

---

### Requirement: An unfinished batch exports nothing

Exporting a batch that is not `done` SHALL exit non-zero and SHALL
NOT write an output file. Where `--findings-out` names an existing
file, that file SHALL be left untouched.

Partial *content* is fine — `not_tested` subtests and domains whose
own status is not `ok` belong in the document. A partially *written*
or empty document does not: a consumer cannot tell it apart from a
measurement that found nothing, and a CI step that trusts the exit
code would archive it as a success.

#### Scenario: Running batch

- **GIVEN** a batch whose request status is `running`
- **WHEN** `--format findings` is requested
- **THEN** the command exits non-zero and no output file exists

#### Scenario: A domain that failed to measure still appears

- **GIVEN** a completed batch in which one domain has status `error`
- **WHEN** the run is exported
- **THEN** that domain appears with its status and an empty results
  list, so the consumer can tell "measured badly" from "not submitted"

---

### Requirement: The same batch exports the same bytes

Two exports of one completed batch SHALL produce byte-identical
files: domains and tests in a stable order, stable JSON formatting.
A consumer re-importing the same file SHALL be able to recognise it
as already seen, and the file SHALL diff cleanly when archived in
git.

#### Scenario: Re-export is byte-identical

- **GIVEN** a completed batch exported twice
- **WHEN** the two files are compared
- **THEN** they are identical byte for byte
