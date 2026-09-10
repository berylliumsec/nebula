# Whole-product stabilization acceptance ledger

Baseline: `fdaca50` on `codex/hypothesis-graph`. Release is gated: do not deploy
partial fixes or mix web assets, Core and native packages. Existing live projects,
sessions, approvals and data are not test fixtures. Use temporary local fixtures
and disposable projects only; no scans or vulnerability reproduction.

## Contract and walkthrough

Core owns saved content, approval decisions and execution progress. Harnesses own
acknowledgement of native requests, not the recorded operator decision. URL owns
selected project/conversation identity. React owns transient presentation and
unsent input; browser storage owns documented device-local recovery. Every area
must cover loading/empty/normal/long/unsupported/error/retry and refresh where
applicable. Not-applicable lifecycle steps require a reason in the evidence.

| Area / entry | Observable invariant and lifecycle | State authorities | Regression layers | Baseline / candidate evidence |
| --- | --- | --- | --- | --- |
| Launch/navigation | Connect/pair, select/search, deep-link, back/forward, reload/relaunch preserve the correct identity | Core/device auth, URL | component, real Core LAN, native | Partial: pairing/reload, project isolation, resource Back/Forward and native relaunch passed; final artifact walkthrough pending |
| Assistant | Select runtime, send/stream, approve/reject/stop, complete, queue/switch/reconnect/retry without duplicate work or contradictory status | approval, turn, harness request, canonical projection | service failure injection, real Core, UI, real harness, native | Partial: A1–A8 regressions, 96-case process failure matrix, native approve/reject/stop and fresh configured runtime smoke passed; final artifact gates remain |
| Browser | Navigate/input, control handoff, capture, reload/reconnect, explain unsupported devices | Core browser session and owning device | local fixture, UI, LAN/native | Partial: headed local input/reconnect and packaged managed capture passed; complete device/handoff matrix pending |
| Model | Inspect mechanisms/evidence, select/filter/expand/resize, clear only model/captures | Core model/evidence, local viewport | component, real Core, LAN/native | Partial: 16 real-Core production browser cases passed; native graph and final candidate pending |
| Workspace | Link/browse/upload/edit/save/recover, terminal, switch projects; one linked folder everywhere | Core workspace, filesystem, device drafts | service, real Core, native | Partial: real linked workspace/terminal and mobile Code conflict/draft journeys passed; final/native coverage pending |
| Project/outputs | Create/update/archive/restore and discover/reuse notes, evidence, findings/reports/Library | Core durable entities | real Core, UI | Partial: project lifecycle, note/report/PDF and Library lifecycle passed; independent findings/evidence and upload failure coverage pending |
| Settings/recovery | Save/rediscover runtimes and policies; retry failures; show accurate build/capability identity | Core catalog, frozen session policy, build manifest | component, real Core, native | Partial: component identity and configured runtime discovery passed; real setup/retry and native identity screen pending |

## Defects and work batches

1. Establish reproducible baseline from production UI entry points; record traces.
2. Repair decision/delivery/progress contract, idempotency and restart recovery.
3. Consume canonical revisioned session state across all status surfaces.
4. Repair shared layout/mutation contracts, then remaining walkthrough defects.
5. Build-identical production/native candidate; rerun walkthrough, real harness
   smoke checks and native acceptance. No release with missing required evidence.

Each discovered defect must record reproduction, affected build, expected result,
fix, regression and verification here before being called resolved.

### Recorded findings and batch evidence

- A1, baseline `fdaca50`: the decision API returned early for `run_command`,
  saving `approved` without delivering to the harness waiter. Reproduced by
  `test_decision_delivers_to_exact_waiter_and_retry_does_not_redeliver[approve-run_command]`:
  HTTP 200 with an unresolved future. Decision and pending delivery now share one
  durable approval row. Duplicate same decisions return that row; conflicting
  decisions return 409 with current state. Missing waiters interrupt without replay.
- A2: independent UI interpretations of waiting status. Added a read-only,
  revisioned session projection and hooked pending counts, composer rail and
  progress to it. Decision-delivery is explicitly Core-waiter delivery, not a
  claim of vendor acknowledgement or completed command execution.
- Baseline production build passed. Two desktop primary-view/dialog walkthroughs
  passed in 30.4s at LAN preview `http://192.168.1.155:1442`, using API fixtures.
  Evidence: `/tmp/nebula-stabilization-baseline`.
- Backend: 24 selected tests passed in 11.71s across approval continuation,
  session projection and catch-up. Includes missing/cancelled waiter, restart,
  duplicate/conflicting decisions and preserving a second request. An old catch-up
  fixture was corrected to create an actual pending approval rather than infer
  one from a waiting turn alone.
- Frontend: 7 selected tests passed in 1.35s (session switching/stale revisions,
  refresh failure/retry, authoritative pending counts, read cursor interactions).
- Production approval UI: 24 passed in 1.7m across permanent desktop Chromium
  1440/1024 and emulated mobile Chromium/WebKit 320/390/430, plus short-window
  checks. API fixtures, not native providers. Evidence:
  `/tmp/nebula-stabilization-approval-matrix`.
- Real-Core production-browser acceptance is being added with an inert adapter:
  UI decision, actual API/store/waiter, completion and reload. It executes no
  commands. Real vendor/runtime and packaged desktop acceptance remain pending.
- Native WebDriver tools found under `/tmp/nebula-companion-validation`; installed
  desktop is `/usr/bin/nebula-ui`. Existing extracted packages are older builds
  and cannot prove acceptance of this candidate.
- N1, newly observed empty-project route: when an existing database has only a
  harness profile and no project, `LegacyProjectRedirect` renders "Opening
  project…" indefinitely despite successful project loading. Observed in the
  real-Core fixture (which originally seeded the profile before first-run
  bootstrap). Needs a dedicated empty-project regression and recovery UI; not
  resolved by correcting that approval fixture's bootstrap order.

