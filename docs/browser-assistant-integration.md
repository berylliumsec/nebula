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
was deployed or changed in the running installation. The branch is pushed in draft
PR #251; it has not been merged. Historical checkpoints below retain their original
evidence and limitations. Codex with `~/.codex-2` is the operator-approved live runtime
target; separate provider coverage remains automated.

Latest runtime checkpoint: CI passed all seven jobs on `fce1bd3` (run `34150415312`).
The rebuilt production DEB passed the full live Codex journey at 1024x700 and
1440x900, with native wheel scrolling and clicks where needed. Both retained
acceptance runs exited zero. Physical
iPhone testing is waiting for the operator to enable Safari Web Inspector and
Remote Automation on the connected iPhone 16 Plus. Neither gate is waived.

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

### Protected-value and consent milestone (2026-09-07)

The browser panel can save, select and remove private values through the existing
CredentialStore. The UI defaults to session-only storage and explains restart
expiry. Only a browser-owned alias enters an assistant action; Core resolves the
value immediately before the approved fill. Unattached references and literal-text
protected fills fail. Removing a reference pauses control and revokes pending
actions. Expired approvals and unavailable credentials are explained in place.

Known values are redacted from returned page text. Browserd masks form controls
and matching echoed text in region screenshots. A bounded in-memory masking list
survives reference removal and Core reconnection until the live browser context
closes, so removing a fill grant does not reveal an already-echoed value. Raw values
are not persisted in action records or browser-session metadata. UI tests verify
the input is cleared and fill/context requests contain no literal secret.

Resuming browser control now participates in the existing remote tool-results
consent flow independently of command-runtime availability. The frontend CI
diagnostic-audit failure was corrected by integrating the existing diagnostic
logger with the component's visible error handling. The earlier PR run passed
Python 3.11/3.12/3.13, migrations, macOS desktop and security jobs; the next push
must re-run frontend CI rather than treating its earlier failure as waived.

Focused browser/browserd/chat/harness tests passed 79 tests, the browser component
passed six tests, both diagnostic audits passed, and type checks passed all 88
Python source files. The real-Core headed script passed protected fill approval,
actual input value observation, masking-pixel inspection of echoed text, and
redaction after reference removal, alongside its existing viewer checks. These use
controlled test values, not operator credentials.

Production UI build index SHA256:
`d92520e9ae916040bbbf5bae94b3ae5c2c8c8f03c3e913d5460851616470e7de`.
The production/LAN eight-profile regression passed on this build (26.4 seconds). The
operator selected Codex using `~/.codex-2` as the live runtime acceptance target.
A separately configured live provider is therefore no longer a required gate for
this change. Provider-path automated coverage remains distinct from observed
live harness behavior. Uploads, harness image attachments, the full real-runtime UI
journey, packaged desktop, physical-device acceptance and remaining lifecycle
gates remain open.

The live Codex workflow was rerun successfully after commit `08f43e8`, using
`--codex-home /home/agent/.codex-2` and model `gpt-6-astra`. The run included
protected fill and screenshot masking, MCP screenshot delivery, a visible answer,
same-conversation follow-up, inline approval and the observed page change.

### Harness image attachment contract

Journey: capture a page region or choose an image in the browser composer, preview
and remove it, send to an image-capable harness, then reopen the same transcript.
Core owns the validated metadata-stripped artifacts and durable message references;
the harness owns live conversation history. Client state holds unsent previews.
Image bytes are resolved and integrity-checked just before dispatch, never stored
in turn metadata or prompts. Reject unsupported models and cross-project artifacts
before creating a conversation. Verify adapter input, durable transcript, unavailable
models, ownership and a live Codex image answer; include the browser UI path in the
remaining real-Core production acceptance.

The operator attachment path passed the live headed Core/Codex script with
`--codex-home /home/agent/.codex-2`: a generated color fixture was uploaded through
`/chat/images`, sent as a structured image in the existing harness conversation,
identified correctly, and found in the authoritative saved message. MCP screenshots,
inline click approval and protected-value checks passed in the same run.

