# Habitat run 04 — de herkomstgegevens ontbreken in de kop

Bouw voort op `main`. Dit repareert een gat dat ík in de
taakreferentie van run 01 heb laten vallen, niet een fout van die run.

## Wat er mis is

`build_document` geeft terug: `{"schema": ..., "domains": [...]}`.
Meer niet. Het contract
(`docs/netnl-findings-v1.md`, eis 5 "Traceability") vraagt ook een
kop:

```json
{
  "schema": "netnl-findings/v1",
  "generated_at": "2026-09-22T10:25:16Z",
  "source": {
    "api": "internet.nl batch v2",
    "endpoint": "api.westerweel.work",
    "request_id": "b2dda607433522760b51faecbcb23c87"
  },
  "domains": [ ... ]
}
```

Die gegevens zijn er allemaal: `reply.api_version`,
`reply.request.request_id`, en de endpoint-host kent `cli.py` al
(`cfg.endpoint_host`, zoals `render.build_document` hem meekrijgt).

Waarom het ertoe doet: de consument toont een bevinding als bewijs. Nu
kan een lezer niet terug naar de batch die hem opleverde. De
Wanderer-kant heeft er inmiddels omheen gewerkt met een bestandshash
als sleutel, en daarbij in een comment vastgelegd dat het request-id
"niet op de draad bestaat" — dat is niet waar, het werd alleen niet
geëxporteerd. Zo wordt een gat een eigenschap.

## Scope — ONLY this
- [ ] 1.1 `generated_at`: het moment waarop het bestand geschreven
  wordt, RFC 3339 in UTC. Injecteerbaar voor de tests (de bestaande
  code geeft elders een `retrieved_at` mee; volg dat patroon) — anders
  is de uitvoer niet deterministisch te testen.
- [ ] 1.2 `source`: `api` (uit `reply.api_version`, met de vaste
  naam ervoor zoals in het contract), `endpoint` (de host, niet de
  volledige URL met credentials), `request_id` (uit
  `reply.request.request_id`).
- [ ] 1.3 `build_document` blijft een zuivere transformatie: de
  endpoint-host en het tijdstip komen als argument mee uit `cli.py`.
  Geen netwerk, geen `datetime.now()` binnenin.
- [ ] 1.4 Determinisme: twee exports van dezelfde batch met hetzelfde
  meegegeven tijdstip blijven byte-identiek. De bestaande test
  daarvoor moet blijven slagen.
- [ ] 1.5 Gouden fixtures opnieuw genereren met een vast tijdstip, en
  `docs/netnl-findings-v1.md` bijwerken als de kop daar afwijkt.
- [ ] 1.6 Een test die faalt als `source.request_id` leeg is voor een
  batch die er wél een heeft.

## Let op
Zet géén credentials in `endpoint`. `cfg.endpoint_host` geeft alleen
de hostnaam; gebruik die.

## Done means
Hele suite groen, `openspec validate 2026-09-22-findings-export
--strict` groen, en in je rapport de kop van de web-fixture. Budget $5.
