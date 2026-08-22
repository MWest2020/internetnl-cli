---
name: security-review
description: Security-checklist voor de security-rol, afgeleid van anthropics/claude-code-security-review. Gebruik bij elke security-run op een builder-branch.
---

Scope: de diff van de change en zijn directe omgeving (dataflow van input
naar de gewijzigde code), geen volledige repo-audit.

Categorieën (uit Anthropics security-review, gecustomized voor dit
ecosysteem):

- Injection: SQL/command/XXE; alles waar externe input een interpreter
  bereikt (let op subprocess/shell-aanroepen in Python/Bash).
- AuthN/AuthZ: IDOR, ontbrekende checks op nieuwe endpoints/paden.
- Secrets: hardcoded keys/tokens/wachtwoorden, ook in tests en
  voorbeelden; een gevonden geheim = verdict FAIL + mens erbij.
- Crypto: eigen crypto, zwakke algoritmes, onveilige randomness.
- RCE/deserialisatie, XSS, SSRF, path traversal.
- Supply chain: nieuwe dependencies — gepind? bekende bron?

False-positive-filter: geen DoS/rate-limiting-findings en geen
theoretische kwetsbaarheden zonder aanvalspad, tenzij de repo daar
expliciet om vraagt. Elk finding noemt locatie + concreet aanvalspad.
