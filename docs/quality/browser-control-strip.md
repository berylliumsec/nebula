# Browser controls and Assistant settings

## Acceptance contract (before implementation)

Journey: open project Browser, navigate and use page tools, hide/show controls,
open utilities without shrinking the page, open Assistant settings and reach every
option, close with Escape and return to its trigger.

| Step | Observable invariant | Authority | Test |
| --- | --- | --- | --- |
| Discover/select/use | Named icon strip; expanded chrome stays compact; one toggle hides it | React presentation; Core owns tabs | component + production Playwright |
| Hide/show | Address drafts, selected tab, stream and page context survive | React draft; Core session | component + real Core |
| Utilities | View, files and credentials expand over the page without reducing its height | React disclosure; Core catalog | production geometry + real Core |
| Settings | Viewport-contained panel; all fields scroll into reach; sensible field order | React draft; Core capability catalog | production Playwright |
| Failure/retry | Errors and pending approvals remain visible with controls hidden | Core action/error | component + real Core |
| Refresh/reconnect | Existing session and conversation recover normally | Core + URL | real Core smoke |
| Create/delete/revoke | Existing tab, file and credential operations unchanged | Core | existing component + real Core smoke |
| Stream/interrupt/background | Collapsing chrome does not remount or close browser stream | Core/WebSocket | component + real Core smoke |

Themes remain authoritative through existing CSS variables. No new stored preference
or backend authority. Test production desktop 1440/1024 and emulated mobile Chromium
and WebKit 320/390/430, short landscape, keyboard/focus/tooltips and reduced motion.
Run build, targeted component regressions and real-Core/LAN browser workflow. Physical
Mac installation is user-owned; physical Mac and software-keyboard checks unavailable.