Focused harness/adapter/chat coverage passed 91 tests. The production bundle built
with index SHA256 `84041ad5f1a0c13933c9d82d928893daacdbe970b9625e4addf94041fd914631`.
The image-capable and image-unavailable composer cases passed 16 production-bundle
LAN fixture checks across desktop 1440/1024 and mobile Chromium/WebKit 320/390/430
profiles (34.8 seconds, origin `http://192.168.1.155:15431`). Model capability gates,
preview removal, reference-only requests and in-browser answers were asserted.
These are emulated profiles; the complete real-runtime UI and physical-device
gates remain open. Uploads to page forms are a separate unfinished workflow.

### Integration with current Assistant changes

Merged `origin/main` at `c39c8fb`, preserving durable queues, decisions, catch-up,
evidence, and the shared attachment menu in the reused browser conversation.
Browser context/results drawers now overlay the browser workspace, and attaching
from them keeps the current view. Composer anchoring observes the active surface.
The incoming main-Assistant regression exposed a browser flex rule overriding
`hidden`; the browser layout now explicitly honors it.

After resolution, the full backend suite passed **773 tests, five skipped**
(`PYTHONPATH=src poetry run pytest -q tests/v3`, 186.73 seconds), the focused
chat/harness/queue/catch-up/decision set passed 118, Python type checking passed
94 source files, and lint/format/diagnostic audits passed. The headed real-Core
script with live Codex passed again, including structured operator image input.
A corruption regression verifies that the exact preview bytes match the stored
size and SHA256 immediately before either runtime receives them.

Production build index SHA256:
`faea9d2ed10b0de50e7070710f4890e8794c6c70d677ea4f9d645f71df233b63`.
The combined browser/attachment/main-Assistant production LAN matrix is tracked
separately below; it uses API fixtures and emulated devices. Physical USB discovery
found no phone and neither adb nor idevice_id is currently installed. A device
and browser/LAN-access question is pending with the operator.

The combined production/LAN fixture regression passed all **32 checks** across the
eight desktop/mobile profiles in 1.3 minutes on the build above.

### Page file upload contract

Journey: choose a file from the current device beside the browser, review its name
and size, select the target file input, then approve the upload inline. The assistant
can discover the same attached-file catalog and propose the same operation. Core
persists a browser-session-scoped artifact reference, never a host filesystem path
in a tool argument. The default bound is eight attached files of at most 4 MiB each.
Removing a file revokes pending actions; approval checks current membership, bytes,
tab and page revision again. Uploads cannot run through the direct operations API
without a decided action. Preview removal, scope isolation, stale/duplicate/revoked
actions and real page file-input observation are required tests. Device file pickers
select device files; arbitrary host paths remain unavailable.

The upload implementation passed 83 focused browser/browserd/chat/harness tests,
seven browser component tests, and all eight production-bundle LAN upload profiles
(24.5 seconds). Build index SHA256:
`a8dc3f8b3a4fac816a9158c37d396a11f45424528209c9fd2dc34ddc6c006742`.
The profiles covered desktop 1440/1024 and emulated mobile Chromium/WebKit at
320/390/430, on `http://192.168.1.155:15431`. File selection, reference-only proposal,
inline approval, visible completion, removal and no horizontal overflow passed.

The real headed Core/browserd script observed an empty file input before approval,
then the expected file name and bytes after approval. Duplicate approval and
revoked-file actions were rejected. The live Codex run using `~/.codex-2` discovered
an attached file, proposed `browser.companion` upload, waited for inline approval,
and uploaded the expected content. This ran alongside screenshot, image input,
follow-up, click, credential masking, and concurrent-viewer checks.

This upload path supplies a bounded Core-owned byte buffer. Browserd's existing
arbitrary-host-file upload path still requires `upload_root` and its enumerated
regular-file checks. Headed runtime, manifest hash and policy-proxy qualification
requirements remain unchanged. File removal detaches/revokes the upload grant;
normal project artifact retention still applies to stored bytes.

