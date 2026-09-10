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
| Launch/navigation | Connect/pair, select/search, deep-link, back/forward, reload/relaunch preserve the correct identity | Core/device auth, URL | component, real Core LAN, native | Pending |
| Assistant | Select runtime, send/stream, approve/reject/stop, complete, queue/switch/reconnect/retry without duplicate work or contradictory status | approval, turn, harness request, canonical projection | service failure injection, real Core, UI, real harness, native | Reported approved decision with waiting turn; full reproduction pending |
| Browser | Navigate/input, control handoff, capture, reload/reconnect, explain unsupported devices | Core browser session and owning device | local fixture, UI, LAN/native | Pending |
| Model | Inspect mechanisms/evidence, select/filter/expand/resize, clear only model/captures | Core model/evidence, local viewport | component, real Core, LAN/native | Pending |
| Workspace | Link/browse/upload/edit/save/recover, terminal, switch projects; one linked folder everywhere | Core workspace, filesystem, device drafts | service, real Core, native | Pending |
| Project/outputs | Create/update/archive/restore and discover/reuse notes, evidence, findings/reports/Library | Core durable entities | real Core, UI | Pending |
| Settings/recovery | Save/rediscover runtimes and policies; retry failures; show accurate build/capability identity | Core catalog, frozen session policy, build manifest | component, real Core, native | Pending |

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

### Candidate native and additional lifecycle findings

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

This is not a completed whole-product release. Packaged/installed desktop
approval/reconnect/fullscreen/scroll/relaunch acceptance, the remaining real-Core
workspace/output/settings lifecycle journeys, crash-at-every-delivery-boundary
coverage, and final same-artifact staging walkthrough remain required. The
current inert approval browser fixture covers approve/reject/reload/idempotency,
not every mandatory failure scenario. Acknowledgement currently records Core
waiter delivery; vendor execution is not inferred from it. No physical device
claim is made. Follow `stabilization-release.md` only after the ledger gates pass.

No stabilization release has been committed, pushed, installed or deployed yet.

## Test and release policy

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
