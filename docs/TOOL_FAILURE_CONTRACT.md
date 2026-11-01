# Assistant tool failures

`nebula.tool-failure/v1` is the model-facing failure envelope for Core-owned
tools. It carries the actual offered tool description, required argument names,
and effective input schema, plus a Core-authored problem and next action. The
schema is inline when it fits within 4 KiB. Larger schemas carry the relevant
field, a SHA-256 schema reference, and `schema_truncated: true`. The whole
envelope stays within the 8 KiB model-result limit; optional previews and long
descriptions are reduced before the schema or recovery instruction.

The complete effective schema and original exception are stored in Core's
`tool-failure-diagnostics` directory under a random diagnostic reference. That
directory is mode 0700 and its records are mode 0600. The model receives no
argument values, raw exception text, paths from exceptions, or diagnostic
record contents through this envelope. A failed tool-result receipt may supply
its existing bounded, redacted output preview and searchable artifact IDs;
those are labeled untrusted tool output. `diagnostic_available` states whether
the protected record was written.

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
| `error_code` | a stable Core code for a known built-in refusal, or `null` |
| `problem` | Core's fixed sentence for the category |
| `side_effects` | `none` or `unknown` |
| `invalid_input` | the argument at fault, or `null` |
| `usage` | bounded offered tool description, required argument names, and declaration source; `null` when the tool was never offered |
| `effective_input_schema` | the schema offered for the call (see above), or `null` |
| `schema_truncated` | `true` when only the relevant field is repeated |
| `schema_reference` | `<tool>@<version>:sha256:<digest>` of the full schema, or `null` |
| `next_action` | Core's fixed guidance for the category and effects |
| `retry_safe` | whether reissuing the call can be correct (see each category) |
| `diagnostic_reference` | the protected record's reference, or `null` |
| `diagnostic_available` | whether that record was written |
| `limit` | only for `capacity_reached` and `budget_exhausted` (below) |
| `result_receipt` | only for a failed Core receipt: status, tool call ID, receipt artifact ID, safe summary and preview, and searchable output artifact IDs when available |
| `available_tools` | bounded offered names when a gateway call used an unavailable tool |

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
| `conflict` | A selected item or revision changed | Read fresh state and verify the earlier effect before a new call | No unchanged retry |
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
`unavailable_resource`, `unavailable_tool` or `budget_exhausted`. A call with unknown effects has
`retry_safe: false` and tells the assistant to inspect recorded state before
issuing another call. Missing and unauthorized resources use the same public
description, so the result cannot reveal whether a resource exists elsewhere.

When a command or MCP call returns a failed `nebula.tool-result/v2` receipt,
Core keeps the receipt's `tool_call_id`, bounded output preview, and searchable
artifact IDs in `result_receipt`. The model can use `tool_output.search` with
`tool_call_id` or `tool_output.read` with a searchable output artifact ID if
those tools are offered. The receipt artifact ID itself is an audit reference,
not a searchable output artifact. The preview is data from the tool, never an
operator instruction. For unknown side effects, inspecting output does not
authorize repeating the call; the assistant must check recorded state first.

Brokers classify by exception type, not by message: `InvalidToolArguments`
(and schema `ValidationError`), `CapacityReached`, `BudgetExhausted`,
`ToolNotPermitted` and other `PolicyDenied`, and a refusal marked
`refused_before_execution` in `nebula.v3.tools`. The provider-chat loop and the
harness gateway pass the same exception to `tool_failure`, so one refusal has
one category on both paths.

Butler discovery and invocation reject an unlisted operation, an incorrect
read/change route, or an invalid nested `arguments` object before dispatch.
These return `invalid_arguments`, `side_effects: none`, and a fixed instruction
in `next_action`. An unlisted operation tells the assistant to search the
Butler catalog by intent, describe a returned action, and invoke that action
with its advertised tool and input schema. A separately offered `work_list`
tool is the route for Work items; a harness `work.list` name is not a Butler
operation. If neither route is offered, the assistant must report that it
could not read fresh state. These messages never echo the attempted operation,
nested arguments, or the original exception.

Notebook and Security Tools are Core-owned built-in MCPs. Their offered
declarations include the built-in profile's usage instruction. Notebook path
refusals explain the project-relative `.ipynb` rule and identify `path`; a
missing headless Security Tools session tells the assistant to read `status`
and use an authorized `navigate` call to start it. Notebook conflict, consent,
and runtime failures give distinct Core-authored recovery steps. A disabled
MCP server, revoked grant, unavailable built-in service, and schema rejection
have stable codes and instructions. Only pre-dispatch checks prove
`side_effects: none`; a conflict or runtime error after execution begins keeps
`side_effects: unknown`.
Security Tools also maps known static session and proxy refusals to specific
codes and recovery actions; an unrecognized runtime `ValueError` receives a
safe status-and-state instruction, not raw exception text. Bounded output query
errors name the field to correct, while a Work revision conflict tells the
assistant to read the current item and verify the prior effect before a new
call.

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
| Core tried to start a subagent or its next round and could not (and a retry of that same start) | `execution_failed` |

A retried start (a vendor retry of the same call, a replayed step) returns the
child the first call started with its current `status`: `running`, or the
state it finished in, not a new child and not a start failure.

## Browser companion refusals

| Refusal | Category |
| --- | --- |
| Arguments the request cannot hold, a region capture the runtime cannot receive | `invalid_arguments` (before execution) |
| A tab no longer open, a file reference outside an upload, a credential reference that does not fit | `invalid_arguments` naming the argument (before execution) |
| A region screenshot too large to keep (the capture changes nothing) | `invalid_arguments` (`width`), `side_effects: none` |
| A page outside the project's scope | `permission_denied` (before execution) |
| Control paused, a Nebula approval handed to the browser | `permission_denied` (before execution) |
| Another project's browser, a detached, closed or revoked browser session, a replayed screenshot that is gone | `unavailable_resource` (before execution) |

A limit is checked after the rules, so a call that is not permitted is never
told to wait and retry, and a goal's budget before the running-at-once limit,
so a call waiting cannot help is never told to wait. A harness conversation
has no Nebula goal, so `goal_tokens` only reaches provider chats.

## Known boundary

Vendor-native tools run inside the provider harness. Nebula does not receive
their effective input schemas or control the result returned by that SDK to its
model. Those failures remain provider-owned until the harness offers a schema
and result interception contract. Selected third-party MCP tools use the
offered description and schema in this contract, but their own error text is
untrusted and cannot be promoted to a Core-authored correction. Core gateway
tools, brokered tools, and built-in MCPs follow the shared contract.