All CI jobs passed on prior commit `3881b68` (Python 3.11/3.12/3.13, frontend,
database migrations, desktop-macOS and security). All CI jobs subsequently passed on upload commit `33eca54` (run
`34132749347`). Remaining gates include the complete production UI against real managed
Chromium/Codex, lifecycle coverage, packaged desktop, real LAN origin and physical
phone interaction.

### Production UI with the real runtime

The optional `--ui-host <host-LAN-address>` mode of the committed Core smoke runner
serves `ui/dist` through the isolated Core and invokes
`scripts/smoke_test_browser_companion_ui.py`. Its entry point is the browser
workspace with the durable conversation ID. It navigates, attaches page context,
sends a visible question and follow-up, approves a requested click in the UI,
observes the changed page, and reloads the same transcript and browser session.
No API routes are mocked. Test authentication is randomized and the temporary
Core enables HTTP device pairing explicitly; this does not change the installed
service. UI screenshots and a Playwright trace are retained beside the staged
runtime. This first UI journey targets headed desktop Chromium at 1440x900; it
does not replace the remaining mobile, packaged-application or physical gates.

### Manual input and production UI review (2026-09-07)

The streamed surface now sends single-touch start/move/end/cancel events through
Core to Chromium, scales pointer and wheel coordinates to the host viewport, and
sends complete key down/up pairs with modifiers and virtual key codes. A Page
keyboard disclosure provides a real editable field for composed/mobile text and
explicit Tab/Shift-Tab/Enter controls without trapping focus in the streamed image.
Rectangle selection has a visible drag preview. Browser Assistant search retains
the browser workspace while selecting a conversation.

The initial real UI run passed its functional journey at
`http://192.168.1.155:60609`, headed Chromium 1440x900, with UI index SHA256
`676f58d5ade09145b216e02ba31a2e4fb87734979013c5ac434c25278a8583e2`.
Screenshot inspection then caught a composer below the workspace and a stale
selection-handoff warning after sending. The browser is now included in the
bounded workspace layout. Clearing/sending context cancels its transient handoff,
removes its URL marker, and rejects late handoff creation after the context was
cleared. New tests require the composer and Send control to remain fully in view
and the sent handoff marker to be absent after reload. The strengthened real UI
rerun passed at `http://192.168.1.155:49929`, headed Chromium 1440x900,
with UI index SHA256
`5a4fa7a61ebe601249639b9125dad31ccfec92122f05f6252b59d68c99f605a3`.
Both the composer and Send control stayed fully in view after reload, and the
stale handoff marker was absent. Screenshot review confirmed the visible page,
conversation catch-up, and composer. Eight strengthened production/LAN viewport
checks passed (23.7 seconds) after the 32 workflow/profile checks. Earlier mobile
failures exposed a composer percentage-height cap; earlier reload failures exposed
a stale stream callback restoring old URL parameters. Both have regression checks.

Validation so far: 21 component/context tests, 21 focused browser/backend tests,
32 production/LAN fixture checks across eight desktop/mobile profiles, production
build, Python type checking and diagnostic audits passed. The real Core stream
also observed touch activation, Tab focus, text insertion, Ctrl-A and Backspace
through the authenticated relay. This is host-runtime observation, not a physical
phone or software-keyboard acceptance claim.

Host-readiness gap identified before the startup milestone below: the packaged Core bootstrap supplies
the verified Playwright runtime, but the browser registry currently discovers
browserd only from `NEBULA_BROWSERD_URL` and `NEBULA_BROWSERD_TOKEN`. The UI's
Prepare/retry control retries opening the session; it does not yet provision the
host browserd/policy-proxy lifecycle. Complete that product path while retaining
headed runtime and proxy qualification, then test the isolated packaged desktop.
Also retain real mobile UI, physical phone, all context-selection modes, current
tab metadata updates, process failure/restart, and remaining conversation lifecycle
gates as outstanding. No deployment or installed-service changes have occurred.

Evidence artifacts for this run (isolated test runtime, not the installation):
`/tmp/nebula-companion-validation/real-browser-ui-desktop.png` and
`/tmp/nebula-companion-validation/real-browser-ui-trace.zip`.

