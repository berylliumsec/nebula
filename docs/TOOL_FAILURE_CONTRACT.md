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

## Envelope

An envelope has these fields; the last two appear only where stated. Only
Core writes them, and none holds tool or exception text.

| Field | Value |
| --- | --- |
| `schema` | `nebula.tool-failure/v1` |
| `status` | `failed` |
| `tool` | the name the model called |
| `category` | one of the categories below |
| `problem` | Core's fixed sentence for the category |
| `side_effects` | `none` or `unknown` |
| `invalid_input` | the argument at fault, or `null` |
| `effective_input_schema` | the schema offered for the call (see above), or `null` |
| `schema_truncated` | `true` when only the relevant field is repeated |
| `schema_reference` | `<tool>@<version>:sha256:<digest>` of the full schema, or `null` |
| `next_action` | Core's fixed guidance for the category and effects |
| `retry_safe` | whether reissuing the call can be correct (see each category) |
| `diagnostic_reference` | the protected record's reference, or `null` |
| `diagnostic_available` | whether that record was written |
| `limit` | only for `capacity_reached` and `budget_exhausted` (below) |
| `result_receipt` | only when a tool returned a failed receipt: `{artifact_id, status}` |

`limit` is `{"resource": <name>, "maximum": <int>, "current": <int>}`: the
limit that refused the call and how much of it is in use, as Core's own
numbers. It is how a refusal's detail reaches the model now that its text
cannot.

| `resource` | `maximum` | `current` |
| --- | --- | --- |
| `running_subagents` | the operator's "Running at once" | subagents of this conversation running now |
| `goal_tokens` | the goal's token budget | tokens the goal has spent |
| `catalog_discovery_calls_per_turn` | `MAX_CATALOG_CALLS_PER_TURN` (8) | catalog searches and loads this turn |

## Categories

| Category | Meaning | Guidance (`next_action`) | Retry |
| --- | --- | --- | --- |
| `invalid_arguments` | An argument is wrong | Correct the argument from the schema, then reissue | After correcting it, when refused before execution |
| `capacity_reached` | Running work holds every slot a limit allows | Wait for running work to finish, then retry this call | Yes, after waiting (`retry_safe: true`) |
| `budget_exhausted` | An allowance the call needs is used up; waiting does not restore it | Do not retry; continue with what is available, or tell the operator | No |
| `permission_denied` | A setting, access or approval rule refuses the call | Request access or choose an authorized action; do not repeat | No |
| `unavailable_resource` | The resource is missing or not this caller's | Use an ID from an available receipt | No |
| `unavailable_dependency` | A dependency could not be reached | Check the diagnostic reference and state | Only when refused before execution |
| `timeout` | The tool ran past its time limit | Check the recorded state before reissuing | Only when refused before execution |
| `cancelled` | The call was cancelled | Check the recorded state before reissuing | Only when refused before execution |
| `execution_failed` | The tool could not complete | Check the diagnostic reference and state | Only when refused before execution |
| `unavailable_tool` | The tool was not offered for this step | Choose an offered tool, or finish | No |
| `outcome_unknown` | The call had started when Core stopped and was not run again | Inspect state; do not repeat unchanged | No |
| `missing_callback` | A background command ended without posting its result | Inspect the recorded output and state | No |

`side_effects` is `none` when Core rejected the call before execution or when
the capability is one of the bounded read-only retrievers; otherwise it is
`unknown`. A `capacity_reached` or `budget_exhausted` refusal is always refused
before execution, whichever path reports it. `retry_safe` is `true` only when
`side_effects` is `none` and the category is not `permission_denied`,
`unavailable_resource` or `budget_exhausted`. A call with unknown effects has
`retry_safe: false` and tells the assistant to inspect recorded state before
issuing another call. Missing and unauthorized resources use the same public
description, so the result cannot reveal whether a resource exists elsewhere.

Brokers classify by exception type, not by message: `InvalidToolArguments`
(and schema `ValidationError`), `CapacityReached`, `BudgetExhausted`,
`ToolNotPermitted` and other `PolicyDenied`, and a refusal marked
`refused_before_execution` in `nebula.v3.tools`. The provider-chat loop and the
harness gateway pass the same exception to `tool_failure`, so one refusal has
one category on both paths.

## Subagent, agent-messaging and catalog refusals

| Refusal | Category |
| --- | --- |
| The operator's running-at-once limit, for `start_subagent` / `subagent.start` and for a message that would start another round | `capacity_reached` (`running_subagents`) |
| The parent's goal token budget is used up (its subagents spend it, #570) | `budget_exhausted` (`goal_tokens`) |
| The turn's catalog search and load allowance is used up | `budget_exhausted` (`catalog_discovery_calls_per_turn`) |
| Subagents turned off for the conversation, a subagent starting its own, no provider model, no provider chat turn | `permission_denied` |
| `message_parent` from outside a subagent or after its round ended | `permission_denied` |
| Agent messaging turned off, outside a chat, or for another project | `permission_denied` |
| Blank task or message, unknown subagent or peer id, unknown `reasoning_effort`, nothing to wait for | `invalid_arguments` (before execution) |
| Core tried to start a subagent or its next round and could not | `execution_failed` |

A limit is checked after the rules, so a call that is not permitted is never
told to wait and retry, and a goal's budget before the running-at-once limit,
so a call waiting cannot help is never told to wait. A harness conversation
has no Nebula goal, so `goal_tokens` only reaches provider chats.

## Known boundary

Vendor-native tools run inside the provider harness. Nebula does not receive
their effective input schemas or control the result returned by that SDK to its
model. Those failures remain provider-owned until the harness offers a schema
and result interception contract. Core gateway tools and brokered tools follow
the shared contract.
