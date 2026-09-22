# Proposal: findings-export — één bestandsvorm die een andere tool kan lezen

## Why

netnl meet en velt een oordeel. Wat het niet doet is zijn uitslag
doorgeven in een vorm waar een andere tool op kan bouwen: `--json`
geeft het API-antwoord door zoals het binnenkwam, inclusief de
eigenaardigheden van batch v2 (categorieën apart van de tests, geen
tijdstip per domein, een verdict-woord naast een status).

Wanderer wil die uitslagen opnemen als een eigen dimensie in plaats van
DNSSEC, SPF/DMARC, STARTTLS/DANE en RPKI zelf na te bouwen. Dat is
geen samenwerking tussen twee codebases maar tussen twee bestanden:
netnl schrijft, Wanderer leest, en geen van beide kent de ander.

Deze change voegt één uitvoervorm toe. Alles wat er al is, blijft.

## What Changes

- `internetnl results <id> --format findings` (en `--findings-out
  <bestand>`) schrijft `netnl-findings/v1`: per domein een blok met
  `measured_at`, `report_url`, `score_percent` en één regel per
  subtest met `test`, `category`, `status`, `verdict` en `detail`.
- Het schemaveld `"schema": "netnl-findings/v1"` staat bovenaan.
  Brekende wijzigingen verhogen de versie.
- De uitvoer is deterministisch: dezelfde afgeronde batch levert
  byte-identieke bestanden, zodat een consument kan ontdubbelen en het
  bestand schoon diff't in een archief.
- Een onafgeronde of mislukte batch levert **exitcode niet-nul en
  geen bestand**.
- Gouden fixtures voor web én mail, uit echte metingen.

## Waarom dit hier hoort, en niet in de consument

De omzetting van het API-antwoord naar deze vorm vraagt kennis van de
API: hoe je de categorie van een test afleidt, welke tijd je moet
gebruiken als er geen tijd per domein is, welke statussen bestaan. Die
kennis hoort bij de tool die de API aanroept. Zet je hem in de
consument, dan heeft elke consument hem opnieuw nodig — en lopen ze
uiteen.

## Wat bewust NIET verandert

- Bestaande uitvoervormen (`--json`, de tabel) blijven exact zoals ze
  zijn. Een CI-gebruiker merkt niets tenzij hij erom vraagt.
- Het gedrag van `results` zelf verandert niet. Dat `results` op een
  lopende batch nu met exitcode 0 een document met `"domains": null`
  schrijft, is bestaand gedrag met bestaande gebruikers; de nieuwe
  uitvoervorm erft het niet.
- netnl krijgt geen afhankelijkheid op Wanderer, en kent het woord
  niet buiten dit voorstel.