```sh
PYTHONPATH=src xvfb-run -a poetry run python scripts/smoke_test_browser_companion_core.py --runtime-root /tmp/nebula-companion-validation/playwright-browsers --harness-source-db /home/agent/.local/share/nebula/v3/nebula.db --codex-home /home/agent/.codex-2 --ui-host 192.168.1.155
npm --prefix ui run test -- --run src/components/BrowserPageSurface.test.tsx src/components/ManagedAssistantBrowser.test.tsx src/state/WorkbenchDraftContext.navigation.test.tsx src/state/WorkbenchDraftContext.test.ts
PYTHONPATH=src poetry run pytest -q tests/v3/test_browser_companion.py tests/v3/test_browserd.py
```

The desktop launcher currently clears its environment and does not pass DISPLAY,
WAYLAND_DISPLAY, or browserd configuration to Core. Host startup work must address
that deliberately; a manually injected test browserd is not packaged acceptance.

### Host startup contract

Core now lazily starts its private browserd endpoint when an operator opens the
managed browser and no external adapter is configured. It will use the packaged,
manifest-verified full Chromium, preserve headed qualification, and own shutdown.
A private authenticated forwarding proxy per browser identity will recheck the
current project scope and revocation state. HTTP requests and HTTPS tunnels remain
bounded to the granted destination; no interception, capture, replay, or arbitrary
proxy tool is introduced. Provider/harness companion instances must share the same
Core-owned runtime, while native browser state remains separate.

Required observations: clean host readiness, concurrent opens yielding one runtime,
missing/invalid bundle recovery in place, scope denial including subrequests,
identity revocation during a connection, restart with saved conversation and lost
live tabs stated honestly, private endpoint/token isolation, and Core shutdown
without orphan browser processes. Validate the forwarding boundary against local
controlled endpoints and exercise the production UI using automatic startup.


### Automatic host validation (2026-09-07)

The first host-startup CI run exposed an invalid shutdown diagnostic category
when diagnostics were enabled. Managed browser cleanup now uses the registered
`runtime` category, with an explicit lifecycle regression assertion. The focused
lifecycle, diagnostics API, and credential API checks passed (10 tests); the full
backend suite and CI are rerun for this correction.

Core-owned startup passed through the full production HTTP/WebSocket and paired
LAN UI journey at `http://192.168.1.155:53161`, headed Chromium 1440x900, UI index
SHA256 `5a4fa7a61ebe601249639b9125dad31ccfec92122f05f6252b59d68c99f605a3`.
The live Codex harness using `~/.codex-2` completed screenshot, image attachment,
approved click, approved upload, visible question/follow-up and same-conversation
reload checks with no external browserd configuration. The existing verified
Chromium executable digest remains
`2d18db9d8608b052b6a552ee00ec1e830f93692e928b65ecc67d693bd33fe801`.

The separate host smoke runner observed concurrent opens sharing one browser,
authentication on the private endpoint, the broker sharing the Core-owned adapter,
a real browser subrequest denied before reaching a reachable controlled origin,
Chromium exit with saved conversation and new blank tabs, stale-action rejection,
paused assistant control and revoked pending approvals after restart, and Core
shutdown releasing its listeners. This runner uses real Chromium and proxy
traffic through Core's ASGI interface; it is separate from the production HTTP/UI
runner. The test originally assumed a proxy 403 would reject fetch's Promise;
the corrected proof checks the proxy denial and absence of origin traffic.

```sh
PYTHONPATH=src xvfb-run -a poetry run python scripts/smoke_test_browser_host.py --runtime-root /tmp/nebula-companion-validation/playwright-browsers
PYTHONPATH=src xvfb-run -a poetry run python scripts/smoke_test_browser_companion_core.py --runtime-root /tmp/nebula-companion-validation/playwright-browsers --harness-source-db /home/agent/.local/share/nebula/v3/nebula.db --codex-home /home/agent/.codex-2 --ui-host 192.168.1.155 --automatic-host
PYTHONPATH=src poetry run pytest -q tests/v3/test_browser_host.py tests/v3/test_browser_host_proxy.py tests/v3/test_browser_companion.py tests/v3/test_browserd.py tests/v3/test_browser_engine.py
```

