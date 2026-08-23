# Nebula Product Quality Gates

## Acceptance contract

Record this compact table before editing:

| Journey step | Observable invariant | State authority | Test layer |
| --- | --- | --- | --- |
| Entry/discovery | The operator can find and understand the feature | Core catalog plus UI | component + Playwright |
| Create/mutate | Success produces an immediately usable saved object | Core database | real Core |
| Select/use | URL and visible selection identify the same object | URL plus Core | Playwright |
| Stream/interrupt | Output is ordered and cancellation is recoverable | Core/harness activity | real Core |
| Refresh/reconnect | Durable content and active identity return without duplication | Core plus URL | real Core |
| Failure/retry | Failure explains recovery and retry is safe | Core error contract | component + real Core |
| Delete/revoke | The object disappears and stale identity is cleared | Core database | real Core |

Add feature-specific rows rather than deleting applicable lifecycle rows.

## Browser and viewport matrix

For operator-visible interface work, run the relevant journey in permanent
Playwright projects:

| Surface | Engine/profile | Required widths |
| --- | --- | --- |
| Desktop | Chromium | 1440 px and 1024 px |
| Mobile | Chromium Android profile | 320, 390, and 430 px boundaries as applicable |
| Mobile | WebKit iPhone profile | 320, 390, and 430 px boundaries as applicable |

Exercise portrait and landscape when viewport height or fixed positioning changes.
Use `visualViewport` assertions for keyboard-sensitive layouts. Label emulation
honestly; only report physical Safari when a physical device was used.

## State and content matrix

Check each applicable state:

- loading and reconnecting;
- empty and first-use;
- normal success;
- disabled or unsupported capability;
- actionable failure and retry;
- long titles, code, activity, and transcript content;
- active streaming and cancellation;
- background/resume and network transition;
- refresh, deep link, browser back/forward, and stale identifier;
- concurrent or forked sessions;
- deletion and revocation.

## Interaction and accessibility matrix

Check keyboard, touch, focus order, visible focus, screen-reader names, 44 px touch
targets, reduced motion, zoom, selection/copy, safe-area insets, software keyboard,
and absence of horizontal clipping. Run automated accessibility analysis, then
exercise the main journey manually because automated scans do not prove usability.

## Origin and build matrix

Use the production bundle for acceptance. Use a non-loopback LAN origin whenever
the change touches authentication, cookies, CSRF, Origin/Host checks, WebSockets,
PWA/service workers, clipboard, downloads, filesystem browsing, media, or APIs
whose availability differs between secure and insecure contexts.

## Completion evidence

Use this format in the final handoff:

```text
Journey: <entry through result>
Unit/component: <command and result>
Desktop Chromium: <project, viewport, result>
Mobile Chromium: <profile, viewports, result>
Mobile WebKit: <profile, viewports, result>
Real Core: <workflow and result>
Production/LAN: <origin, build type, result>
Physical device: <device/browser or not run>
Skipped gates: <none, or limitation and why>
```

A skipped required gate prevents an unqualified completion claim.
