# Habitat run 02 — de categorie komt uit de metadata, niet uit een
# voorvoegsel

Run 01 is binnen en goed; dit repareert één ding dat ík verkeerd had
opgeschreven in de taakreferentie. Bouw voort op `main`.

## Wat er mis is

`_category_for` (`src/internetnl_cli/findings.py`) neemt de langste
sleutel uit `results.categories` die een voorvoegsel van de testnaam
is. Dat is precies wat run 01 gevraagd werd te bouwen, en het klopt
niet. In de gouden fixture staat nu:

    web_ns_rpki_exists   category: null
    web_ns_rpki_valid    category: null
    web_rpki_exists      category: web_rpki
    web_rpki_valid       category: web_rpki

Zes tests over beide fixtures (`web_ns_rpki_*`, `mail_ns_rpki_*`,
`mail_mx_ns_rpki_*`) horen bij RPKI maar dragen een infix `ns`, dus
geen categorienaam is hun voorvoegsel. Een consument die op
`category` scoort, laat nameserver-RPKI vallen: een domein met
ongeldige RPKI op zijn nameservers leest dan gewoon "goed".

## Waar de juiste koppeling staat

`GET /metadata/report` → `report.hierarchy.<web|mail>`: per categorie
een lijst subtestgroepen. Op api.westerweel.work:

    web_rpki -> web_rpki
    web_rpki -> web_ns_rpki

Een test hoort bij de categorie waarvan één van de groepsnamen het
langste voorvoegsel van de testnaam is. Deze repo haalt dat document
al op en ontleedt het al: `client.metadata_report()` en
`gating.reference_from_metadata`.

## De spanning die je moet oplossen, niet wegpoetsen

`findings.py` zegt in zijn eigen docstring "No network I/O", en
`test_results_format_findings_on_done_run_writes_one_document_no_metadata_call`
legt dat vast. Die keuze was verdedigbaar. Maar de categorie is niet
af te leiden zonder de hiërarchie.

Los het zo op: **`findings.py` blijft een zuivere transformatie** en
krijgt de hiërarchie als argument mee. `cli.py` haalt hem op — daar
gebeurt al netwerkverkeer, en `_render` roept `metadata_report()` al
aan voor de gating. Pas die ene test aan naar wat er nu hoort te
gebeuren; verwijder hem niet.

## Scope — ONLY this
- [ ] 1.1 Categorie uit de metadata-hiërarchie, met de groepsnamen als
  voorvoegsels. `findings.py` blijft zonder netwerk.
- [ ] 1.2 Metadata niet op te halen of onbruikbaar → terugvallen op de
  voorvoegselregel **en waarschuwen op stderr**. Een stille terugval
  bereikt de lezer als een regel die zelfverzekerd "passed" zegt.
  `_render` heeft hier al een patroon voor (`degraded_reason`).
- [ ] 1.3 De gouden fixtures opnieuw genereren. Controleer daarna met
  de hand dat de zes RPKI-tests een categorie hebben, en zeg de
  uitkomst in je rapport.
- [ ] 1.4 Een test die faalt zodra een test uit de fixtures
  `category: null` krijgt terwijl de metadata hem wél plaatst.
- [ ] 1.5 `docs/netnl-findings-v1.md` bijwerken: de afleiding komt uit
  de metadata, de voorvoegselregel is een terugval met waarschuwing.

## Done means
De hele suite groen (`uv run pytest`), `openspec validate
2026-09-22-findings-export --strict` groen, en in je rapport de
categorie van de zes RPKI-tests. Budget is $6.
