# Butler storage-source handoff contract

Journey: from a project-scoped Butler request, the operator reviews an exact storage source on the requesting device, supplies any callback credential reference there, and confirms enrollment, token rotation, or revocation. The one-time token is shown only in that browser. Core owns the source and its revision.

| Journey step | Observable invariant | State authority | Test layer |
| --- | --- | --- | --- |
| Entry/discovery | Butler can list only the selected project's nonsecret source metadata and prepare a typed action; the browser names the proposed action and source | Core source records and Butler's selected project | Core tool, component, real-Core browser |
| Create/mutate | Enroll creates a disabled source; rotate changes one exact source token; revoke disables that source and invalidates its token. Confirmation precedes each write | Core source database and revision; requesting browser for confirmation and credential reference | Core route and real-Core browser |
| Select/use | The browser rejects a changed project or stale source revision. The operator can copy the one-time token and webhook URL before dismissing them; Butler receives neither token nor credential reference | Selected project, Core source revision, browser transient state | Component and real-Core browser |
| Stream/interrupt | Not applicable: these operations are one request each. Closing a handoff before confirmation creates no source or token change | Browser transient state | Component and real-Core browser |
| Background | Background Butler cannot acquire a requesting-device handoff | Butler capability boundary | Core tool |
| Refresh/reconnect | Saved source status and revision return from Core. A one-time token is never reconstructed from storage after reload | Core database; browser memory only for one-time value | Real-Core browser |
| Failure/retry | A lost write response is reported as uncertain. The browser rereads the source list before any new attempt, so a retry cannot silently create another source or rotate a token again | Core readback and browser error state | Core route, component, real-Core browser |
| Delete/revoke | Revocation is confirmed against the same selected project and source revision; the refreshed list shows disabled status and a higher revision | Core database | Core route and real-Core browser |

The requesting browser must compare the returned source with a fresh authenticated Core read before saying the action succeeded. It must not put one-time tokens or callback credential references into chat receipts, assistant messages, logs, URLs, or browser storage. A failed readback keeps the result uncertain and directs the operator to inspect the current source before retrying. The existing Storage settings controls remain available for direct operator use.