The focused backend run passed 32 tests. Type checking, Ruff, Rust formatting and
both diagnostic audits passed. All CI jobs passed on prior commit `30e0d0c`
(run `34135613150`); this host-startup milestone needs its own CI run. The desktop
launcher now forwards display-session variables to Core. Packaged desktop runtime
acceptance is still outstanding; this shell has no physical desktop display and
headed automation has used Xvfb. Physical phone, real mobile UI profiles and the
remaining lifecycle/context-selection matrix also remain required. The installed
application and services have not been changed.

### Packaged and selection validation follow-up (2026-09-07)

Commit `8e057d8` fixes shutdown diagnostics; its full backend run passed 780 tests
with five skips, and all jobs in CI run `34139184835` passed. The real LAN journey
at `http://192.168.1.155:45209` exercised element picking, selected text, rectangle
preview/discard, page attachment, a live Codex answer, approved click, and reload
at 1440x900. The later compact 1024x700 run also opened the same conversation in
main Assistant and returned to the browser. Mobile real-Core validation remains
in progress; a missing mobile navigation locator was corrected, and a 320px
selection failure is retained for investigation.

The extracted production DEB built from `b872f45` starts its bundled Core and
managed Chromium under native WebKitGTK WebDriver. The visible scoped-page
navigation and context-attachment steps passed. Its live test found that attaching
a browser to an existing Codex thread retained an obsolete tool inventory. The
fix refreshes the gateway connection between turns while preserving external
thread identity, carries browser availability in the Core-generated turn state,
and rechecks the conversation binding on every gateway browser call. Live proof
of that correction and a rebuilt package remain required.

The subsequent real-Core run with both catalog refresh and per-turn capability
state passed the late-attachment MCP screenshot, protected-value, image, upload,
and approval checks before reaching mobile UI selection. The mobile selector
test still failed before a preview appeared, so it is not a mobile pass. The
gateway regression suite passed 67 tests; the two browser components passed 12
tests. The native-thread identity remains unchanged in the connection-refresh
regression. The packaged binary must be rebuilt before repeating its live test.

Live tab metadata now refreshes independently of historical captures and preserves
an address being edited. Component coverage checks that metadata updates do not
reconnect the stream. The production fixture matrix passed 32 checks across the
eight desktop/mobile profiles after that change; this remains fixture evidence.
The new packaged runner uses a temporary XDG profile, an extracted DEB, staged
tauri-driver/WebKitWebDriver, and a read-only copy of the approved Codex profile.
It does not install the package or change the running installation.


### Mobile sheet and cancellation follow-up (2026-09-07)

The production DEB from `b1ccec9` passed the native WebKitGTK driver journey with
its bundled Core: scoped navigation, page attachment, a live Codex answer and an
inline approved click. The package was extracted under `/tmp`; no installation or
service was changed. The current source includes a later main merge and mobile
changes, so final package rebuilding and acceptance remain required.

The mobile Assistant now uses an opaque expandable sheet. Its required actions
remain visible within the sheet; collapsing returns focus to the Assistant toggle.
It follows the visual viewport and reserves space for mobile navigation and safe
areas. Chromium device emulation at 320/390/430 px previously completed the live
journey. The latest WebKit 26.5 iPhone 13 emulation at 320x700 passed element/text/
region selection, attachment, streamed answer, approved click, reload and the same
conversation in main Assistant at `http://192.168.1.155:50089`. UI index SHA256:
`20a7dc3f11090bd32df923b7914e33e73b1438cf6926a66e1fd693eb72e15125`.
Physical touch, rotation and software-keyboard acceptance remain outstanding.

