# Habitat run 01 — de findings-export (alle taken)

Contract: `openspec/changes/2026-09-22-findings-export/`.

Lees eerst `proposal.md`, `tasks.md` en
`specs/batch-measurement/spec.md`. De twee gouden fixtures staan al in
`tests/fixtures/` — dat zijn ECHTE metingen van 2026-09-22 op een
zelf-gehoste instantie (web 95%, mail 70%). Verzin er geen bij en pas
ze niet aan; als je uitvoer er niet op past, is je uitvoer fout.

## Waar je op moet letten

De vorm van het API-antwoord is gemeten, niet aangenomen:

- Elke testuitslag is exact `{"status": ..., "verdict": ...}`. Er is
  GEEN per-variantdetail in de API. `detail` blijft `null`. Ga dat
  niet uit het HTML-rapport halen.
- `status` kent zes waarden: `passed`, `failed`, `warning`, `info`,
  `not_tested`, `error`. In de mail-fixture staat een echte `error`
  (`mail_starttls_tls_available`).
- `verdict` is een apart woord (`good`, `bad`, `warning`,
  `not-tested`, `recommendations`, `other`). Draag beide over.
- Een test draagt zelf geen categorie. Die leid je af uit de testnaam
  via `results.categories` (langste voorvoegsel wint).
- Er is geen tijdstip per domein; gebruik `request.finished_date`.
- `report.url` geef je ongewijzigd door.

## Scope
Alle taken in `tasks.md` (1.1–4.2). Het is één samenhangend geheel;
splitsen levert een half schema op.

## Done means
De testsuite groen (`uv run pytest` of wat deze repo gebruikt — kijk
in de CI-workflow), `openspec validate 2026-09-22-findings-export
--strict` groen, en in je rapport: de daadwerkelijke uitvoer van de
export op beide fixtures.

Budget is $9.
