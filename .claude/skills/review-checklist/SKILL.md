---
name: review-checklist
description: Checklist voor de reviewer-rol — gaps vinden, geen stijlvoorkeuren. Gebruik bij elke reviewer-run op een builder-branch.
---

Loop de diff langs deze vragen; elk "nee" is een finding:

1. **Spec-dekking** — implementeert de diff elke requirement/scenario uit
   de spec-delta's van de change? Ontbrekend scenario = blocking.
2. **Taken vs. werkelijkheid** — is elke afgevinkte taak in tasks.md echt
   gedaan (niet alleen aangevinkt)?
3. **Bewijs** — staat er verifieerbaar bewijs (testuitvoer, dry-run) voor
   de verificatiecriteria? Bewering zonder bewijs = major.
4. **Scope** — raakt de diff bestanden buiten de change? Dat is een
   finding, ook als de wijziging "handig" is.
5. **Regressie** — breekt de diff bestaand gedrag (grep naar gebruikers
   van gewijzigde functies/contracten)?
6. **Conventies met gedragseffect** — alleen conventieschendingen die
   gedrag of onderhoudbaarheid echt raken; geen smaak.

Severity: blocking = spec niet gehaald of regressie; major = bewijs of
dekking ontbreekt; minor = al het overige dat het melden waard is.