- N2, production candidate walkthrough: expanded harness plan overflows its own
  content box by 3px at the compact desktop width. Existing
  `completed harness output keeps one continuous transcript scroll` failed at
  `scrollWidth=817`, `clientWidth=814`. Inspect plan flex sizing before changing
  the assertion. Evidence: `/tmp/nebula-stabilization-desktop-walkthrough`.
  Diagnosis: the rotating 15px chevron's transformed bounds exceeded the right
  edge by 2.89px. A dedicated 24px icon slot contains the animation.
- N3, the same plan journey at 320px: the scrollable ordered list could not be
  focused by keyboard (`scrollable-region-focusable` accessibility failure).
  The named plan list needs a keyboard focus target, without changing scroll
  ownership or weakening the accessibility assertion. Evidence:
  `/tmp/nebula-stabilization-plan-matrix`.
- Further backend failure injection: activity persistence failure cancelled the
  orphan waiter and interrupted its owner (red/green). A terminal harness can no
  longer advertise a pending action from a stale owner record (red/green).
  Latest focused batch: 26 passed in 14.60s; six compatibility tests passed in
  the prior 31-case batch (19.57s).
- Frontend: 9 passed in 1.43s, including build alignment. Empty-project recovery
  passed all 8 permanent profiles in 16.0s, using production LAN UI/API fixtures.
  Evidence: `/tmp/nebula-stabilization-empty-matrix`.
- Real-Core production LAN: create/isolate/reload project, approve/reject with
  durable pairing, duplicate decisions, archive/retry/restore all passed (4 in
  39.2s). Evidence: `/tmp/nebula-stabilization-real-core-projects`.
- Model/reset desktop: 2 passed; shared-browser input initially blocked by the
  headless host's missing display. With Xvfb, real headed managed Chromium
  click/type/reconnect passed (1 in 8.0s). Evidence:
  `/tmp/nebula-stabilization-model-baseline`,
  `/tmp/nebula-stabilization-browser-display`. No browser security setting changed.
- Plan rail: 8 passed in 35.4s after containing the rotating icon and making the
  scrollable list focusable. The long-answer test now hovers the visible last
  paragraph instead of the center of a many-screen-tall article, and explicitly
  checks that its action is inside the viewport. Evidence:
  `/tmp/nebula-stabilization-plan-matrix-3`.
- Primary view/dialog walkthrough: 16 passed in 3.0m across all eight permanent
  profiles, production LAN bundle with API fixtures. Evidence:
  `/tmp/nebula-stabilization-primary-matrix`.
- Actual Core approval matrix: 16 passed in 1.3m across all eight profiles, with
  real SQLite, API, waiter, pairing, reload and idempotency. The adapter is inert;
  these are not vendor permission tests. Evidence:
  `/tmp/nebula-stabilization-approval-real-matrix`.
- Configured runtimes: Codex and Grok each completed a fresh no-tool conversation
  and reloaded its saved answer (2 passed in 29.2s). Profiles/models were read
  from configuration and health discovery, not hardcoded. Evidence:
  `/tmp/nebula-stabilization-configured-runtimes`. Live data was read only to
  discover non-secret runtime fields; all test turns used disposable databases.
- Candidate backend selection: 38 passed in 17.51s; frontend 13 passed in 1.39s;
  selected new Python modules passed type checking. Production build passed.
  Build integrity gate includes the service worker/public assets and rejects
  mismatches, dirty builds and modified/missing/extra assets (6 selected tests).
- Final pending-approval display matrix after canonical request gating: 24 passed
  in 1.6m; evidence `/tmp/nebula-stabilization-approval-ui-final`. The final
  frontend rerun passed 13 in 2.79s. All 82 emitted web/public assets match their
  hashes; this dirty development build is deliberately not a release artifact.

## Remaining release gates

### Canonical projection hardening contract

- A4: the first projection uses a maximum timestamp as its revision. That is not
  monotonic when a clock moves backwards, a request expires without a write, or
  a retained request is deleted. Before release, add a durable, serialized
  revision for the derived snapshot, separate from execution records. The same
  state must keep its revision; a changed state must advance it after reload,
  restart and concurrent reads. Building a display snapshot must never change
  an approval, turn, policy or command. Catch-up's pending lookup must not toggle
  the authoritative connection projection by omitting runtime observations.
  Planned layers: failing clock/deletion/expiry/concurrency tests, additive
  cache-table migration/rollback, stale-revision component tests, real-Core UI.
- A5: `session_activity.live` means an active turn, not a connected transport.
  Idle must not be labelled disconnected simply because no turn is running.
  Observe the adapter's actual transport where available and report unknown
  where unavailable; keep execution, connection and decision delivery separate.
  Planned layers: deterministic RPC/idle/exit tests and configured runtime smoke.
- A4/A5 focused result: 16 passed (12.02s). The red regressions proved a
  backwards clock could decrease the revision, removal/expiry could leave it
  unchanged, and an idle connected adapter appeared disconnected. A derived
  cache now serializes semantic revisions; unchanged concurrent reads share a
  revision and reopened SQLite retains it. Catch-up uses the same pure pending
  computation without overwriting connection state. The additive migration's
  downgrade removes only the cache; retained turns survive. Codex/ACP transport
  reader/exit probes are covered; adapters without a probe report unknown.
  PostgreSQL is not configured and has not been exercised. Real-Core browser
  and new packaged-build verification of these changes remain required.
- A4/A5 integration: all 50 selected backend/compatibility cases passed in
  22.92s; the projection passed focused type checking and lint. The current
  production web build passed and all 16 real-Core approve/reject/reload cases
  passed in 1.4m across desktop and mobile Chromium/WebKit profiles. Evidence:
  `/tmp/nebula-stabilization-projection-python.log`,
  `/tmp/nebula-stabilization-projection-web-build.log`,
  `/tmp/nebula-stabilization-projection-real-matrix`. This is a dirty diagnostic
  build, not a release candidate. New native artifact verification remains open.

### Candidate native and additional lifecycle findings

