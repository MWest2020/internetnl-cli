# Tasks: findings-export

## 1. Het schema
- [x] 1.1 `netnl-findings/v1` schrijven zoals de consumentkant hem
  beschrijft (zie proposal + de twee fixtures). Per domein:
  `domain`, `type`, `measured_at`, `score_percent`, `report_url`,
  `results[]`. Per test: `test`, `category`, `status`, `verdict`,
  `detail`.
- [x] 1.2 `category` afleiden uit de testnaam: het langste voorvoegsel
  dat als sleutel in `results.categories` voorkomt
  (`web_dnssec_exist` → `web_dnssec`). Geen eigen woordenlijst, geen
  hernoeming. Geen treffer → `category: null`, de test blijft staan.
- [x] 1.3 `measured_at` = `request.finished_date` van de batch, voor
  elk domein gelijk. `report_url` = `domains[d].report.url`,
  ongewijzigd doorgegeven (op een zelf-gehoste instantie wijst die
  naar die instantie — nooit zelf een internet.nl-URL bouwen).
- [x] 1.4 `detail` is altijd `null` in v1. De batch-API levert geen
  per-variantdetail; dat staat alleen in het HTML-rapport. Het veld
  bestaat zodat een latere API-versie het kan vullen. **Niet scrapen.**

## 2. Eerlijkheid aan de rand
- [x] 2.1 Een batch die niet `done` is: exitcode niet-nul, geen
  bestand geschreven. Ook geen leeg bestand, ook niet als `--findings-out`
  naar een bestaand bestand wijst (dan blijft de oude inhoud staan).
- [x] 2.2 Een domein met `status != "ok"` (bijv. `error`) komt in het
  bestand met zijn status en een lege `results`, niet weggelaten. Wie
  hem weglaat, laat de consument denken dat het domein niet in de
  batch zat.
- [x] 2.3 Tests: een lopende batch, een mislukte batch, een batch met
  één kapot domein. Controleer elke test één keer mét de reparatie
  eruit en zeg in je rapport dat je dat deed.

## 3. Determinisme en fixtures
- [x] 3.1 Stabiele volgorde (domeinen alfabetisch, tests alfabetisch)
  en stabiele JSON-opmaak, zodat twee exports van dezelfde batch
  byte-identiek zijn. Test die dat vastlegt.
- [x] 3.2 De twee echte metingen als gouden fixtures opnemen:
  `tests/fixtures/batch-v2-web-20260922.json` en
  `-mail-20260922.json` (de API-antwoorden), plus de verwachte
  findings-uitvoer ernaast. Ze staan in de Wanderer-change onder
  `openspec/changes/2026-09-19-propose-internetnl-standards/fixtures/`
  — neem ze byte-voor-byte over.
- [x] 3.3 Een test die de export van de web-fixture vergelijkt met het
  verwachte findings-bestand, en hetzelfde voor mail.

## 4. Afronden
- [x] 4.1 README: één alinea over de uitvoervorm en waar het schema
  staat. CHANGELOG onder [Unreleased].
- [x] 4.2 Het contract (`NETNL-CONTRACT.md` uit de Wanderer-change)
  als `docs/netnl-findings-v1.md` in deze repo landen, want dit is de
  producentkant en het schema hoort te staan waar het geschreven wordt.
