# Assistant link routing contract

Entry: a link in an assistant transcript in a project Workbench conversation.

| Journey step | Observable invariant | State authority | Test layer |
| --- | --- | --- | --- |
| Discover | Workspace files and HTTP(S) URLs remain ordinary, keyboard-accessible links; unsafe and out-of-workspace targets are inert. | Rendered message plus selected project workspace | Unit + Playwright |
| Select/use | On a wide screen, a file opens in Code beside the conversation and a website opens in Browser beside it; on a compact screen the matching full view opens. A second click opens the new target. | URL for view, side, path, line and requested URL; Core for file bytes and browser tab | Component + Playwright + real Core |
| Stream/interrupt | A link in a streamed answer can be opened without sending another assistant turn. Interrupting the answer does not undo the chosen pane. | Transcript stream and URL | Playwright |
| Refresh/reconnect | The same conversation and target return after reload; a repeated link click is a new navigation request. Browser session and scope remain authoritative in Core. | URL plus Core browser session and file workspace | Playwright + real Core |
| Failure/retry | A browser runtime or scope refusal appears in the pane; the conversation stays available and the operator can click again after recovery. | Core error and transient pane state | Component + real Core |
| Create/delete | Clicking a link creates no project object; deletion is outside this journey. | Core | Not applicable |

The Assistant browser keeps its read-only general operations route. Only an explicit link-open route may navigate a tab, and it reuses Core's authenticated browser session and project scope checks. File reads retain the editor's workspace boundary. The native browser remains the desktop destination; the managed browser serves LAN clients.