- A6 contract, approval-to-continuation: the API's current `delivered` receipt
  ends at Core's waiter. Native permission consumers must declare their handoff
  target before the decision, with a pending adapter handoff persisted in the
  decision transaction. Record transport-write acknowledgement (or SDK callback
  return) separately from execution progress; these protocols do not provide an
  acknowledgement-of-acknowledgement and must not be described as proving that
  a command ran. Actual subsequent turn activity comes from the durable ledger,
  ordered after delivery, not from the callback that merely releases waiting.
  Failures/restart between waiter delivery and adapter handoff must preserve the
  known decision and known delivery, mark uncertainty, and never replay. Late
  receipts must not overwrite recorded outcomes or bind to another turn. Tests:
  red API/projection regression, deterministic Codex/ACP handoff failure,
  restart at both boundaries, ordered activity/second-request isolation, then
  real-Core and native fixture journeys. Existing policy gates remain unchanged.
- A6 implementation and bounded evidence: 63 selected backend cases passed in
  33.05s, including 11 new handoff/ordering/owner-binding cases and the real
  Codex/ACP permission consumers. A decision now carries adapter intent in its
  atomic save; receipts distinguish transport write/SDK callback from ordered
  execution activity. Restart preserves known delivery and records an unknown
  handoff without replay. Removed historical owner guessing. Evidence:
  `/tmp/nebula-stabilization-handoff-python.log`. Sixteen production LAN
  real-Core approval/rejection cases passed in 1.3m with the stronger durable
  activity assertions (`/tmp/nebula-stabilization-handoff-real-matrix`). These
  use an inert broker adapter; actual packaged adapter acceptance remains open.

- Clean candidate `b382bef99f6a5646f18b77698536f29f20178446`, built at
  `2026-09-10T16:13:20Z`: web/Core identities and 82 asset hashes verified;
  managed DEB compiled and extracted without installation. Native browser
  navigation/capture attachment passed at 1440x900 and 1024x768. Evidence:
  `/tmp/nebula-stabilization-candidate-eoGxo3`. The older native smoke selector
  depended on button styling; it now uses the existing accessible Go control.
- N4, actual extracted desktop candidate: a diagnostics availability banner above
  the workbench consumes height in addition to a 100%-height session page. The
  composer submit button extends below the main viewport, behind the shell dock;
  native WebDriver cannot click it. At 1440x900, main ends at y=800 while the
  composer ends at y=841. Evidence: `native-approval-1440-retry` in the candidate
  directory. Required contract: shell owns page height, notices consume their
  own row, transcript owns scrolling, and primary actions remain visible with
  zero/one/wrapped notices at desktop/mobile and short landscape. Add a permanent
  production regression before adjusting this shared layout.
- A3, restart with a removed approval ToolCall or HarnessTurn currently raises
  NotFoundError and can prevent Core startup. New selected failure-injection
  regression reproduces this. Required contract: mark only that saved delivery
  failed, retain the recorded decision/binding, continue startup without replay
  or fabricating an owner. Verify both missing-record cases and prior continuation
  tests before committing.
- Real-Core workspace batch: 3 passed in 18.9s (linked host-folder selection,
  LAN quick-open, Git diff and terminal handoff), evidence
  `/tmp/nebula-stabilization-workspace-real`. Chromium/WebKit emulated 390px:
  edit/save, external-change recovery, 21 draft buffers, evidence and candidate
  finding handoff passed (2 in 36.8s), evidence
  `/tmp/nebula-stabilization-code-real-mobile`.
- N4 repair: the shell now allocates a bounded, focusable notices row before the
  workbench. A second failure at 320px proved the composer's 42% height cap clipped
  its own footer. Context/activity now scroll independently of input and actions.
  Eight notice/hit-test/accessibility profiles passed in 21.2s; all 32 selected
  approval/plan-scroll regressions passed in 2.2m. Evidence:
  `/tmp/nebula-stabilization-notices-matrix-2`,
  `/tmp/nebula-stabilization-composer-contract-matrix`. Native verification of
  this repair requires a newly built package, not the earlier extracted binary.
- N5, follow-up short-window regression: at 844x390 with a diagnostics notice,
  Send extends to y=326.23 while the work area ends at y=304. The two-profile
  production check failed on desktop and stopped its WebKit peer early. Retain
  the existing N4 contract: notices must consume bounded space, required actions
  remain reachable, and transcript/input have explicit scroll ownership. Evidence:
  `/tmp/nebula-stabilization-notices-landscape`. Inspect all height consumers;
  do not remove the assertion or shrink touch targets to make it pass.
- N5 follow-up: Chromium passes the compact-shell repair, but WebKit still clips
  after rotation. The transcript has correctly shrunk to zero; message search
  consumes 36.95px and the composer retains 124px from portrait. Inspection found
  textarea autosizing depends only on draft/view, so an unchanged draft retains
  its narrow-pane height after becoming wider. Add a resize-observed autosize
  hook (with width-change/cleanup/no-loop regressions) before completing the
  short-window matrix. Evidence: `/tmp/nebula-stabilization-notices-webkit-geometry`.
- N5 resolution evidence: resize observation was necessary but insufficient.
  WebKit also measured minimum-height padding as content (63px for one line),
  while fixed search/composer rows exceeded their container. Measure natural
  content before restoring the CSS minimum, remove inline baseline spacing,
  and allow bounded input shrinkage. Short windows reduce shell spacing rather
  than touch targets. All eight profiles passed portrait and 844x390 landscape
  with short/long notices, entire composer/search bounds, hit testing, keyboard
  focus, 44px controls and axe (40.7s). Five sizing regressions and the selected
  frontend batch passed: 54 tests, 30.52s. Evidence:
  `/tmp/nebula-stabilization-composer-frontend.log`. Successful browser traces
  are retained in the subsequent `notices-retained` run; the earlier default
  reporter retains failure traces only, not successful attachment bodies.