Manual browser navigation now immediately reports operator takeover and an older
control poll cannot overwrite an explicit control change. The live cancellation
journey exposed an independent MCP task retaining its pending approval after Stop.
Actions now retain their originating chat-turn ID; the broker revokes them when
the durable turn ends and dispatch checks that status again before page mutation.
Cancellation and late approval coverage passed within 18 companion backend tests.
The live lifecycle retry is still in progress; this is not yet lifecycle acceptance.

The attachment cleanup regression now creates a handoff before the browser chat
sends and verifies the resulting URL no longer carries it. Seven Chromium profiles
passed in the production fixture matrix. Three concurrent WebKit cases exceeded
the 30-second whole-test limit; an isolated headed WebKit 320px rerun passed in
27.3 seconds. The two-answer fixture has a 60-second whole-test limit for the
remaining engine runs. Sixteen component/navigation tests passed. The complete
required matrix, final source build, physical phone and final CI remain open.


The subsequent headed production WebKit fixture matrix passed all three widths
(320/390/430) with the 60-second whole-test limit; individual runs took
27.1/29.9/44.2 seconds. Live cancellation now removes the approval and leaves the
page unchanged. Linked retry then exposed a missing browser binding on replacement
turns. Binding resolution now runs at the shared detached-turn start entry point,
using the current Core association rather than the original turn's metadata.
Regression coverage checks attached and removed-browser retries without changing
the original cancelled execution. The focused harness/companion suite passed 67
tests; Ruff and type checking of four affected backend modules passed. The live
retry/takeover/recovery continuation remains pending.


### Visible lifecycle result (2026-09-07)

The production LAN lifecycle run passed at `http://192.168.1.155:38515`, headed
Chromium 149.0.7827.55 at 1440x900, using live Codex and automatic host startup:
stop revoked its pending action without a page change; linked retry required a new
approval; opening a tab took over a queued action without changing the old target;
a second viewer detached and the first reconnected; Chromium restart retained the
conversation while reporting lost tabs and paused control. The retained result is
`/tmp/nebula-companion-validation/lifecycle/result.json`, with screenshot and trace
beside it. This is Xvfb automation, not physical-device evidence.

Cancellation also exposed a diagnostic ContextVar token being reset from a
different task when ASGI closed an event generator. A deterministic regression
failed with that exact exception before the fix. Correlation now surrounds each
iterator advance and close, ending before yielding to the consumer. The stream
and chat API checks passed 11 tests. Final packaging must include this follow-up.


### Landscape and reload checks (2026-09-07)

Permanent `browser-chromium-landscape` and `browser-webkit-landscape` Playwright
projects cover 844x390 device emulation. The initial desktop-column layout clipped
the composer at that height. The Assistant sheet now also applies to short coarse-
pointer viewports. The production answer/follow-up fixture passed in Chromium
(2.2 seconds) and WebKit (41.3 seconds), asserting the complete composer and Send
button are inside the viewport. Page-control tests collapse the sheet through its
visible button before interacting with the underlying page. Remaining landscape
capability and live-Core runs are in progress.

Live WebKit 390 px passed the complete main journey. The 430 px run reached the
page change and then exceeded the former five-second reload assertion while Core
was still displaying Loading workspace. Startup/reload readiness now has a bounded
30-second wait; the remaining live portrait run is in progress. This wait does not
change page mutation, approval or result assertions.


### Final automated validation follow-up (2026-09-07)

The full backend suite passed **789 tests, five skipped** in 174.20 seconds. Ruff,
formatting of 177 files, Python/Rust diagnostic audit, frontend diagnostic audit and
four-module type checking passed. The normal stream-end handler now has the audit's
required expected-condition annotation; this corrects the CI failure on `ee0217b`.

The host regression reproduced loss of the restart notice when background tab
polling ran before reconnect. Core now persists that indication, pauses control and
revokes pending actions when it observes replacement live tabs. Reconnect retains
the notice until successful explicit navigation. Both the real headed host runner
and its durable-store unit regression passed.

