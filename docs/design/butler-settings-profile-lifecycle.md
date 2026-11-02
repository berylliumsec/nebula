# Butler integration profile lifecycle

Entry: an explicit operator request in interactive Butler to create, rename,
disable or remove a nonsecret provider, Assistant harness or MCP profile. The
existing Settings gateway discovers the owning Core route and body schema.
Credential values use the separate requesting-browser credential flow.

| Journey step | Observable invariant | State authority | Check |
| --- | --- | --- | --- |
| Discover/select | Route schema and the current profile ID/revision come from Core; no guessed identifiers or model names | Settings schema, profile collection and exact profile | Core gateway tests |
| Create | The POST result identifies one new profile; readback targets that exact profile rather than replacing it with a collection | Core create route and exact GET | Parameterized Core and production-LAN browser |
| Change | Only requested fields are patched at the observed revision; stale changes fail | Core profile validator and revision | Parameterized Core and browser |
| Read/use | The exact saved name and ID appear in Settings after refresh; disabled fixtures never become the Butler runtime | Core profile and Settings catalog | Production-LAN browser |
| Delete | The observed revision is forwarded in If-Match; Core dependency guards apply and absence is read from the exact resource | Core delete transaction and exact GET | Parameterized Core and browser |
| Failure/retry | Failed or unavailable exact readback does not claim verification; callers inspect saved profiles before retrying a create | Core result and explicit read_back flag | Core gateway tests |

This short configuration journey does not stream, interrupt a model turn, probe
an external service, trust a new local program, or attest to provider health.
Running sessions keep their submitted configuration. Tests use disabled,
nonsecret synthetic profiles and a separate inert local model for Butler.