- N5 retained rerun: 8 passed in 64.43s, zero skipped/flaky cases, with successful
  traces/screenshots in `/tmp/nebula-stabilization-notices-retained` and the
  list/JSON receipt in `/tmp/nebula-stabilization-notices-retained.log`.
  The 32-case approval/plan-scroll regression batch also passed (2.6m).
- N6 visual review of that retained WebKit landscape screenshot: bounding the
  notice row protects the composer but clips the notice's own detail/actions.
  Before repair, add a regression requiring its controls to fit without scrolling
  the row. Keep the failure summary visible, move verbose diagnostic detail to
  the shared modal with focus restoration, and retain dismissal semantics.
  State owners: diagnostic health event and device-local dismissal; no Core
  execution state. Layers: banner component lifecycle, eight production profiles,
  keyboard/detail/dismiss/focus, long content and packaged native walkthrough.
- N6 component result: six cases pass, including resolved detail staying closed
  on a later failure. The combined selected frontend batch passed 60 cases in
  23.22s (`/tmp/nebula-stabilization-notice-frontend.log`). A browser regression
  also caught a hover transform lifting the 44px Details control 1px above its
  row; compact notice controls now keep stationary bounds. No dismissal,
  diagnostic collection, execution or policy authority changed.
- N6 production evidence: all eight profiles passed in 69.63s, zero skipped or
  flaky cases. Detail/close/dismiss, focus return, short/long messages, portrait
  and landscape, action bounds and axe passed. Reviewed the retained WebKit
  landscape screenshot: complete status row and both controls, no cut-off
  diagnostic text. Traces and JSON/list receipt:
  `/tmp/nebula-stabilization-notice-disclosure-stable`,
  `/tmp/nebula-stabilization-notice-disclosure-stable.log`. This is production
  web with API fixtures; new native verification is still required.
- Native continuation lifecycle contract for the next batch: use only the
  extracted package, isolated XDG profile and inert ACP fixture. Through visible
  controls, approve/reject, stop a separate pending request, enter/exit focus
  mode, close/relaunch the actual app and rediscover saved conversations.
  Core records own terminal outcomes; the adapter receipt file independently
  detects duplicate/late delivery; URL owns the selected conversation. Relaunch
  must not replay a command or invent approval continuation. Record the actual
  native viewport/build and retained screenshots; native graph fullscreen and
  LAN disconnect/restart remain distinct gates, not implied by focus mode.
- Native acceptance extension: three inert ACP protocol cases passed (0.07s),
  covering allow/deny/cancel, independent receipt writes, bounded long output
  and no output on cancellation. Runner lint/compilation passed. Actual native
  execution remains pending a newly built identical package; source-level tests
  are not native acceptance. Selected runs are 1440x900 and 1024x768, with
  approve/reject/stop, fullscreen bounds, application relaunch and wheel scrolling.
- Candidate `1b621b7942e955e1c4539de8e3cda045b350e8bc`, built
  `2026-09-10T18:09:10Z`: 66 selected Python cases passed in 31.18s; Core/web
  identity and all 81 emitted asset hashes match. Managed DEB SHA256:
  `5e8bc450a1e97b32d89d1965438713b6d5b9f1ec4a0885ee35efdffd887efc2b`.
  Extracted native 1440x900 passed approve/reject/stop, durable handoff/progress,
  exact-once receipts, focus-mode bounds, actual app relaunch, saved-conversation
  selection and native wheel scrolling. Reviewed its relaunch screenshot; notice,
  transcript and composer are reachable. Evidence:
  `/tmp/nebula-stabilization-candidate3-WQ2nf3`. The 1024x768 run is pending.
- The same candidate also passed the complete selected native journey at
  1024x768. Both viewport directories retain three independent session states,
  adapter receipts, approval/rejection and relaunch/scroll screenshots. These
  verify A4/A5/A6 and N4/N6 in the extracted package; they are not installed-live
  acceptance or proof of every remaining whole-product journey.

### Output completion contract

- N7 visual review: the retained WebKit 320 Library screenshot shows its primary
  toolbar action cropped to fragments of text. Shared mobile CSS assumes label
  spans, but several page actions supply bare text; their icons can also shrink.
  Journey: discover the primary action on every header, focus it, open its real
  dialog/file chooser, and return. The header must show a complete icon or label,
  keep an accessible name and 44px target, and never crop text into nonsense.
  Use a shared typed action contract for these buttons; test icon/text bounds in
  the permanent primary-view walk, then production real-Core Library on mobile.
  Header presentation is transient; no saved state or runtime policy changes.
  Further shared-header bounds testing found that a populated public-IP control
  consumed all but 7px of the primary action's host at 320px. Keep this secondary
  status compact on mobile, with an accessible disclosure for the full address,
  copy/manual-copy recovery and focus return; never hide required approvals to
  make room. Add disclosure success/failure regressions before changing it.
- N7 verified on the production LAN bundle: 16 primary-screen/dialog journeys
  passed in 195.38s, zero skips/flaky cases, at 1440/1024 desktop and all six
  Chromium/WebKit mobile profiles. Tests now assert primary icon, text and host
  bounds, 44px targets, IP disclosure/copy recovery, accessibility and focus
  return. The desktop walk no longer silently resizes to 1756px. Sources keeps
  its secondary Add URL action beside the source controls so its Upload action
  remains reachable. Retained evidence: `/tmp/nebula-stabilization-header-matrix5`.
  Reviewed WebKit 320 header screenshot. Library also passed both real-Core
  320px engines after the typed header action change (`header-library`, 2 in
  26.67s). Selected frontend tests: 71 passed in 25.43s (`header71.log` under the
  same `/tmp/nebula-stabilization-` prefix). These changes are not yet packaged.
- Library: from Add document or script, upload only a synthetic local document;
  verify failure/retry, indexing, saved-item discovery despite a stale filter,
  inspect/reload/download/reindex/remove and retained immutable artifact. Core's
  item/artifact records and local Chroma index are authoritative; the browser
  owns the chosen file and search filter. A failed upload must preserve a valid
  retry action. No scripts execute. The local embedding model is already cached.
