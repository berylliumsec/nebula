# Integration Settings read projections

The existing Settings entry points for VPN profiles, TypeSafe and local Web
Search combine saved SQL configuration with observations owned by the Core host.
They must not require an execution project or infer credential/runtime health
from a saved database row.

| Journey | Observable invariant | Authority | Focused evidence |
| --- | --- | --- | --- |
| Open or refresh Settings | Existing public response shapes and normal empty defaults remain compatible | Shared integration schemas | Schema and projection tests |
| Read VPN profiles | Keys/references stay private; availability comes from the supplied canonical store; pages and byte limits are explicit | SQL metadata plus host credential status | Paged/redacted bounded catalog tests |
| Read TypeSafe status | Saved key takes precedence over environment; local-only opt-ins are excluded; a failed saved test remains visible | SQL settings/policies plus host vault status | Saved/environment/empty/default tests |
| Read Web Search status | Saved metadata survives; current state comes from the supplied runtime rather than the saved container state | SQL metadata plus host runtime observation | Runtime/saved-state/default tests |
| Mutate or test integrations | No read helper performs these actions | Authenticated host effect owner | Outside this slice |

`integration_settings.py` exports the existing request/response schemas without
constructing an execution kernel. The legacy kernel delegates its status reads
to the same helpers. `typesafe_integration_status(store, credentials)` and
`web_search_integration_status(store, search_runtime)` require the caller's
canonical dependencies; no independent credential store is created. Vault
observations run outside the API event loop. SQL policy counts do not hydrate a
policy catalog.

`public_vpn_profiles(store, credentials, offset=0, limit=1000)` returns the public
array and an optional next offset. Each page has at most 1,000 items and 512 KiB;
oversized pages fail visibly with 413. Its caller must advertise partial/next
offset headers and must not treat a partial catalog as complete. The ordinary
empty catalog remains `[]`. The host Settings owner mounts and authenticates the
three exact GET routes and supplies the actual vault/search observations.

Focused local receipts are `/tmp/nebula-integration-settings-selection-receipt.json`
and `/tmp/nebula-integration-settings-tests.log` (with JUnit XML beside it).
Fifteen focused cases passed: seven projection cases on both SQLite and
PostgreSQL, plus one request-schema validation case. Ruff and targeted mypy
passed.
These fixture observations do not contact providers, containers, vaults or SSH.
Integrated public route authentication, a production LAN journey and browser
acceptance remain separate gates owned by integration; local projection tests
provide only partial verification.
