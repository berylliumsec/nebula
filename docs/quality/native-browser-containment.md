# Native browser containment

## Operator contract

Entry: Workbench → Project browser → Native browser → open an in-scope page.
The page starts immediately below the address bar and remains within the visible
Browser surface. Opening the research workbench, changing its width, expanding the
sidebar, resizing the window, or changing text size must move and resize the native
page with that surface. The page must never cover Nebula controls. A hidden tab or
an inactive Browser must not intercept input.

| Journey step | Observable invariant | State authority | Test layer |
| --- | --- | --- | --- |
| Discover/select | Native browser and its address control remain visible and usable | React view state | Playwright desktop and mobile fallback |
| Create/use | In-scope navigation places the native page inside the current surface | DOM surface bounds; AppKit child frame | Playwright bounds receipt; packaged macOS visual check |
| Resize/occlude | Sidebar, research panel, zoom, and window changes keep all native edges inside the surface | DOM bounds; AppKit clipping view | Playwright bounds receipt; packaged macOS visual check |
| Refresh/reconnect | Restored tab and page return in the current surface without covering controls | Core tab record; React selection; AppKit frame | Existing session tests; packaged macOS check |
| Failure/retry | A failed native resize is diagnosed, and a later layout sync can retry | Tauri command result; React layout observer | Component and packaged macOS check |
| Hide/revoke | Inactive tabs and views stop drawing or receiving input | React visibility; AppKit view state | Existing visibility tests; packaged macOS check |

Streaming, browser data deletion, and proxy policy are outside this geometry
change. The browser URL and tab identity remain Core-owned; React owns temporary
selection and layout, while AppKit owns the native view's actual frame and clipping.

## Focused verification plan

Exercise the native-bounds journey in the existing desktop Playwright test and
focused component tests for geometry updates. Exercise the mobile browser fallback
at its 320–430 px boundaries because the native option is desktop-only. Build the
production UI. A packaged macOS build is the acceptance gate for rendered WKWebView
clipping, safe-area alignment, and input hit testing; Linux cannot substitute for it.

## Browser setting combinations

The capture setting matrix contains 24 combinations: three capture detail levels
(metadata, headers, bodies), proxy on/off, interception on/off, and upstream
chaining on/off. Eighteen are valid; the six combinations with interception on
and the capture proxy off must be rejected. An upstream configuration can remain
saved while the capture proxy is off, ready for a later enabled session, but has
no native effect until then. Each valid combination must round-trip through Core
with the exact saved values. The UI must show which settings are active, disabled,
or require CA trust, and failures must reconcile the saved and native states.
Sessions saved by older versions with interception on and the capture proxy off
show interception as inactive and clear the invalid value on the next capture
setting change.

The Session pane puts the capture proxy decision ahead of optional upstream
configuration, presents live capture state accurately, and keeps the upstream
form behind a disclosure. Session, Identities, and Actions use the full tool
width instead of reserving an unrelated assessment rail. A disabled upstream
form explains which prerequisite is missing.

The interaction matrix also covers local/remote Core, desktop/paired web client,
empty/single/multiple tabs, default/alternate identity, scoped/blocked target,
research pane open/closed, Assistant open/closed, sidebar expanded/collapsed,
window resize, and 100%/nondefault text size. These dimensions are checked on
their relevant journeys rather than multiplied into one enormous test run.

## Audit evidence and remaining gate

| Boundary | Focused evidence |
| --- | --- |
| Core capture settings | `tests/v3/test_browser_security.py`: 33 passed, including all 24 capture/proxy/interception/upstream combinations; invalid states leave the stored session unchanged. |
| React settings and research panel | Two component files: 36 passed. These include native-save mismatch/retry, capture enable from an inconsistent older session, interception rollback after a Core failure, upstream native-apply failure, CA rotation/revocation abort when disabling capture fails, and compact research layout. |
| Native frame math | Two exact Rust browser tests passed on Linux. The macOS CI job compiles the AppKit code with these selections. |
| Desktop and paired layout | 63 selected mocked-browser Playwright cases passed across desktop Chromium, emulated mobile Chromium and WebKit at 320/390/430 px, and emulated landscape profiles. The desktop case records Session screenshots and asserts browser bounds after research, Assistant, sidebar, zoom, and resize changes. |
| Durable browser journey | Two real-Core Playwright cases passed over a non-loopback LAN origin with the production UI. One checks the served browser bundle against `web-build.json`, scope and research navigation, and a saved Repeater draft after reload. The other checks device-browser handoff, accessibility, touch targets, and short-window layout. |

