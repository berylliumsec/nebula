# Assistant tool failures

`nebula.tool-failure/v1` is the model-facing failure envelope. It carries the
effective input schema for the call when the schema fits within 4 KiB. Larger
schemas carry the relevant field, a SHA-256 schema reference, and
`schema_truncated: true`. The complete effective schema and original exception
are stored in Core's `tool-failure-diagnostics` directory under a random
diagnostic reference. That directory is mode 0700 and its records are mode 0600.
The model receives no argument values, exception text, paths, or diagnostic
record contents through this envelope. `diagnostic_available` states whether the
protected record was actually written; the reference remains usable for the
public diagnostic event when private storage is unavailable.

| Source | Assistant-visible tools | Schema owner | Failure handoff |
| --- | --- | --- | --- |
| Fixed command runtime | `run_command`, `process_io`, `tool_output.search/read`, `workspace.search/read` | `automation_tools.py` `ToolSpec` snapshot | `AutomationBroker` to chat or mission result |
| Brokered built-ins | artifact and workspace retrieval, `web.search`, goal result publishing, catalog search/load/call, browser automation and companion, provider subagent actions | per-turn `ToolSpec` in runtime components | `ToolBroker` to chat or mission result |
| Runtime plugins | selected OCI commands and SSH environments | frozen `ToolSpec` in runtime components | `ToolBroker` to chat or mission result |
| Core MCP plugins | selected server tools | frozen probed MCP `input_schema` in `McpToolPlugin.spec` | `ToolBroker` to chat or mission result |
| Harness gateway | retrieval, knowledge, subagents, browser companion, OCI, selected MCP | `tools/list` `inputSchema` for the session | `_gateway_call` returns `structuredContent` and an error text block |
| Vendor-native harness tools | provider-owned Read/Edit/Bash/Skill/Agent and equivalents | provider SDK | Provider-owned tool result; Core records the event and status |

The envelope categories are `invalid_arguments`, `unavailable_resource`,
`unavailable_dependency`, `timeout`, `cancelled`, `permission_denied`, and
`execution_failed`. `side_effects` is `none` when Core rejected the call
before execution or when the capability is one of the bounded read-only
retrievers; otherwise it is `unknown`. A call with unknown effects has
`retry_safe: false` and tells the assistant to inspect recorded state before
issuing another call. Missing and unauthorized resources use the same public
description, so the result cannot reveal whether a resource exists elsewhere.

## Known boundary

Vendor-native tools run inside the provider harness. Nebula does not receive
their effective input schemas or control the result returned by that SDK to its
model. Those failures remain provider-owned until the harness offers a schema
and result interception contract. Core gateway tools and brokered tools follow
the shared contract.
