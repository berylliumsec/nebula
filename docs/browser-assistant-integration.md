# Browser Assistant implementation contract

Branch: `codex/browser-assistant-integration`, isolated from the running installation.

## Operator journey and authorities

| Journey | Observable invariant | Authority | Validation |
| --- | --- | --- | --- |
| Discover/open | Integrated Chromium and native browser are distinguishable | Core capabilities | component + packaged UI |
| Create/select | Visible browser and conversation identify saved objects | Core database + URL | real Core |
| Capture/ask | Operator previews bounded page context before sending | immutable capture + Core transcript | component + real browser |
| Stream/follow up | Answers and approvals remain beside the page | Core conversation | real provider and harness |
| Control/takeover | Actions target the attached tab revision; takeover stops queued work | Core dispatcher + Chromium | integration |
| Refresh/reconnect | Transcript returns; mutations are not replayed | Core database | real Core + LAN |
| Failure/retry | Recovery distinguishes saved conversation from lost live page | Core runtime status | integration |
| Fork/delete/revoke | Conversation lifecycle follows shared Assistant behavior; revoked control fails closed | Core | integration |

All rows are required. Chromium owns live page state; clients own only transient UI and drafts.

## Implementation scope

Persistent inline conversation; shared host Chromium viewing and manual control; text,
DOM-element and region captures; authenticated Core transport; provider/MCP parity;
inline approval of consequential actions and explicit operator takeover. Native browser
remains available separately. No external MCP onboarding, arbitrary script tools,
proxy attack tools, autonomous security testing, production deployment or cookie migration.

## Required evidence

Record commands/results, build identity and origin. Test desktop Chromium 1024/1440;
mobile Chromium and WebKit 320/390/430 with portrait/landscape; keyboard, touch,
focus, screen-reader names, reduced motion, zoom, long content and keyboard geometry.
Exercise packaged desktop, production LAN, real Core, a configured provider and harness,
and a physical mobile browser. Mock/fixture passes do not satisfy live acceptance.

## Status

Core integration implemented in the isolated worktree; the full plan remains
partially verified and is not ready for an unqualified completion claim. Nothing
was merged, pushed, deployed, or changed in the running installation.

### Implemented

- Shared Chromium surface with authenticated Core streaming, manual input, tab
  creation/closure/selection, persisted active tab, and selectable viewport sizes.
- The same Assistant presentation and state used by the main chat view, including
  streaming, follow-ups, settings, context preview, cancellation, and approvals.
  Browser handoffs retain the selected conversation and native/managed surface.
- Operator page/text/element/region capture. Images use the existing verified
  provider image-attachment path. Text-only runtimes have an explained disabled
  region selector. URL credentials/query strings/fragments are removed before
  model transfer; input values and marked sensitive DOM regions are excluded.
- `browser.companion` provider and harness MCP dispatch through the same broker.
  Consequential operations wait for inline approval and return their actual result.
  Page revisions, conversation/project ownership, expiry, cancellation, duplicate
  decisions and takeover are checked. This tool has no arbitrary script or
  security-testing operations.
- Core persists conversation bindings and approval receipts. The new action kind
  is registered with the existing generic entity store; no table rewrite is needed.
  Closed Chromium contexts are removed, and retry re-queries live tabs while
  distinguishing lost page state from saved conversation history.

### Validation, 2026-09-07