Production landscape device emulation now completes the full live journey in both
Chromium 149.0.7827.55 and WebKit 26.5 at 844x390, paired to real Core at
`http://192.168.1.155:42079`, UI SHA256
`cd55e37324b39f92b4dcb1b897d8feee3b2efbdb2db6bfa8cf32a812ae672c6e`.
The run includes touch scrolling through the authenticated Core stream and a live
Codex check that reads a controlled page heading while ignoring page instructions
to navigate elsewhere. Only completed read operations were accepted for that turn.
The degraded-Core landscape fixture reproduced inaccessible Go controls before the
scrolling fix and passed afterward in Chromium (2.4s) and WebKit (45.8s). The six
landscape image-capability/upload cases also passed after allowing a 60-second
whole-workflow deadline for the slow WebKit cases.

The final portrait rerun passed WebKit 430x932 and Chromium 320x700, including
selection, answer, inline approval, reload and shared conversation, at
`http://192.168.1.155:43495`, UI SHA256
`6d65bbed40d8adeb23db35ea339d277ef0222df59a407ad85da9d4cf5de234a5`.
Earlier 320/390 WebKit and 390/430 Chromium passes remain recorded separately.

The native packaged preflight now explicitly sets and verifies its webview
viewport. At 1024x700 it passed startup, managed browser navigation and context
attachment using the extracted `3f346f9` package. The latest recovery/layout changes
still require the final package rebuild and complete live 1024/1440 journeys.
Physical phone touch, rotation, background/resume and software-keyboard evidence
remain required and have not been substituted with emulation.

### Packaged compact-window regression (2026-09-07)

The `ca39ce1` DEB passed the complete 1440x900 native WebKitGTK journey with its
bundled Core and live Codex from `~/.codex-2`: navigation, attached page context,
answer, inline approval, and a fresh capture of the resulting Saved button.
DEB SHA256: `272fa45b94c39d8a987ab99deed4c21d1afae3543cd169f48ad4b9d08d98a149`.
Core SHA256: `22f3ecc7102ad23f68d8083232d454e97ab0fd430af311680f620e29bae42637`.
Evidence: `/tmp/nebula-companion-validation/packaged-final-1440.log` and
`packaged-final-1440/packaged-desktop-boot.png` under the same validation root.

At 1024x700 the native driver reported `element not interactable` for Send.
The retained `packaged-final-1024-diagnostic/packaged-click-failure.png` shows
the attachment and wrapped footer extending beyond the panel's clipped edge.
The browser composer now shrinks within its available height and scrolls its
contents. It no longer has an unlimited maximum height inside a clipped panel.
The native runner retains the exact driver error and screenshot on failed clicks.
Native acceptance of the follow-up remains pending the rebuilt package.
The production LAN answer/follow-up fixtures passed all six healthy/degraded
cases in compact Chromium and landscape Chromium/WebKit after the change
(`composer-bound-fixtures-headed.log`). The first launch used headless mode
against the headed-only runtime root; four Chromium cases did not launch.
The successful run explicitly used `--headed`; no product assertion was relaxed.

The rebuilt `151d566` compact package bounded the composer, but WebKitGTK's
native click did not scroll its partially visible Send control. Geometry recorded
a 224 px scroll viewport, 274 px content, `overflow-y: auto`, and scrollTop zero.
The native runner now issues a W3C wheel action over that actual scroll container
before clicking an offscreen composer control. It does not force a click or
change the DOM layout. This closed the full 1024x700 journey, including the live
answer and approved page change; the wrapper exited zero. Evidence:
`/tmp/nebula-companion-validation/packaged-151d566-1024-wheel.log`, screenshot in
the matching directory. The failed click geometry remains in
`packaged-151d566-1024-geometry/packaged-click-geometry.json`.

Runtime build identity for those checks is `151d566`; subsequent evidence/runner-only edits
do not change its bundled UI or Core. DEB SHA256:
`a70b24cdec1827171fb29bae198e732b217dc5f230e61551bb922047b2374c0c`.
Core SHA256: `6e17a61448c07f17b0236f9fb2e4564015cdf4bd1d0ee30365eb9c25c8bc7e12`.
UI index SHA256: `fc4830067578e0a49441bf93ed745853fbfcfe35e0bb2e08b0a1feec95bed6d9`.
The full manifest is `package-151d566-manifest.json` in the validation root.