- Reports: extend the saved-note/report journey through Export PDF, verify the
  downloaded PDF contains the persisted title/note and its render references
  that saved revision. No draft or unrelated live content may enter the export.
- Layers: real-Core production LAN desktop first, then the same journeys across
  the eight permanent profiles; retained traces, downloads and durable records.
  Add embedded-UI acceptance support to the real-Core test launcher so final
  packaged-Core LAN checks do not mix in an unrelated static directory.
- L1 red reproduction: a successful Library upload remains hidden behind its
  earlier filter. The production real-Core test found zero visible item rows
  despite the success notice (`output-completion-baseline`, 1 PDF pass/1 Library
  failure). Clear that stale filter only after successful persistence.
- L2 red reproduction after L1: Close Library details changes the route but keeps
  the inspector mounted; its action menu intercepts Download for the remainder
  of the test. The shared `useCanonicalResourceSelection` serves Library,
  assets, evidence, sources and findings. Contract: closing/back to the list
  clears presentation selection, and an invalid new ID must not show another
  record. Preserve an unchanged selected record's edit snapshot/revision.
  Add component navigation regressions before repair, then repeat real-Core
  Library and the affected inspector UI journeys. Evidence:
  `/tmp/nebula-stabilization-library-discovery`.
- L1/L2 green: four navigation regressions passed, including unchanged edit
  revision preservation. The complete selected frontend batch passed 64 tests
  in 34.95s. Production LAN real-Core Library and note/report/PDF journeys passed
  all eight desktop/Chromium/WebKit profiles: 16 passed in 122.60s, zero skips or
  flaky cases. Retained traces, screenshots, original downloads, rendered PDFs
  and durable records: `/tmp/nebula-stabilization-output-matrix`; full command
  results: `/tmp/nebula-stabilization-output-matrix.log` and
  `/tmp/nebula-stabilization-resource-frontend.log`. Library covers stale-filter
  discovery, Close/Back/Forward, reload, download, reindex failure/retry and
  removal while retaining the immutable artifact. Upload failure/lost-response
  behavior itself remains a separate gate; no unsafe blind upload retry added.
- A3 repair passed both missing-record cases and the full 28-case continuation,
  projection and catch-up selection (18.08s). Selected shell/state frontend
  tests: 49 passed in 29.64s. These results do not prove every crash boundary.
- Model/reset: all 16 production LAN real-Core profiles passed in 1.3m, including
  desktop, Chromium/WebKit 320/390/430, and each journey's landscape/fullscreen,
  lost-response retry and scoped reset checks. Evidence:
  `/tmp/nebula-stabilization-model-matrix`.
- New permanent notes-to-report journey passed all 8 real-Core browser profiles
  (desktop 11.4s; remaining 7 in 51.3s). It verifies an injected pre-save failure
  retains the draft, retry creates exactly one saved note, reload, report
  selection/reuse, and an in-place dependency explanation preventing deletion
  of a retained note. Evidence: `/tmp/nebula-stabilization-outputs-real`,
  `/tmp/nebula-stabilization-outputs-real-matrix`. PDF export and Library lifecycle
  still require their own evidence.

This is not a completed whole-product release. Extracted-package native
approve/reject/stop/focus-mode/scroll/relaunch passed on candidate `1b621b7`;
installed acceptance, native graph and explicit transport recovery, remaining
real-Core output/settings lifecycles, crash-at-every-delivery-boundary coverage
and final same-artifact staging walkthrough remain required. The inert approval
browser fixture covers approve/reject/reload/idempotency, not every mandatory
failure scenario. Projection distinguishes Core waiter delivery, adapter
transport/callback handoff and subsequent durable progress; none alone is proof
of remote command execution. No physical device claim is made. Follow
`stabilization-release.md` only after the ledger gates pass.

Candidate `963e76681f289b625fc7815336a7cee0a745ecbe` (built
`2026-09-10T16:48:25Z`) passed the repeated 16-case primary-screen/dialog browser
walkthrough and native approval/rejection/reload/duplicate-decision journeys at
1440x900 and 1024x768. Both runs used the extracted managed DEB and its bundled
Core with an inert local ACP peer; independent receipts prove exactly one
adapter delivery for each decision. This verifies N4's native clipping repair,
not the entire native lifecycle or the subsequent A4/A5 changes. Evidence:
`/tmp/nebula-stabilization-candidate2-2fD6dg`; retained DEB SHA256:
`bcfe717d7a1c66ffdb3600594213396cd43582ee0122a0a689be2d9fa41782af`.

Stabilization work has local commits, but no release has been pushed, installed
or deployed. Live remains unchanged.

## Test and release policy

### Asset/evidence/finding completion contract

L3 reproduced in real Core: asset and evidence uploads persist successfully but
their new rows remain hidden by stale search/filter state. Evidence's source file
survives the injected pre-write failure; asset input does too. The candidate
finding case is still running. L4 unit reproduction: closing a keyboard-opened
inspector leaves focus on the disappearing Close control rather than its opener
(1 failure/4 pass). Add cancellation-safe focus restoration to the shared
canonical inspector hook; deep links use the list search as a fallback. Evidence:
`/tmp/nebula-stabilization-resource-create-red` and
`/tmp/nebula-stabilization-resource-focus-red.log`.
The finding journey initially had an incorrect select locator; after changing it
to the actual named combobox, it reproduced the same saved-but-hidden defect
(`/tmp/nebula-stabilization-finding-create-red`). Clear only this list's filters
after its successful save. The focus regression now passes all five hook cases;
production verification is pending the approval matrix's completion so no
running browser batch receives a changed asset bundle.

