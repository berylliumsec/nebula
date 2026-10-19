# Advisory task plans for agents

The **Ask for a plan first** setting applies to provider, Codex, and Grok
chats. Core asks the selected agent to make a concise plan and offers
`plan.submit` as a way to share structured steps. The agent keeps its normal
tools and permissions while planning. A missing, late, or changed plan does not
pause the turn. The setting is a preference, not a pre-action safety boundary.

| Journey step | Observable invariant | State authority | Test layer |
| --- | --- | --- | --- |
| Discover and select | The setting is available for provider, Codex, and Grok chats | Core catalog and UI | component and browser |
| Save and start | The next message uses the saved setting and selected runtime in the same chat | Core chat and runtime session | real Core |
| Plan and act | The agent receives the request and `plan.submit` while its ordinary tools remain available | Core turn and runtime | provider and harness integration |
| Delegate | Core adds a shared plan, if one exists, to the subagent's task; a harness-reported plan is a fallback | Core plan and subagent context | Core integration |
| Refresh | The saved setting, shared plan, and turn state survive reload | Core database | real Core and browser |
| Missing plan | The task continues and the activity view shows that a plan was requested but not shared | Core turn and plan | Core and browser |

Existing project scope, approvals, and tool policies still apply. The native
harness Plan only mode is a separate operator choice. Historical turns that
started under the older enforced plan flow retain their saved record; new
turns use advisory planning.
