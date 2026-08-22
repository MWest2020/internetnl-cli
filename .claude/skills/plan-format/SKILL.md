---
name: plan-format
description: Plansjabloon voor de architect-rol — zelf-contained buildertaken met verificatiecriteria. Gebruik bij elke architect-run op een openspec-change.
---

Een goed plan is self-contained: het noemt de betrokken bestanden en
interfaces, zegt wat out-of-scope is, en eindigt per taak met een
end-to-end-verificatiestap.

Per buildertaak (schema-velden):

- **objective** — één zin, het waarneembare resultaat (niet de activiteit).
- **files** — exacte paden die geraakt worden; een pad dat je niet kunt
  noemen is een teken dat je verder moet verkennen.
- **steps** — genummerd, klein genoeg dat elke stap in één commit past.
- **verification** — het commando of de check waarmee de builder bewijst
  dat de taak af is (exitcode, testnaam, dry-run-uitvoer).
- **out_of_scope** — wat er expliciet NIET gebeurt, zodat de builder niet
  gaat zwerven.

Risico's die de eigenaar moet beslissen horen in `risks`, niet stilzwijgend
in het plan verwerkt.