From each actual create/upload action, submit a synthetic record with stale list
filters already active. Inject one failure before persistence: preserve the
chosen input/file, explain the error and permit retry. Success must reveal the
saved record, reset incompatible filters, allow keyboard inspection/Close with
focus return, and survive reload. Verify the real Core record; immutable evidence
downloads must match their source bytes. Use disposable projects only. Follow with
finding edit/history navigation to ensure one record's draft cannot be applied to
another; no vulnerability reproduction or external target is involved. Layers:
real-Core production LAN desktop baseline, component ownership regressions,
desktop and mobile engines, final same-artifact/native handoffs as applicable.

L3/L4 desktop production: asset and evidence passed; the finding case also passed
after correcting the test's Inspect locator to its existing named Edit action.
All three preserve failed input, reveal the saved record and return keyboard
focus. Evidence: `/tmp/nebula-stabilization-resource-reveal-green` (two pass,
one test-locator timeout) and `/tmp/nebula-stabilization-finding-history-red`
(finding creation pass, history regression fail).
L5 reproduced: Back to the list, edit another finding, reopen the first silently
loses the first unsaved draft. Bind transient edit snapshots to project + record,
including the original revision, error and in-flight mutation. History may retain
these drafts during the page's lifetime; explicit confirmed discard removes only
that record's draft. A late save must update only its owning record. Do not persist
these drafts across app restarts or silently rebase over another operator's save.
Add deterministic hook regressions and repeat the real-Core history/conflict
journey across permanent desktop/mobile profiles before the packaged gate.
L3–L5 interaction verification: **32 passed in 166.61s**, zero skips/flaky,
across Chromium desktop 1440/1024 and emulated Chromium/WebKit 320/390/430.
The history case includes a delayed saved response while viewing another draft;
only its owning record changes. Explicit discard and focus return also passed.
The original App assertion required awaiting asynchronous confirmation/navigation;
42 focused hook/application tests then passed. Refreshed frontend selection:
**79 passed in 26.45s**. Evidence: `/tmp/nebula-stabilization-resource-matrix`,
adjacent selection/log, and `/tmp/nebula-stabilization-frontend79.log`.

N8 visual review of a successful WebKit 320 trace found the shared inspector and
sticky finding footer are too translucent: underlying list text visibly overlaps
the editable content. Resource interaction success does not waive this defect.
Use the existing theme's opaque overlay surface, retaining its palette and
geometry; verify all resource inspectors, footer readability, long content,
short viewport scrolling and the existing focus journey on the production bundle.
Add a failing computed-surface regression before the CSS repair and inspect the
resulting screenshots. Evidence image:
`/tmp/nebula-resource-review-UO6hqF/77196d61fdd38c32f44195afd5599d7cba0048b9`.
The permanent WebKit 320 regression measured the original background alpha at
0.829804 and failed before repair. Inspectors and their sticky finding actions
now use a theme-matched opaque overlay token. The six-case production LAN check
passed in **54.85s**, zero skips/flaky: findings plus provider/harness Settings,
at desktop 1440 and WebKit 320. Includes opacity in all four palettes, inspector
accessibility, focus/discard and mobile runtime-card touch targets. Theme tests
must use ThemeProvider's storage event path, not just change the DOM attribute:
the latter leaves native controls under the previous inline color scheme. Wait
for the inspector to be fully in view and finish animation before screenshots.
Evidence: `/tmp/nebula-stabilization-inspector-opacity-red` and
`/tmp/nebula-stabilization-settings-inspector-six4`. Broader matrix pending.

### Runtime settings acceptance contract

From Settings → Advanced, add one disposable provider backed by the existing
loopback model stub and one harness backed by the inert ACP fixture. Inject a
pre-write save failure, then a failed health-check response after persistence.
Failed input must remain editable; after persistence the saved profile must be
discoverable, with a Check/Refresh recovery action that cannot create duplicates.
Retry health, rediscover reported models, save a selected default, reload and
verify the same durable ID/revision and model. Exercise disabled/enabled state
and downstream chat selector visibility without executing a tool or sending any
external request. Core owns profiles and health; the form owns only unsaved input.
Health failure is not save failure. Collect the two desktop cases, then repeat
applicable permanent browser profiles and the final production/native candidate.
S1 reproduced after correcting the test's plural endpoint: provider save/health
recovery passes, but harness persistence followed by failed health keeps the Add
dialog open, without exposing the saved profile or a distinct Check retry.
Record the saved identity immediately; make catalog refresh and health failures
recoverable independently, never retry a create because a health check failed.
Evidence: `/tmp/nebula-stabilization-runtime-settings-red2` (one pass/one fail).
S1 component regressions failed for both post-save health and catalog errors,
then passed after separating these phases (2 passed, 1.82s). The saved response
is immediately catalogued by ID; Check retries only health/discovery and no
longer depends on the unrelated MCP catalog. The six-case LAN pass verifies one
durable profile, discovered defaults, reload, disable/enable and the actual
Settings → Workbench → chat selector handoff, with an enabled unsent composer.
The provider's existing local nonce capability probe is asserted explicitly;
no conversation is sent, executable tool invoked or external runtime contacted.
Refreshed frontend selection: **81 passed in 30.72s** at
`/tmp/nebula-stabilization-frontend81.log` (selection adjacent). Earlier browser
attempts exposed test setup issues (plural endpoint, desktop/mobile navigation,
unsent-input name and expected nonce negotiation); retained failed attempts are
not counted as product acceptance. Required eight-profile matrix and native/
same-artifact acceptance remain pending.

N8/S1 expanded production LAN matrix: **48 passed in 351.86s**, zero skips,
unexpected or flaky results, in desktop Chromium 1440/1024 and emulated
Chromium/WebKit 320/390/430. Covers three resource creation/retry/inspection
journeys, revision-bound finding drafts, and provider/harness settings lifecycle.
Reviewed retained WebKit 320 light/dark screenshots: underlying list text no
longer bleeds through the inspector or sticky save actions. Evidence:
`/tmp/nebula-stabilization-resources-settings48-v2` and
`/tmp/nebula-inspector-final-review-NrvbCn`. The first expanded run caught a
test measurement during a finite theme transition (15 pass/1 fail/1 interrupted);
the test now waits for that transition before contrast measurement. This does
not waive contrast failures. One focused evidence case then passed, followed
by the entire reviewed 48-case selection above. Final packaged gate remains open.