The remaining acceptance gate is a packaged macOS run with a real WKWebView: verify
that the page begins immediately below the address bar and stays clipped during
window resize, Assistant/research/sidebar changes, tab switching, and hide/show.
The Linux host cannot observe AppKit compositing or native input hit testing.
Physical iPhone and Android checks were not available; mobile evidence is browser
emulation. Until the macOS gate passes, native visual containment is partially
verified rather than confirmed in the affected desktop app.

## Follow-up: native page overdraw in the macOS development app

An operator screenshot after a fresh `dev:desktop` build shows the native white
page extending past the Browser surface while the DOM's white surface stays inside
the panel. The earlier `NSView` clipping attempt therefore did not establish the
visible invariant. The clipping boundary is now an `NSClipView` with the child
WKWebView as its document view; the child is explicitly framed to its bounds on
create and resize. The main WKWebView's `pageZoom` also converts CSS-pixel surface
bounds to AppKit points when Text size differs from 100%.

An independent macOS 27.2 AppKit probe confirmed that `NSClipView.documentVisibleRect`
and its oversized WKWebView's `visibleRect` remain limited to the clip frame after
resizing. This probe is not a Nebula visual acceptance result. In the affected
Mac development app at `579ff9ec`, the white page still covers part of the URL
field, both with and without Assistant open. That build attached the clip to
Nebula's main WKWebView, although Wry created the browser tab beside it in the
window's native content view. The follow-up keeps the clip under that original
parent and checks both webviews share it before showing the page. At that point,
its placement was a hypothesis pending the next Mac run, recorded below.

For this follow-up, the two exact Rust frame tests passed, all nine selected
desktop Chromium Browser cases passed against this worktree's development server
on a dedicated port, and the production UI bundle built. These tests exercise DOM
bounds and native command dispatch, but cannot observe AppKit's rendered page.

Repeat the real Browser journey at 100% and a nondefault Text size, including
resize, Assistant open/closed, tab switching, hide/show, and input at each clipped
edge before native containment can be called verified.

## Source comparison and next Mac check

The operator's next Mac screenshot still showed the page covering the lower part
of the address bar after the clip moved to Wry's parent. This rejects the parent
change as a complete fix. In that image, the white page begins about one macOS
title-bar height above the expected Browser surface. The React rectangle is
measured in the main WKWebView, while the clip is attached to its parent. Passing
the same numbers to that parent skips the offset between those views. The next
revision converts the frame between those two AppKit coordinate systems with
`convertRect:toView:` on create and resize. An opt-in `NEBULA_BROWSER_GEOMETRY`
debug trace prints the DOM bounds, both native view geometries, and the converted
frame for the affected Mac build. This is a code-level diagnosis supported by the
screenshots; only a rebuilt Mac run can confirm the rendered result.

Browser Use Desktop measures the browser slot and sets a separate Electron
`WebContentsView` to exactly that rectangle before attaching it. OpenHands and
Cline render browser screenshots in their UI, avoiding a live native page
composited over their controls. VS Code's Simple Browser uses an iframe and
cannot serve as a general arbitrary-site replacement. Sources:
[Browser Use slot](https://github.com/browser-use/desktop/blob/f073b7574f7927185ebbebd87556391d5cb0cfd1/app/src/renderer/hub/AgentPane.tsx#L722-L736),
[Browser Use native view](https://github.com/browser-use/desktop/blob/f073b7574f7927185ebbebd87556391d5cb0cfd1/app/src/main/sessions/BrowserPool.ts#L619-L696),
[OpenHands panel](https://github.com/OpenHands/OpenHands/blob/02e02ae0c92ebb033afcf2fb399bd3683df82ca8/src/components/features/browser/browser.tsx#L6-L25),
[Cline browser](https://github.com/cline/cline/blob/a48408909bf9b2ff7f620d420be8547fb509e8ed/apps/vscode/src/services/browser/BrowserSession.ts#L212-L225),
[Cline screenshot panel](https://github.com/cline/cline/blob/a48408909bf9b2ff7f620d420be8547fb509e8ed/apps/vscode/webview-ui/src/components/chat/BrowserSessionRow.tsx#L140-L186),
[VS Code Simple Browser](https://github.com/microsoft/vscode/blob/72b3e1558739d58ace9bcb200578d4508c7c00d5/extensions/simple-browser/src/simpleBrowserView.ts#L143-L170),
[AppKit coordinates](https://developer.apple.com/documentation/appkit/nsview/convert%28_%3Ato%3A%29-3cqqt).
