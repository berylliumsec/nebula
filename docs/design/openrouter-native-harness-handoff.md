# OpenRouter native harness implementation handoff

Updated: 2026-09-18 (G9 operator-workflow increment)

## Non-negotiable ownership boundary

- `.agents` is the provider-neutral project tree.
- Nebula-native/OpenRouter/other non-harness provider turns consume `.agents`
  directly.
- Codex and Grok remain harness-owned. Nebula does **not** manage their context,
  compaction, goals, checkpoints, hooks, continuation, or model switching. Do not
  route the native-hook implementation through `harnesses.py`.
- Harness status may be displayed only when reported by the harness adapter.

## Current authoritative implementation

The broader G1-G7 work is present in this dirty worktree; consult
`openrouter-native-harness.md` for the requirement-by-requirement status. G9 is the
active area.

Implemented and previously verified:

- Native hook discovery is restricted to
  `<workspace>/.agents/hooks/*/hook.json` in `src/nebula/v3/native_hooks.py`.
- Versioned manifests validate event names, bounded relative executables, timeout,
  declared side effects, and `continue|block` failure policy.
- Selection freezes manifest/executable digests; execution revalidates both.
- Hook attempts and bounded output are durable `NativeHookExecution` entities.
- Provider requests accept `hook_ids`; exact snapshots are stored on provider
  `ChatTurn.request_snapshot`.
- Provider turns emit started/completed events once. The current worktree also adds
  best-effort failed/cancelled terminal events.
- Hook output cannot mutate approval state.
- Restart marks running hooks interrupted. Unknown workspace/external effects block
  resume and require explicit operator reconciliation; effects are never replayed.
  A hook declaring `side_effects=none` may retry. (Superseded on 2026-09-22:
  recovery now carries unknown effects forward and resumes automatically; see
  `execution-recovery-lifecycle.md`.)
- Authenticated APIs expose the project catalog, safe execution summaries, and
  revision-bound hook reconciliation.

Operator workflow added in the latest partial increment:

- `ui/src/api/types.ts` and `ui/src/api/client.ts` map hook catalog, selected IDs,
  execution summaries, and reconciliation.
- Provider Assistant settings in `ui/src/pages/SessionsPage.tsx` show explicit hook
  checkboxes. Harness mode clears/hides them.
- Selected hook IDs are sent only on provider chat requests.
- The UI loads durable outcomes after completion and for restored pending turns.
- Restart recovery distinguishes unresolved hook executions from unresolved tool
  calls and sends the matching reconciliation request.

## Verification evidence

Passing for this operator-workflow increment:

- Ruff on touched Python files.
- Focused backend tests including failed/cancelled lifecycle events, HTTP execution,
  store-cap paging, snapshot/once-run, and restart reconciliation.
- `ui/src/App.test.tsx`: 37 passed, including the named provider hook journey and
  hook restart reconciliation.
- `ui/src/api/client.test.ts`: 57 passed.
- `npm --prefix ui run build`: production TypeScript/Vite build passed.
- Catalogued mocked Playwright: 6 passed (desktop, mobile Chromium, mobile WebKit).
- Production-bundle real-Core LAN: 3 passed (desktop, 390px Chromium, 390px WebKit).
- `git diff --check` passes.

## Hook selection decision

Hook checkboxes are per-mounted conversation UI state for the next provider turn.
They remain selected across later messages while Workbench Chat stays mounted so a
continuous session does not require re-checking. They are not a durable
project/session preference: reload clears them because hook side effects must stay
an explicit opt-in. Core remains authoritative for which hooks ran via turn
snapshots and `NativeHookExecution` records. Do not store this in local storage.

## Pagination

A request may select at most 32 hooks across four lifecycle events, so a single
turn is far below 1,000 attempts. The store still pages at 1,000 rows. Core now
pages engagement-scoped executions and returns the complete turn-scoped list on
`GET /chat/turns/{id}/hooks` and `GET /chat/sessions/{id}/hooks`. No operator-facing
page control is required.

## Current unfinished check

G8–G10 now have Core/UI implementations and focused tests. The approved design is
still not a launch-complete claim: live OpenRouter inference, physical devices,
G3 visible approval-denial/cancellation journeys, and G7 fault injection at every
request/tool/receipt boundary remain open.

## Required next work

1. Bounded live OpenRouter model/tool turns (G1/G3) with two model families.
2. Real-Core child delegation, checkpoint restore, and scheduled occurrence
   journeys on desktop plus 390px engines.
3. Physical-device acceptance.
4. Do not claim broad Codex/Claude parity.

## Worktree cautions

- The worktree contains extensive staged and unstaged work from this implementation.
  Preserve unrelated edits; do not reset or blanket-stage.
- New files were staged earlier so selection digests include them.
- Full suites are prohibited without fresh explicit user approval. Follow
  `docs/TEST_SELECTION.md` and the mandatory Nebula product-quality skill.
- Recompute the test-selection digest after this increment; do not reuse the previous receipt.
