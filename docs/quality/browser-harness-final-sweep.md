# Final browser harness content sweep

Reviewed against `b70adff6eaeca3d4e633fdb32035fcc0c4e3834c`.

The sweep covered browser execution and research modules, tool discovery and
suggestions, skill loading, UI demo data, usage scenarios, examples and remaining
security terminology. No additional built-in attack execution workflow was found.
This is a review of product content and prescribed workflows, not a claim that the
codebase has no vulnerabilities or that generic tools cannot be used offensively.

## Finding and repair

`previewApproval` and the usage recording's mission adapter still illustrated
`nmap.service_detection` with scan arguments. The adapter also used the rejected
legacy `active_scan` risk class, which the UI mapped to its fallback risk.
An independent read-only review confirmed that these are inert data: the usage
adapter intercepts requests and updates a local boolean without dispatching a
command to Core. The preview object has no runtime consumers.

Both now illustrate `browser.navigate` with its required `tab_id` and `url`.
The wire fixture uses `network_action`; command argv and container metadata are
removed. The scenario assertions and recording guide match that example.

The Nmap observation parser is retained. It reads already-returned stdout and
emits port observations; it does not select targets, construct commands or execute
a scanner. Tool installation metadata, operator-selected skills, advisory/KEV
terminology, security hardening and retired-URL notices are also retained.

## Acceptance boundary

This changes sample content and recording fixtures, not production controls,
layout, execution, API, persistence or authorization behavior. Unit coverage
checks the preview and intercepted approval lifecycle. No real-Core, refresh,
reconnect, physical-device or new recording result is claimed; those runtime
journeys are unchanged. The usage video was not re-recorded.

Selected validation: two Vitest cases in `ui/src/data/demo.test.ts`; the single
Nmap observation-parser regression; the packaged product-knowledge source-digest
check; and the production UI build. The bundled knowledge generator produces the
same source digest because this recording guide is not an indexed source.
CI is bound to precisely those four tests, with native and Playwright selections
empty. No deployment or live Core restart is included.
