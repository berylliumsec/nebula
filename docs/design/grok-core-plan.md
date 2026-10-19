# Grok Core plan containment

The operator selects Grok as a main chat runtime, keeps **Plan before acting**
enabled, and optionally delegates to a provider subagent. Core owns the saved
assistant choice, bound chat runtime, task plan, and tool decisions. Grok owns its
conversation, but its planned turns must receive only Core's gateway tools.

| Journey step | Observable invariant | State authority | Test layer |
| --- | --- | --- | --- |
| Discover and select | Grok and Plan are available together; Butler can explain that settings apply on the next message | Core catalog and UI | component and browser |
| Save and start | A new message rebinds the same chat to Grok and retains the chosen provider subagent | Core chat and harness session | real Core |
| Plan and execute | A restricted Grok session can discover linked workspace files and submit a validated plan; no vendor-native or ambient tool runs before the plan | Core task plan and gateway | adapter, Core and real Grok |
| Tool use | Every execution tool goes through Core's declared-tool guard; an undeclared tool requires replanning before effects | Core gateway and task plan | Core and real Grok |
| Interrupt and refresh | The saved plan, turn status, and bound runtime survive interruption and reload | Core database | real Core and browser |
| Failure and retry | Unsupported or failed planning is visible and does not silently run an unplanned task | Core turn and task plan | Core and browser |
| Delete or revoke | Existing chat and profile removal rules continue to own cleanup | Core database | existing Core coverage |

For a planned Grok turn, Core uses an isolated account home containing only a
link to the selected account's authentication file. Compatibility integrations,
plugins, hooks, web search, and native subagents are unavailable. Native tool
execution is denied even if Grok proposes one.
Grok runs with a deny-by-default tool permission mode that explicitly allows
only the session-scoped Core MCP gateway. Core rejects ambient permission rules.
Grok runs from private scratch; the linked project workspace is available only
through Core's gateway. Core validates `plan.submit`, permits only declared
execution tools, and includes the saved plan in provider-subagent context.
Ordinary Grok turns retain their selected profile behavior.
