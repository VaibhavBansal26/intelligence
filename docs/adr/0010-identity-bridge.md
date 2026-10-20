# ADR 0010: Identity bridge for sources with different peppers

Status: accepted (2026-10-20)

## Context
The analytics contract joins contacts across Funnels and Conversations by `contact_key = HMAC-SHA256(pepper, lower(trim(email)))` and assumes the workspace admin copies one pepper into both apps. The integration run ([INTEGRATION-FINDINGS.md](../INTEGRATION-FINDINGS.md), N3) showed that each real app generates its own random pepper per workspace and can only rotate it to another random value, so the same e-mail has a different key in each app and the join is empty (0 of 367 shared people). The fix belongs in the Node apps (an endpoint to set the pepper), which this service cannot change, and may take a while.

## Decision
Add `POST /v1/identity/bridge`. The customer sends a contact list it already holds (CRM export, newsletter list). For every address the service computes the key under each active source's pepper in memory, discards the address, and stores the pairs in `contact_links (source_id, contact_key, canonical_key)`. The canonical key is the key of the first `funnels` source (else the first source). Rooms of the other sources are re-keyed to it (`rooms.contact_key`, original kept in `rooms.contact_key_raw`), and rows ingested later are re-keyed on arrival, so every existing feature and the join report work unchanged. Changing a source's pepper deletes its links and restores the raw keys.

## Alternatives
- **Wait for the Node apps to accept a pepper.** Correct long term, but the integration is broken until then. The bridge stays useful afterwards for apps owned by different vendors.
- **Join in every query through `contact_links`.** No data rewrite, but every feature builder (leads, rooms, profiles, columnar tee, scorers) would need the mapping; one canonical column is smaller and safer.
- **Store e-mail addresses (or a second hash) to join without a list.** Violates the PII stance (APPROACH.md, Privacy): the service has never held addresses.
- **Ask for one hash per source from the customer.** Moves the HMAC work and the pepper to the customer and makes them handle secrets.

## Consequences
- Only people on the supplied list join; coverage is bounded by the list (the run joined 367 of 367 shared people from a list of 760 addresses).
- A request carries up to `MAX_BODY_BYTES` (1 MB, about 25,000 addresses); calls accumulate, so a longer list is sent in chunks (`"replace": true` starts over).
- Re-keying is an UPDATE over the source's rooms; for very large room tables run it off-peak.
- The bridge has no meaning for a single-source account and answers `422 BRIDGE_NEEDS_TWO_SOURCES`.
