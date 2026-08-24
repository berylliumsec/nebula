# Nebula AI Security Browser inventory

Status: implementation contract for `codex/ai-security-browser` at
`47daf3feda88a469f5f563662c0d2bb6d9225a56`.

## Operator journey and acceptance contract

The operator enters **Workbench > Browser**, opens an authorized target in the
Project-isolated native profile, sees whether the live URL matches the durable
Project scope, captures a bounded semantic snapshot only when the final page is
confirmed in scope, reviews that
snapshot in Chat, and explicitly submits it to the selected provider or harness.
The page is never sent to AI in the background and captured page content is
carried through the existing exact-hash, untrusted-context boundary.

| Journey step | Observable invariant | State authority | Test layer |
| --- | --- | --- | --- |
| Entry/discovery | Browser, target-scope state, and AI action are understandable without product knowledge | Workbench URL + Core scope | component + Playwright |
| Navigate | Only HTTP(S) URLs without embedded credentials open; the final live URL updates the tab and scope badge | native webview + Core scope | Rust + component |
| Capture | One explicit action returns a bounded rendered-text, form, and link snapshot with URL/title provenance and no input values or cookies; missing, inactive, outside, or changed scope fails closed | live native webview + Core scope | Rust + component + desktop dogfood |
| Review/use | Capture opens Chat as an editable, removable attachment; it does not submit a turn | Workbench draft context | component + real Core |
| Send | The reviewed attachment is hashed over its exact UTF-8 text and marked as untrusted data for the model | Chat request + Core | existing UI/Core regressions + real Core |
| Failure/retry | Capture and scope-load failures remain actionable in Browser; retry is safe and does not duplicate a chat turn | component transient state | component + desktop dogfood |
| Refresh/reconnect | Scope reloads from Core; browser profile persistence remains Project-scoped, while open-tab restoration is explicitly not yet supported | Core + native profile | real Core + desktop dogfood |
| Clear/revoke | Clear closes tabs and removes only this Project's browser storage | native browser state | existing Rust + desktop dogfood |

Lifecycle coverage: discover, navigate, capture, review, send, fail/retry, switch
views, reconnect, and clear are required. Stream/interrupt belongs to the existing
Chat lifecycle. Browser-session fork and durable tab restoration are deferred.

## Current baseline

Nebula already provides Project-isolated browser storage, a native system
webview, HTTP(S)-only navigation, tabs and navigation controls, bounded staged
downloads into Project Files, public-page ingestion into Sources, and explicit
Project-data clearing. The web/LAN UI honestly falls back to the device browser
instead of pretending it can embed the desktop webview.

The scope badge added by this work is advisory context for manual browsing, not
an executable policy gate. It does not claim that the native webview blocks an
operator from leaving scope.

## Baseline gaps blocking a leading security-research browser

The benchmark is deliberately composite. Burp's current bar is a mature
request/response workbench plus scope-aware AI assistance
([tools](https://portswigger.net/burp/documentation/desktop/tools),
[AI features](https://portswigger.net/burp/documentation/ai-features)); general
AI-browser frameworks set the bar for observe/extract/act workflows and
deterministic action reuse
([Browser Use](https://docs.browser-use.com/cloud/agent/quickstart),
[Stagehand](https://github.com/browserbase/stagehand/blob/main/packages/docs/v2/first-steps/quickstart.mdx)).
Nebula has to meet both bars while retaining Project scope, evidence provenance,
operator approval, provider policy, and the rest of the Workbench lifecycle.

1. **No live-page AI context.** AI can receive a public URL through Sources but
   cannot inspect the authenticated page the operator is actually viewing.
2. **No visible scope decision.** The durable Project scope protects executable
   tools but is absent from Browser navigation, where researchers spend most of
   their time.
3. **No traffic workbench.** There is no HTTP history, request/response viewer,
   interception, modification, replay, diffing, WebSocket inspection, or HTTP/2
   tooling comparable to a mature security proxy.
4. **No governed browser actions.** Nebula has harness browser capabilities, but
   no same-session observe/propose/approve/act bridge into this native profile.
5. **No browser evidence primitive.** Screenshots, DOM snapshots, and request
   pairs cannot yet become immutable Evidence records with hashes and provenance.
6. **No durable research trail.** Tabs, navigation history, captures, annotations,
   and AI action receipts do not survive app restart as a reviewable timeline.
7. **No identity matrix.** Researchers cannot create named cookie jars, compare
   two roles side-by-side, or run authorization-difference checks.
8. **No extension/proxy ecosystem.** There is no compatible interception proxy,
   upstream proxy configuration, CA lifecycle, devtools, or extension API.
9. **No deterministic automation ladder.** There is no separation between
   inspect/extract, proposed single action, approved deterministic replay, and
   bounded agentic exploration.
10. **Mobile/LAN is a handoff, not continuity.** It can open or index a URL but
    cannot resume the desktop browser session or inspect its live state.

## Upgrade sequence

This worktree implements the prerequisite first slice: live semantic capture,
scope status, and explicit Chat handoff. The next complete slices should be:

1. immutable capture-to-Evidence with screenshot/DOM/request provenance;
2. a local interception proxy and HTTP history/replay/diff workbench;
3. proposed browser actions with scope checks and per-action approval;
4. deterministic action receipts/replay before multi-step autonomy;
5. named identities and authorization-difference workflows;
6. durable, reconnectable browser research sessions.

The do-not-claim boundary is strict: semantic capture does not make Nebula a
Burp replacement, does not prove request/response visibility, and does not prove
that a provider or harness acted inside the live native session.
