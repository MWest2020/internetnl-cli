# demo-run Specification

## Purpose

The way someone who has never seen this tool gets a real result out of it.

A CLI against an API you must first deploy has a first step that most people
never finish. So the demo family exists, and it is discoverable from the
deployment example rather than hidden in prose: type a domain, get a genuine
answer from the same service every tenant uses. It is rate-limited on purpose —
the point is to prove the thing works, not to be free capacity.
## Requirements
### Requirement: The demo family is discoverable from the deployment example

`deploy/.env.example` SHALL document every environment variable the facade
reads for the anonymous demo route, commented out, carrying the same
default the code applies and a short note of what it bounds. The example
SHALL state that the family is opt-in and name the variables required for
the route to exist at all. Where the example and `netnl/settings.py`
disagree about a default, `settings.py` is authoritative and the example is
a defect: the example file is documentation, never a second source of
truth.

#### Scenario: An operator finds the demo bounds without reading the source

- WHEN an operator reads `deploy/.env.example` before deploying the facade
- THEN they find every `NETNL_DEMO_*` variable the code reads, including the
  per-hour, per-IP, poll and cooldown bounds that protect the upstream
  instance, and the tenant name that acts as the kill switch

#### Scenario: A newly added demo setting is not left undocumented

- WHEN a setting for the demo route is added to `netnl/settings.py`
- THEN `deploy/.env.example` carries it too, with the same default

