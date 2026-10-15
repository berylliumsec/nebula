# Butler capability recovery

Operator journey: in the selected project, ask Butler to give an existing main
agent a missing capability, then retry its task. Butler finds the agent in the
Core work roster, reads its saved Assistant settings and available tools, saves
the precise next-turn selection, sends the follow-up to that main agent, and
checks the resulting work. A queued follow-up remains in progress until Core
records the reply. Butler never messages a child directly.

| Step | Observable invariant | Authority | Focused check |
| --- | --- | --- | --- |
| Discover | Available MCP servers explain their purpose and usable tools; command tools and MCP selection have distinct descriptions | Core profile catalog | Core test |
| Configure | A selected, enabled profile is saved with the chat revision for the next message | Core chat session | Core test |
| Retry | The follow-up request includes the saved profile and goes to the parent main chat | Core queue | Core test |
| Observe | Butler waits briefly on the exact queued item, then reads its linked saved reply before claiming the agent completed the task | Core chat and queue | Core test and real Core journey |
| Recover | Unavailable tools, stale revisions, or a failed retry produce an actionable status without inventing a result | Core errors | Core test and real Core journey |

This changes Butler's tool discovery and chat settings description. It does not
grant a browser outside the project's scope, enable a disabled global profile,
or turn a saved setting into evidence that a search succeeded. Reconnect uses
Core's saved chat and queue; no browser-local state owns the capability.
