# Application model as assistant working knowledge

Baseline: `158548e`. Requested change: retire the application-model GUI, not the
stored knowledge, evidence, schema or assistant tools. No deployment is requested
in this turn. Existing approval, capability and privacy gates remain unchanged.

| Journey | Observable invariant | Authority | Verification |
| --- | --- | --- | --- |
| Workbench/project navigation | No Model tab, inspector or mobile More entry | Route/tab definitions | App tests; production Chromium/WebKit |
| Old model bookmark | Same project opens Assistant; graph-only query state is discarded | URL and Core project selection | Real-Core browser, refresh/back |
| Use working knowledge | Existing model tools retain project-scoped evidence-backed knowledge; no GUI prerequisite | Core graph and existing runtime capabilities | Core tools/API tests and prompt contract |
| Reload/reconnect | Navigation does not reset knowledge or captures | Core storage; URL | Real-Core paired browser |
| Empty/unavailable project | Existing loading/offline/unavailable recovery remains usable | Workspace state | App tests |

No knowledge migration or data deletion. Editing/resetting the graph through a GUI
is intentionally retired; backend transactions/reset contracts remain unchanged.
Approval, streaming and workspace mutations are unchanged, covered by existing
regressions rather than new runtime execution. Do not enable tools or cloud data
transfer for a text-only session merely to expose knowledge.

Plan: red navigation regression; remove dedicated routes/desktop/mobile entries
and retired rendering code; replace graph-UI acceptance with knowledge-retention
and legacy-link acceptance. Preserve project isolation, reload and history. Run
focused App/Core tests and the production real-Core journey on the eight permanent
desktop/mobile Chromium/WebKit profiles. Physical/native acceptance is separate;
do not claim a newly installed build without packaging and deployment.

## Regression notes

- Red: the existing App test still found the Model tab; the viewer test found
  `Shared Chromium browser` and manual controls. The new Core guard regression
  returned 404 rather than rejecting a manual operation at the viewer boundary.
- Retired page/editor/outline/graph/reset components and their dedicated GUI
  tests are deleted, recoverable from Git. No stored knowledge or captures are
  deleted. Backend schema, storage, model tools and reset contracts remain.
- The first real-browser setup lacked a virtual display for host Chromium and
  incorrectly expected an open composer instead of the normal Start new chat
  entry point. Those fixture assumptions were corrected, not product behavior.
  Project-scoped bookmarks preserve identity; a global legacy URL without a
  project ID still uses the existing current/default-project rules.
- Read-only conversion exposed a real keyboard-scroll accessibility defect;
  the viewport (not its image) now receives local keyboard focus, without an
  input-forwarding callback. Axe verifies this boundary.
- Screenshot review caught the old six-column toolbar squeezing the read-only
  address to 44px. The toolbar now has three columns; the new regression checks
  usable address width. Mobile header-height checks also caught excess hint
  space; its restrained typography restores the existing geometry limit.
- Interrupted pre-final matrices are retained, not counted as passes. One
  WebKit page fixture timed out during setup. The isolated WebKit check passed
  in 12.2s with Xvfb confined to the host Chromium server; final browser batches
  run serially instead of overlapping WebKit processes. No test-case retries
  or weakened geometry/accessibility assertions were added.
- The compact degraded-Core conversation exposed non-shrinking recovery notices
  pushing the composer beyond its panel. Browser Assistant notices now shrink
  within a panel-relative scroll area; the original full Send-button visibility
  assertion passes unchanged. Diagnostic geometry is retained in
  `/tmp/nebula-knowledge-composer1.log`; isolated repair verification passed in
  `/tmp/nebula-knowledge-composer-fixed.log`.

## Evidence

- Core: **29 passed**, `/tmp/nebula-knowledge-core29.log`; collection in the
  adjacent `-selection.log`. Includes project-scoped graph/API/tool availability,
  mechanism admission, viewer HTTP rejection, shared instructions, approval
  continuation/cancellation and no replay. Only local/inert fixtures.
- Components: **54 passed**, `/tmp/nebula-knowledge-ui-final.log`; the subsequent
  composer correction is CSS-only and verified in production browser tests.
  Exact files are in the diff-bound test
  selection. Initial red and intermediate failures remain in `/tmp/nebula-knowledge-*`.
- `npm --prefix ui run build`: production build passes; existing large-chunk
  warning remains; final composer build log is
  `/tmp/nebula-knowledge-build-composer.log`. This is a dirty working-tree verification build, not an
  installed/released artifact.
- Real-Core production LAN matrix: **16 passed**;
  `/tmp/nebula-knowledge-real16-final` and adjacent log. Eight permanent desktop
  Chromium 1440/1024 and emulated Chromium/WebKit 320/390/430 profiles, 16 cases.
  Source Core serves production assets at `http://192.168.1.155:19425` with real
  pairing, durable graph checks and a fixed local fixture page. HTTP mutations
  return 403; a stale-client WebSocket input closes with 4403 and page text is
  unchanged. Viewer reconnect and explicit pause/resume remain available.
- Context/conversation/upload-approval production UI matrix: **24 passed**, no
  retries or skips, `/tmp/nebula-knowledge-interface24-accepted.log` (4.3m).
  Traces and screenshots are in the adjacent directory. Selected mocked-API cases
  cover eight permanent profiles at `http://192.168.1.155:1445`, desktop Chromium
  1440/1024 and emulated Chromium/WebKit 320/390/430. Keyboard focus restoration,
  long context/transcript, degraded-Core recovery, upload approval, accessibility,
  touch targets and full composer visibility pass. Kept separate from actual Core
  persistence evidence. Desktop/mobile screenshots were reviewed, including the
  compact degraded-Core toolbar after the final correction.
- Native acceptance scripts now assert no Model GUI and knowledge retention on
  relaunch, and observe rather than manually navigate the browser. **Not run
  against a new package** in this turn. The installed desktop and live LAN remain
  the earlier `9f08047` release. Physical devices are unverified. No commit, push,
  migration, publication or deployment is part of this change request.

Added request: the managed/shared browser becomes **Assistant browser**. The
operator watches a read-only stream, attaches context/approved resources, gives
directions in chat, and retains pause/resume and approval/rejection. No implicit
resume, approval bypass, scope change or new autonomous capabilities. The separate
embedded device browser is outside this managed-browser change. HTTP operator
navigation/actions and WebSocket input must not mutate the Assistant's page;
reconnect and tab viewing must not change its active tab. Verify input rejection,
unchanged page state, frame reconnection and visible recovery, using only a local
inert fixture site. Retire tests for intentionally removed manual page controls;
retain backend tool/approval tests. Browser surface and management components are
additional selected unit targets.