### Core outage/recovery contract

The browser-to-Core connection is distinct from a turn's harness transport.
From a disposable conversation with an inert pending approval, terminate only
its fixture Core and keep the UI open. The global connection chip must stop
claiming Core is ready, explain uncertainty and expose a valid reconnect action.
Retain the selected project/session URL, transcript and unsent input; do not
reinterpret connection loss as approval or completion. Reopen the same disposable
database, reconnect through the visible control and show its reconciled interrupted
turn without replay or a receipt. Add a production LAN regression first, then
component health/timeout/stale-response tests if repair is required. Repeat this
through the native supervisor using its isolated profile before release.

A9 reproduced: after its Core process exits, the global chip continues showing
degraded/online because workspace health is read only during bootstrap. Added
bounded read-only health probes, offline/visibility handling and generation-safe
cancellation. Unreachable Core now exposes reconnect while retaining draft/URL.
The production LAN outage journey passed **8 profiles in 59.95s**, zero skips/
flaky, with durable interrupted state and zero adapter receipts. Ten focused
connection/recovery unit tests passed in 0.95s. Compatibility checking then caught
a network-online regression: marking the workspace offline suspends session
polling, so the online event must re-bootstrap after an observed connection loss.
It must not retry an authentication/setup failure or replay a mutation. Added a
workspace integration regression; the three-case approval compatibility repeat
and native supervisor outage gate remain pending. Evidence:
`/tmp/nebula-stabilization-core-outage-red`,
`/tmp/nebula-stabilization-core-outage8`,
`/tmp/nebula-stabilization-connection-approval3`.

A9 compatibility repair uses successful read-only Core reachability, not just a
browser online event, to re-bootstrap saved state after loss. Probes are bounded,
coalesced, cancelled on binding changes and ignored after their deadline. They
continue after loss without repeating the error; no command or approval is
replayed. The existing disconnect/restart-waiting/crash-after-record cases now
pass (**3 in 33.2s**), plus **11 focused connection/recovery tests in 0.98s**.
The refreshed frontend selection passed **92 tests in 37.74s**. The Core failure
copy assertion was updated to the new accurate unavailable/reconnect wording.
A test-only unsupported locator option initially blocked the production build;
two prematurely started old-bundle browser runs were terminated and are not
acceptance. Successful rebuild: `/tmp/nebula-stabilization-connection-build4.log`.
Compatibility evidence: `/tmp/nebula-stabilization-connection-approval3-v4`;
frontend: `/tmp/nebula-stabilization-frontend92-v2.log`.
Final A9 outage repeat after the compatibility repair: **8 passed in 68.37s**,
zero skips/unexpected/flaky, all permanent real-Core desktop/mobile profiles.
Evidence: `/tmp/nebula-stabilization-core-outage8-v4`. The native supervisor
outage, graph and identity journeys still require the next clean package.

### Native graph and identity completion contract

Extend the permanent extracted-package journey, not an ad hoc browser script.
Seed a small synthetic mechanism graph in its disposable project; enter Model
through the native tab, select an outline object, expand Relationships, resize
the actual native window and restore with focus/selection preserved. Assert both
the panel's viewport bounds and actual node spread. Then enter Settings through
navigation and Diagnostics through its visible link; all three displayed
Interface/Core/Desktop commits must agree. Core owns graph data, UI owns viewport
and selection, build manifests own identity. No live model or installation is
modified. Actual clean-package runs at 1440 and 1024 remain the required gate.

### Native visual review: N9 relationship label collisions

Candidate `206ab62`, native 1440 → 1024 resize: the fullscreen panel fills the
window and retains selection, but relationship buttons overlap object cards and
each other, obscuring names. Evidence:
`/tmp/nebula-stabilization-candidate4-gqcUYx/native1440/native-model-fullscreen-resized.png`.
The map must preserve readable object names and individually reachable labeled
relationship controls at desktop/mobile widths. Graph objects/edges remain Core
owned; positions and measured button bounds are presentation only. Add a failing
production collision regression, then a deterministic placement test; verify
resize, long labels, keyboard inspection, focus restoration and dense maps.
Do not hide required relationship actions or change the model schema.

The same native journey initially asserted transcript contents immediately when
global Core readiness returned. URL/draft were retained, but history refresh is
asynchronous. The regression now waits for exactly one saved answer and still
fails on absence/duplication. Native final completion remains gated on that check.

Candidate 4 artifact identity: `206ab62ea4b451018e3b07160d22cbec75458a93`,
timestamp `2026-09-10T20:20:21Z`, version `3.0.0-alpha.13`, managed Linux x86_64.
DEB SHA-256 `a5fb7d9a6c46277d7d08636356c54612f3a004db9a5481effb2d3be6951da14e`;
extracted Core SHA-256 `26fa6b61ba917e18333547e5c8577b4eb4e9c350d3472bed3061aded640d7218`.
Both match retained artifacts in `/tmp/nebula-stabilization-candidate4-gqcUYx`.
All 81 embedded web assets passed exact integrity checks before/after Tauri.
Python selection: **66 passed in 31.81s**. Native **1440 and 1024 passed** the
inert approve/reject/stop, restart/relaunch, transcript scrolling, fullscreen
resize, unsent draft retention and three-way displayed identity checks. Native
evidence: `native1440-v2` and `native1024` under the candidate directory. N9
visual overlap is not waived by those earlier geometry/interaction passes.

The compiled-Core LAN harness initially used API-only `serve` without a static
directory: root returned 404. That is a harness entry-point error, not acceptance.
It now uses the packaged product's `ui --no-browser --lan --allow-insecure-lan`
entry point, reads its generated disposable token privately and clears any external
UI directory override. One asset case then passed (12.8s), followed by **48
resource/draft/settings cases in 481.30s** and **16 note/report/PDF/Library cases
in 301.77s**. Both matrices used the exact compiled Core plus embedded web assets
on all eight profiles; zero skips/unexpected/flaky. Failed original batches are
retained but not counted. Evidence: `resources-settings48-v2` and `outputs16-v2`.