```text
Journey: page context preview -> attach -> question -> inline answer -> follow-up;
         browser view and durable conversation ID remain selected (API fixtures).
Backend: PYTHONPATH=src poetry run pytest -q tests/v3/test_harnesses.py
         tests/v3/test_chat.py tests/v3/test_browser_companion.py
         tests/v3/test_browserd.py tests/v3/test_browser_security.py
         tests/v3/test_browser_engine.py
         86 passed; one existing Starlette TestClient deprecation warning.
UI: npm run test -- --run src/components/ManagedAssistantBrowser.test.tsx
    src/components/WorkbenchBrowser.test.tsx src/state/WorkbenchDraftContext.test.ts
    src/state/WorkbenchDraftContext.navigation.test.tsx
    23 passed.
Chromium runtime: real headless Chromium exercised capture/redaction, changed-page
                  rejection, and new/close tab lifecycle on controlled local content.
Approval: real durable store plus fixture executor; assistant waited for approval
          and received the completed action result. Not a live provider test.
Real Core: TestClient authentication rejection and actionable missing-runtime 409.
Production bundle: npm --prefix ui run build passed; existing chunk-size and mixed
                   Tauri event import warnings remain.
Build identity: ui/dist/index.html SHA-256
               7100b05455894805304d322d128102cbde7dff72420ece1e8976fce00594aa6e
Production/LAN: http://192.168.1.155:15431, isolated Vite production preview.
Desktop Chromium: desktop 1440 and compact 1024, passed.
Mobile Chromium: small 320, Pixel 5 profile, wide 430, passed (emulated).
Mobile WebKit: small 320, iPhone profile, wide 430, passed (emulated).
Browser command: NEBULA_UI_TEST_HOST=192.168.1.155 NEBULA_UI_TEST_PORT=15431
  NEBULA_UI_TEST_COMMAND='npm run preview -- --host 0.0.0.0 --port 15431'
  npm run test:e2e -- --grep 'browser Assistant stays' --project desktop
  --project compact --project mobile-chromium-small --project mobile-chromium
  --project mobile-chromium-wide --project mobile-webkit-small
  --project mobile-webkit --project mobile-webkit-wide --workers 1
  8 passed. APIs and Assistant responses are fixtures, not live service evidence.
Static checks: Ruff and git diff --check passed.
Physical device: not run.
```

The first mobile runs exposed overlapping flex children; those were corrected.
A subsequent 320 px WebKit follow-up activation was intermittent. Focus retention
and icon hit targets were adjusted; the final two eight-profile LAN runs passed.
Those observations do not replace physical Safari/keyboard acceptance.

### Remaining work and evidence

- Qualify the complete streamed browser and action workflow against a headed,
  installed `browserd`, its policy proxy, and real provider/harness connections.
  This execution environment has no `NEBULA_BROWSERD_URL`, `NEBULA_BROWSERD_TOKEN`,
  `NEBULA_BROWSERD_POLICY_PROXY_URL`, or display configured. No production readiness
  restriction was weakened to make a test pass.
- Complete the packaged desktop, physical mobile, portrait/landscape, keyboard,
  accessibility, refresh/reconnect, concurrent-viewer, revocation and process-failure
  matrices using that real runtime. Only the bounded checks above were exercised.
- Model-initiated screenshot delivery through MCP is not implemented; screenshots
  currently require the operator to select and attach a region. Protected-reference
  filling and remote file-upload workflows also remain to be integrated. The model
  currently receives a refusal for sensitive controls and asks for manual entry.
- Browser runtime installation is not performed by Prepare / retry; it reconnects
  an already configured host runtime. First-time host provisioning remains separate.

These are retained as implementation/acceptance gaps, not waived requirements.

### Completion-goal validation pass (2026-09-07)

The completion, PR and merge goal remains active. The operator offered a physical
phone; device connection details and physical evidence are pending.

The real-Core check exposed an initial-open failure that fixture UI tests missed:
Core persisted an active tab without its required durable tab membership. Core now
updates tab membership and selection together, retaining Chromium as live-page
authority. Manual REST actions now pause assistant control before waiting for the
queue, as streamed input already did. Cancellation revokes pending approvals and
settles the tool ledger. Attachments include bounded element structure. Harness
browser tools use the shared gateway concurrency gate. Imports and typing were
corrected to satisfy packaging and static checks.

An isolated full headed Chromium runtime was staged with the repository's staging
script at `/tmp/nebula-companion-validation/playwright-browsers`, under Xvfb.
Playwright 1.61.0; Chromium revision 1228; executable SHA256
`2d18db9d8608b052b6a552ee00ec1e830f93692e928b65ecc67d693bd33fe801`.
Profiles and Core databases use temporary directories. No installed service was
modified. This is a controlled proxy fixture, not production proxy qualification.

Passed repeatable commands:

