# Integration audit fixes — 2026-09-09

Baseline: `79e9fae` (PR #265). These changes address the eight findings from the
integration audit without changing application APIs or persisted schemas.

## Operator contracts

| Journey | Invariant | State authority | Regression layers |
| --- | --- | --- | --- |
| Continue/fork a long harness conversation | Latest conversation context and increasing message sequence survive project-wide pagination | Core ChatMessage records | Real SQLite service tests with >1,000 messages |
| Edit, change tabs, reload, recover | Every unsaved buffer is recovered, or a save failure preserves the last snapshot and explains recovery | Browser IndexedDB; Core for saved files | Persistence/provider tests; production real-Core editor journey with 21 buffers |
| Open/reconnect to a mission | Replayed events do not double-count progress; live progress agrees with saved state | Core run snapshot and task records | Native verification tests; provider/transport tests; browser replay/retry journey |
| Retry Library | Retry fetches global Library items even without a project | Core Library | Provider and browser recovery tests |
| Retry mission activity | History, selected identity, and event stream return together | Core plus URL | Provider and browser recovery tests |
| Recover harness discovery | Failed discovery remains visible and retry restores runtime choices | Core harness catalog | Provider and browser recovery tests |
| Configure default, start chat | Configured harness default wins over discovery order | Harness profile; UI new-chat selection | Selector tests; production browser new-chat/reload journey |
| Copy on HTTP LAN | Exact text is copied, focus/selection return, failure explains manual recovery | Browser clipboard capability | Clipboard/component tests; production HTTP LAN browser journey |

Create/use, refresh/reconnect and failure/retry are the relevant lifecycle steps.
Deletion/revocation semantics are unchanged. No new layout, permissions or runtime
capabilities are introduced.

## Implementation decisions

- Harness history readers paginate all project messages before selecting the
  conversation. All four message/context consumers share this behavior.
- Editor recovery no longer truncates at 20 buffers or 50 projects. An oversized
  or invalid save rejects before overwriting the prior IndexedDB snapshot. The
  existing recovery warning tells the operator to save/download drafts and retry.
- Mission events drive the timeline and invalidate run snapshots; they do not
  increment already-current counters. Refreshes coalesce concurrent invalidations
  and repeat if another event arrives in flight. Replay completion refreshes the
  selected snapshot, including after reconnect. Native verification now publishes
  `run.progress` atomically with the authoritative completed-task count.
- Library and harnesses have explicit retry branches. Activity retry restores
  history and reinitializes selection/streaming. Failed catalog loads remain
  visible; successful recovery clears the degraded state.
- New-chat/runtime-switch defaults honor the configured harness model.
- Copy controls share modern-API/legacy fallback behavior, exact-text copying,
  focus and selection restoration, and failure feedback.

## Local acceptance evidence

- Frontend: 80 files / 427 tests passed with `npm --prefix ui test -- --maxWorkers=2`.
  An initial unrestricted run hit the pre-existing 5-second HostFolderPicker test
  timeout under concurrent work; the bounded-worker full run passed unchanged.
- Backend affected suites: 67 passed (`test_harnesses.py`, `test_orchestration.py`).
  The harness adapter is a fixture; SQLite storage and service logic are real.
- Production bundle built; frontend diagnostic audit passed.
- Defaults and Copy: 16 production HTTP LAN checks passed across desktop
  1440/1024 and mobile Chromium/WebKit default/320/430 profiles.
- Workspace recovery/replay: 4 production HTTP LAN checks passed across desktop,
  compact, mobile Chromium and mobile WebKit.
- Real-Core editor: production browser journey passed, including saving real
  workspace files and restoring the active 21st unsaved buffer after reload.
- Real-Core mission ledger: production LAN failure, retry and relaunch journey
  passed with a local model fixture and executable tools disabled.
- LAN origin: `http://192.168.1.155` with isolated ephemeral test ports.
- Mobile coverage is emulation. No physical-device test was performed.

The complete hosted CI checks remain the merge gate. These local results do not
claim physical Safari validation or a new release/package publication.