N9 production regression failed with an overlapping label in the dense model.
Labels now use measured free row space, avoid each other and keep 44px targets;
crowded layouts extend the existing scrollable canvas. Object positions/schema
are unchanged; opaque cards and subtle label guide lines retain readability.
**23 graph/page tests passed in 3.64s**, then the desktop regression passed in
14.7s including 2800px expansion. Its screenshot was reviewed. Evidence:
`/tmp/nebula-stabilization-model-collision-red2`,
`/tmp/nebula-stabilization-model-unit23.log`,
`/tmp/nebula-stabilization-model-collision-green`. Eight-profile and updated
native collision checks remain required. The first red attempt used the wrong
Python import path; it is setup failure, not a product regression result.
N9 eight-profile result: **8 passed in 102.14s**, zero skips/unexpected/flaky,
at `/tmp/nebula-stabilization-model-collision8`. Includes desktop 1440/1024,
ultrawide 2800 and emulated Chromium/WebKit 320/390/430, long labels, accessible
inspection/focus, restoration and short landscape. Native collision assertions
have been added; they require a package containing the actual graph repair.

### Browser handoff contract

From Workbench → Browser, choose the device-browser engine, open a harmless local
fixture in a real separate tab, and return to Nebula. The opened tab must have no
opener; the original screen must explain that embedded inspection is unavailable
on this device without falsely reporting that a successful popup was blocked.
Retain the normalized address, scope indication and a valid retry path. The
browser owns tab creation; Core owns the selected project's saved scope; React
owns unsent address input. Existing tests replace window.open with a fake handle,
so add a real popup journey before repairing any returned-handle assumption.
No research execution, scanning, remote URL ingestion or external navigation.

### Next approval failure-injection contract

Baseline desktop: Stop, actual double-click, lost decision response and browser
offline/reconnect passed. A7 reproduced: with two independent requests on one
turn, deciding the displayed request leaves one durable pending request but zero
review cards. `pendingResponse` is singular, gets cleared after every decision,
and reload chooses the turn's last approval ID even if already decided. Restore
the exact pending request from the canonical projection, including event gaps and
reload; never replay a command or infer an ID from another turn. Evidence:
`/tmp/nebula-stabilization-approval-failures-baseline` (4 pass/1 fail; six cases
not run due to fail-fast, not waived). Add canonical selection and stale-session
guards, then repeat the full eleven-case failure selection.
The added twelfth case found A8: hold the first decision's HTTP response after
Core completes it, switch to a fresh conversation, and request another inert
approval. Its Approve button remains disabled by the previous conversation's
mutation state. Reset presentation busy state on selection and guard late
decision callbacks by the existing selection generation. The prior approved
work remains approved; no cancellation or replay is inferred from navigation.
Red evidence: `/tmp/nebula-stabilization-late-approval-red`.
The combined run passed the first eleven scenarios, including A7 and reload,
then exposed a second A8 path: an already-started history refresh from the old
turn reselected the previous conversation. Guard asynchronous history and
interaction results, catalog-driven selection, follow errors and detached
stream completion—not only the decision response. Evidence:
`/tmp/nebula-stabilization-approval-failures-isolated` (11 pass/1 fail).
A7/A8 green: the delayed response/history case passed three independent desktop
repetitions (21.92s). The complete twelve-case failure matrix then passed all
eight production LAN profiles: **96 passed in 531.49s, zero skips/flaky cases**.
Includes Stop, double-click, lost response, offline/reconnect, two independent
requests plus reload, restart while waiting, adapter exit, four crash barriers
and conversation switching with a delayed response. Durable records and independent
receipt counts agree; no commands execute or replay. Retained selection/traces/
screenshots/records: `/tmp/nebula-stabilization-approval-failure-matrix` and its
adjacent `.log`/`-selection.log`. Five state-hook tests passed; the broader 72-case
frontend selection passed in 28.26s before the final late-history guards. Repeat
frontend coverage on the final diff. Native/extracted and final artifact gates
remain required; this is not release approval.
The refreshed frontend selection passed **73 tests in 24.66s**, including the
final late-history guards and the separate inspector-focus regression. Collection
and log: `/tmp/nebula-stabilization-frontend73-selection.log` and
`/tmp/nebula-stabilization-frontend73.log`.

Use only the disposable approval Core and inert local adapter. Start from New
chat, send, review the actual card, then exercise Stop, double-click, a lost
decision response, offline/reconnect, a second independent pending request,
adapter exit and process termination before/after waiter delivery and progress.
Core's saved approval/owning turns are authoritative; an append-only local
fixture receipt independently counts delivery. The browser URL retains identity.
Restart must reopen the same disposable database and reconcile interruption,
never recreate the request or emit another receipt. Decisions, handoff and
observed progress remain separate. No tool, model call or target interaction
executes. First collect the desktop failure cases; repeat affected failures on
permanent mobile profiles and the eventual packaged candidate. Approval policy
is unchanged. These cases supplement, not replace, native adapter acceptance.

Collect exact cases before each bounded batch; record count/filter/projects and
exclusions in `.github/test-selection.json`. No full suites. Matrix: desktop
Chromium 1440/1024; emulated Chromium/WebKit 320/390/430 plus short landscape.
Check keyboard/focus, labels, 44px targets, zoom, reduced motion and scrolling.
Physical phones are unverified, outside the selected release bar. Production LAN,
real Core and installed-package acceptance are required. Mocked tests are not
evidence of native continuation or durable reconnect.

Deployment additionally requires reviewed commits, consistent backup, retained
rollback artifacts/configuration, no interruption of active work without a
maintenance handoff, and matching Core/web/native identities. Pending gates are
not permission to ship.