The sequential 1440x900 acceptance run also passed and exited zero:
`packaged-151d566-1440-sequential.log`, with screenshot in the matching directory.
An earlier concurrent 1440 run printed its successful result but its wrapper
exited 143; it is retained as superseded evidence, not a clean acceptance run.
Both accepted runs use the same extracted package and isolated XDG profiles,
native WebKitGTK 2.52.6, bundled Core and the qualified Chromium runtime. The
native input runner is `scripts/smoke_test_browser_packaged.py` with `--width`
and `--height`, `--package-root desktop-package-151d566`, the staged tauri/native
drivers, the installed database supplied read-only through `--harness-source-db`,
and `--codex-home /home/agent/.codex-2`; complete arguments remain in the runner
and session evidence. No package was installed.

Physical device status: the governed Mac inventory reports a connected iPhone
16 Plus. A bounded Safari WebDriver probe could not create a session because
Web Inspector was disabled. The operator was asked to enable Web Inspector and
Remote Automation and leave it unlocked. No device setting was bypassed, and no
Mac runtime lease is held while waiting. Touch, software keyboard, actual rotation
and background/resume remain unverified physical gates. The PR remains draft.

### Current-main integration and packaged validation (2026-09-07)

Main's harness workspace update (`502a247`) merged without conflicts as
`fce1bd3`. The shared gateway review confirmed that browser-binding refresh and
dispatch checks remain in place; the new portable-name handling is conditional
on the Grok adapter. No live Grok runtime was used. The focused harness,
adapter and browser suite passed 103 tests in 16.05 seconds; all seven CI jobs
passed on `fce1bd3` (run `34150415312`).

The rebuilt, extracted production DEB passed sequential native WebKitGTK journeys
at 1024x700 and 1440x900, both with exit zero. Each used live Codex, late browser
attachment to the seeded conversation, a contextual answer, inline approval and
a fresh capture confirming Saved. Evidence is retained in
`/tmp/nebula-companion-validation/packaged-fce1bd3-1024.log` and
`packaged-fce1bd3-1440.log`, with screenshots in their matching directories.
Manifest: `package-fce1bd3-manifest.json` in the same validation root.
DEB SHA256: `b07d84ccf2c1d060f2a19ea153ebf3e7b3fcb8f2a4d4656cf9eff9807c19cf4a`.
Core SHA256: `2ec38826b724afa3ef2dd20ecdb312742387e6ccba780ba2101da2ec628100c4`.
The UI index remains `fc4830067578e0a49441bf93ed745853fbfcfe35e0bb2e08b0a1feec95bed6d9`.

A fresh bounded physical-device probe again returned Web Inspector disabled on
the connected iPhone 16 Plus. The probe closed its driver; the global Mac lease
registry was clear afterward. Physical acceptance still requires the operator's
device setup, followed by touch, software keyboard, rotation, zoom and
background/resume checks. Earlier emulation results do not supply that evidence.

The final audit added a permanent Axe scan of the integrated page and Assistant
panel to the conversation fixture. The initial six-profile scan passed; the
strengthened fixture then passed all six profiles with roughly 6 KB page context,
a multi-paragraph follow-up and a long unbroken URL. It asserts zero Axe violations,
reachable composer/Send and no horizontal document overflow. Profiles: Chromium
1440x900 and 1024x700, Chromium Android 320x700, WebKit iPhone 430x932, and both
engines at 844x390 landscape. These are production LAN fixtures at
`http://192.168.1.155:15431`, not live provider or physical-device evidence.
Results: `integrated-browser-accessibility.log` and
`integrated-browser-long-accessibility.log` in the validation root, six passed
each. The expanded long-content plus Axe workflow has a 90-second whole-test
deadline; its WebKit cases completed in 53.4 and 53.0 seconds. No individual
interaction or geometry assertion was weakened. This follow-up changes only
tests and evidence; the accepted runtime build remains `fce1bd3`.
