# Open eigenaarsbesluiten

Vragen die bij het archiveren van een change nog openstonden. Ze zijn hier
naartoe verhuisd omdat ze wachten op iets dat buiten de change ligt —
gebruiksvolume, meetgegevens, of een keuze die pas met echte tenants te maken
is. Een change gijzelen tot zo'n vraag beantwoord is, levert alleen een
administratie op die niet meer overeenkomt met wat er draait.

Elke regel noemt: waar hij vandaan komt, waar hij op wacht, en wat er nu
geldt. Beantwoord je er een, werk dan de bijbehorende default of documentatie
bij en haal hem hier weg.

## Wachten op meetgegevens

Besluit 2026-09-08 (Mark): **eerst monitoring, dan pas de limieten
vastzetten.** De VPS draait al Prometheus en Grafana; die gaan geheugengebruik
en wachtrijdiepte vastleggen. Tot die cijfers er zijn blijven de defaults
staan en blijven deze vragen open — bewust, want ze op gevoel dichtzetten is
precies wat de meting moet vervangen.

### D1 — Demo-limieten (uit `add-demo-run` O2)

`NETNL_DEMO_MAX_PER_HOUR` (6), `NETNL_DEMO_MAX_CONCURRENT` (2) en
`NETNL_DEMO_PER_IP_PER_HOUR` (2) zijn een startpunt, geen meting.

Stand 2026-09-08: **nul anonieme demo-runs** sinds de pagina live ging. Er is
dus nog niets om tegen af te zetten. De limieten knellen aantoonbaar niet.

### D2 — Supporter-limieten (uit `add-supporter-issuance` O2)

`NETNL_SUPPORTER_MAX_PER_HOUR` (20) en `NETNL_SUPPORTER_MAX_ATTEMPTS` (3),
idem.

Stand 2026-09-08: twee uitgegeven sleutels in totaal, waarvan één een echte
donatie. Ruim onder elke grens.

### D3 — Capaciteit van de upstream-instance (uit `add-measurement-api` 4.3)

De private beta met echte gebruikers moet uitwijzen waar de knie zit. Dat
hangt af van het onboarden van tenants, niet van een getal dat wij kunnen
kiezen.

Wat er wél gemeten is (2026-09-08, scan van 362 domeinen): **2,4
CPU-seconden per domein**, waarbij de VPS piekte op 15 procent van twee
cores en het geheugen rond 950 MB vrij bleef. Rekenkracht is dus niet het
eerste knelpunt; geheugen waarschijnlijk wel, met `WORKER_MEMORY_LIMIT` op
1 GB per worker. Dat is een bovengrens-indicatie, geen belastingtest: de
blokken liepen sequentieel, niet gelijktijdig.

## Wachten op een tweede gebruiker

### D4 — `NETNL_DEMO_ALLOWED_ORIGIN` als lijst (uit `add-demo-run` O6)

Het ontwerp ondersteunt bewust precies één origin. Een lijst wordt pas nodig
bij bijvoorbeeld een staging- naast een productiepagina. Die situatie bestaat
niet.

Let op bij het heropenen: één origin is een bewuste vereenvoudiging van de
CORS-afhandeling, geen omissie. Een lijst betekent het antwoord per verzoek
kiezen, en dat is de plek waar een CORS-bug ontstaat.

## Gesloten, hier bewaard omdat het besluit context draagt

### `add-measurement-api` 5.3 — één repo, geen splitsing

Besloten 2026-09-08 (Mark). Client, facade en deployrecept blijven samen: ze
delen de HTTP-client, één testsuite en één openspec-administratie. Pas
heroverwegen als iemand anders dan de eigenaar daadwerkelijk een facade
draait — dat, en niet repo-netheid, zou de splitsing rechtvaardigen.

### `add-supporter-issuance` O1 — BMC-signature-header bevestigd

De echte donatie van 2026-09-04 (`pi_3UBw…`) verifieerde tegen de default
`X-Signature-Sha256`. Omdat verificatie gebeurt vóór er state wordt geraakt,
is een uitgegeven credential het bewijs dat header en encoding klopten.

### `add-supporter-issuance` O3 — donatiedrempel

Van 0 naar 2 in de valuta van het BMC-account (2026-09-04). Membership of
tiers voor bredere sleutels is bewust géén onderdeel hiervan.

### `add-demo-run` O3 — geen capaciteitsreservering voor de demo

Besloten 2026-09-05. Een slice uit `NETNL_MAX_CONCURRENT` snijden beschermt
niets zolang er geen verkeer is dat erom vraagt.
