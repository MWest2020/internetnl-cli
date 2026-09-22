# Habitat run 03 — de hiërarchie is drie lagen diep, en de fixture was
# verzonnen

Run 02 is binnen en de RPKI-tests krijgen nu een categorie. Maar de
fixture waarmee dat bewezen werd, is niet wat de instantie teruggeeft.
Bouw voort op `main`.

## Wat er mis is

`tests/fixtures/metadata-report-20260922.json` bevat een **platte**
hiërarchie: elke categorie heeft de testnamen rechtstreeks als
children (`web_ipv6 -> web_ipv6_ns_address`).

De echte instantie geeft drie lagen (opgehaald van
api.westerweel.work, nu bijgevoegd als
`tests/fixtures/metadata-report-live-20260922.json`):

    web_ipv6 -> web_ipv6_nameservers -> web_ipv6_ns_address
                                     -> web_ipv6_ns_reach
    web_ipv6 -> web_ipv6_webserver   -> web_ipv6_ws_address
    web_https -> web_https_certificate -> web_https_cert_chain

`category_groups_from_metadata` leest maar één laag children. Tegen de
echte metadata levert dat namen als `web_ipv6_nameservers` op — en die
is géén voorvoegsel van `web_ipv6_ns_address`. Gemeten: **14 van de 38
webtests krijgen dan `category: null`**, waaronder alle
certificaat-, IPv6- en header-tests. De gouden fixtures zien er goed
uit omdat de metadata-fixture is aangepast aan de code in plaats van
andersom.

Het patroon dat wél klopt staat drie functies hoger in hetzelfde
bestand: `reference_from_metadata` loopt de boom **recursief** af en
herkent een echte test aan `report.data[naam].type == "test"`.

## Scope — ONLY this
- [ ] 1.1 `category_groups_from_metadata` loopt de boom recursief af,
  net als `reference_from_metadata`, en levert een koppeling
  **testnaam → categorienaam** voor elke knoop die volgens
  `report.data` type `test` heeft. Geen voorvoegselregel meer nodig
  als de metadata er is: de instantie zegt exact welke test bij welke
  categorie hoort. Hernoem de functie als de naam niet meer klopt.
- [ ] 1.2 `findings.py` doet dan een exacte opzoeking; de
  voorvoegselregel blijft alleen de terugval als er geen metadata is
  (mét de waarschuwing die er al staat).
- [ ] 1.3 Vervang de verzonnen metadata-fixture door de echte
  (`metadata-report-live-20260922.json` staat er al in; gooi
  `metadata-report-20260922.json` weg of vervang de inhoud). Genereer
  de gouden findings-fixtures opnieuw.
- [ ] 1.4 Een test die faalt zodra een test uit de web-fixture
  `category: null` krijgt. Nul mag het er zijn, niet veertien.
- [ ] 1.5 Controleer `reference_from_metadata` tegen de echte fixture:
  vindt hij daar nog steeds alle testnamen? Zo nee, dan was de
  unknown-detectie óók stuk en repareer hem mee — zeg het in je
  rapport.

## Waarom dit ertoe doet
Een verzonnen fixture bewijst dat de code doet wat de fixture zegt,
niet dat hij doet wat de instantie doet. Dat is hier twee keer op rij
misgegaan: eerst de voorvoegselregel die ik opschreef, nu de fixture
die eromheen werd gebouwd. De echte metadata is opgehaald en zit in de
repo; gebruik die.

## Done means
Hele suite groen, `openspec validate 2026-09-22-findings-export
--strict` groen, en in je rapport: hoeveel tests in beide gouden
fixtures nog `category: null` hebben (verwacht: 0) en wat 1.5 opleverde.
Budget is $6.
