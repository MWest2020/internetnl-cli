---
name: verify
description: Verificatie-discipline voor de builder-rol — bewijs draaien vóór klaar melden. Gebruik aan het eind van elke buildertaak.
---

"Klaar" bestaat alleen met bewijs. Voor elke afgeronde taak:

1. Draai het verificatiecriterium uit het plan (of, als dat ontbreekt:
   de testsuite / een dry-run van het geraakte gedrag).
2. Zet het commando én de relevante uitvoer (exitcode, testnamen) in de
   structured output onder `evidence`.
3. Faalt de verificatie: niet maskeren, niet de test aanpassen — fix de
   code of rapporteer verdict FAIL met deviations.

Heeft de repo `scripts/verify.sh`, dan draait de Stop-gate die ook
automatisch: een run kán niet eindigen op een falende verify. De gate
omzeilen (verify.sh aanpassen/verwijderen) is per definitie een
scope-schending.