```sh
PYTHONPATH=src xvfb-run -a poetry run python scripts/smoke_test_browserd.py --headed --runtime-root /tmp/nebula-companion-validation/playwright-browsers
PYTHONPATH=src xvfb-run -a poetry run python scripts/smoke_test_browser_companion_core.py --runtime-root /tmp/nebula-companion-validation/playwright-browsers
PYTHONPATH=src poetry run pytest -q tests/v3/test_browser_companion.py tests/v3/test_chat.py tests/v3/test_packaging.py
PYTHONPATH=src poetry run mypy src/nebula/v3
```

The first smoke observed a visible change, stale-revision rejection and existing
durable duplicate/pause behavior. The second used real Core and browserd loopback
HTTP/WebSocket connections: authentication, initial open, navigation, visible click
result, conversation binding/reopen, relayed frame, resize takeover, and viewer
disconnect preserving the binding passed. It does not exercise a provider answer,
packaged desktop, production LAN origin, or physical phone.

The focused Python set passed 63 tests. Type checking passed all 88 source files.
The full backend run initially reported 740 passed, 5 skipped and one packaging
import-placement failure; that failure passed after correction. The full rerun
passed 742 tests with 5 skipped (173 seconds initial run, 166 seconds rerun).
UI component checks passed 17 tests in three files. The production build
passed; index HTML SHA256 is
`e606b1613f59b4864b2e3e8641ceb11d60a3a39e1682db70733198eb575720ae`.
The same eight-profile LAN command recorded above passed again against this bundle
(API/provider fixtures). CI now installs Chromium for the real-browser Python test.

The screenshot-tool, protected-reference, upload, live-provider/harness, packaged
desktop and broader lifecycle/mobile gaps listed above remain open.

### Screenshot and live harness milestone (2026-09-07)

Browser tool screenshots now become durable conversation-owned image artifacts.
The provider routing/final-answer path adds the latest image only for a verified
vision profile; MCP returns an image content block. Text-only or unknown harness
models do not advertise region capture. Codex model discovery reads the installed
protocol's `inputModalities`; API/UI model options expose `image_input`/`imageInput`.
Operator image attachments in harness chat still require separate integration.
Input, textarea, contenteditable and explicitly sensitive fields are masked in
region screenshots. Literal page content remains untrusted.

Captured controls retain actual DOM node identity in a browser-side handle. Replacing
a button with an identical clone changes its page revision, and actions use an
element handle rather than looking up a new node at the old list position. A real
Chromium regression covers identical replacement and stale rejection.

The managed Codex host was unable to execute MCP while `code_mode_host` was forced
off. A live test confirmed enabling that stable host restores tool dispatch while
code mode, shell, native browsing and computer control remain disabled. Managed
gateway sessions now use the existing approval callback path instead of `never`;
Core still owns consequential-action approvals. The initial `never` run was blocked
by the harness's automatic approval review before reaching Core.

The configured default Codex home failed token refresh. Using the operator-approved
`/home/agent/.codex-2` through an isolated temporary launcher authenticated successfully;
the installed profile and service were not changed. This passed:

```sh
PYTHONPATH=src xvfb-run -a poetry run python scripts/smoke_test_browser_companion_core.py --runtime-root /tmp/nebula-companion-validation/playwright-browsers --harness-source-db /home/agent/.local/share/nebula/v3/nebula.db --codex-home /home/agent/.codex-2
```

Observed with live `gpt-6-astra`: MCP tab discovery, image screenshot result, answer
reporting the visible button label, follow-up on the same conversation, pending
Core click approval, approval completion, and the real page changing from Saved
to Ready. The test also verifies a second viewer receives an 844x390 frame after
the first disconnects. Evidence from the first model turn is retained at
`/tmp/nebula-companion-validation/last-harness-events.json` (controlled fixture only).
This is not a packaged UI or physical-device acceptance claim.

Focused browser/chat/harness tests passed 73 tests. Static type checks passed all
88 source files. The production bundle built successfully; index SHA256
`0e8854cea4f091555804ceb4770564692949fbd65d1218129cd322a7df3b6cdb`.
No local Ollama endpoint was available for a real provider check. Live provider,
protected-reference filling, uploads, harness image attachments, full browser UI
journey, packaged desktop, LAN with real runtime, physical phone, and remaining
lifecycle gates are still outstanding. The earlier screenshot-tool gap is closed
at implementation, focused-test and live-harness layers only.
