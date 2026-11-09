# Provider tools acceptance fixture contract

Use only a fresh disposable Core root and its seeded project/provider/chat. Keep
normal pairing, authorization, production UI and real Core routes enabled. No
provider, MCP process, Jev, Docker or operator installation is contacted.

Copy `tests/v3/provider_tools_context_fixture.py` into each retained candidate
bundle as `acceptance_provider_tools_context.py`. Its manager initializer uses
common existing domain types. New renderer imports execute inside the retained
child. Select only the explicit `provider-tools-context` reset mode.

After creating the empty provider chat and before admitting API/execution
processes, call `await initialize(store, root=data_root, project_id=...,
session_id=...)`. Keep its return document in the receipt. It writes
`acceptance-provider-tools-context.json` in that owned root. Before the actual
project application's lifespan, call `configure_app(app, root=data_root,
lease=lease, version=version)`. This updates the actual
`app.state.control_assistant_work.chat` dependencies. Other reset modes retain
their existing adapter.

The stored provider remains `vllm/model-a`, with verified tools/streaming and a
12,000-token window with 2,048 output tokens. Jev is disabled. The session selects
**Synthetic-native-source**; **Synthetic-deferred-source** stays on demand.
Real catalog, ToolBroker, schema validation, policy and durable tool ledger use
harmless `AnalysisTool` handlers.

| Step | Existing Assistant action | Visible result | Durable evidence |
| --- | --- | --- | --- |
| Entry | Pair and open the seeded Assistant/chat | Saved provider/source selected | Core profile, scope and session reads |
| Learn | Send `[TOOLS_CONTEXT:LEARN]` | Catalog load/read activity, then `TOOLS_CONTEXT_LEARNED ABC` | One load/call and one complete managed definition |
| Continue | Send `[TOOLS_CONTEXT:USE]` | `TOOLS_CONTEXT_USED ABC` | Another call, zero extra loads, same definition digest |
| Compact | Send distinct `[TOOLS_CONTEXT:FILL] 1` through FILL 6 until compaction appears | Padding answers and positive Working context compacted-through boundary | Ready snapshot; summarizer receives no tool schemas |
| Refresh | Reload the same URL, then send a distinct USE | Same chat/history and answer | No duplicate submission/load/effect |
| Replace execution | After a completed turn, hand off this project to `acceptance-v2`; verify public deployment status and a new producer PID, then send a distinct USE | Reconnect and `TOOLS_CONTEXT_USED ABC` | Fresh ChatService reads the same manifest; zero extra loads |
| Fork | Fork at an earlier answer and select it | Selected historical transcript | Message revision resolves only boundary knowledge |

Restarting the supervisor while preserving its children does not prove
independence from ChatService memory. The final step requires a new execution
context with the same database and verified admitted release.

There is no manual Compact or Clear tool-results control here. FILL messages
exercise automatic compaction. The focused backend clearing test proves result
clearing separately; do not invent a browser control or conflate the two paths.

Use real context APIs/database reads for durable assertions. Sanitized
`acceptance-events.jsonl` adds `provider-tools-request`,
`provider-tools-operation` and `provider-tools-effect`. Assert one canonical
section per request, one complete deferred definition after initial loading,
zero schemas duplicated in replay, stable canonical SHA-256 when state is
unchanged, stable native declarations, one initial load total and one effect per
LEARN/USE. The provider refuses to silently reload a missing known schema.

The schema includes enum/pattern/string-length constraints, integer bounds,
both required fields and `additionalProperties: false`. The description includes
a newline, indentation and final space. All survive exactly. Cached-input usage
is zero; estimates measure context size, not live-provider cache savings.

Select the journey explicitly in applicable permanent desktop Chromium and
mobile Chromium/WebKit profiles. Record LAN origin, source/UI hashes, collection
count, engines/viewports, pairing, durable reads and missing gates. Remove the
owned fixture after capture. Browser evidence remains pending until execution.
