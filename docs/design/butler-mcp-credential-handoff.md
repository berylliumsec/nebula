# Butler MCP credential attachment

Entry point: an operator asks interactive Butler to attach a saved managed
credential reference to one MCP server. Butler pages nonsecret server identities,
transports, authentication modes, revisions and occupied key names. The
requesting browser checks the exact reference's status and confirms the server,
slot and optional header or environment name. Core owns the profile revision
and credential value; Butler never sees the value.

| Journey step | Observable invariant | State authority | Check |
| --- | --- | --- | --- |
| Discover/select | An exact non-builtin MCP server and current revision are selected by identity | Core MCP profile; selected browser project | Capability and component tests |
| Create/use | HTTP bearer attachment sets bearer auth and clears header refs; HTTP header attachment sets header auth, preserves other active header refs, and clears bearer; stdio environment attachment preserves other environment refs | Core profile, credential store, browser confirmation | Component and real-Core browser |
| Read back | Core returns a newer revision with the exact slot/name/reference and coherent mode; credential status remains available | Core stores | Component and real-Core browser |
| Refresh/reconnect | The saved reference survives reload; a failed Settings refresh exposes Retry catalogs | Core store; Settings presentation | Real-Core browser |
| Failure/retry | Changed profile, invalid key, unavailable reference or mismatched transport fails before mutation; lost response triggers exact Core read before any retry | Core conflict contract and browser | Component and real-Core browser |
| Delete/revoke | Exact-reference credential deletion remains separate and may leave a configured MCP server unauthenticated | Credential store and MCP profile | Existing credential journey |

HTTP MCP servers require their existing URL and transport policy. Builtin
servers cannot be modified. A saved credential reference does not prove the
remote MCP server accepted it; the operator checks MCP health before use.
Streaming and interruption do not apply to this short profile mutation.
