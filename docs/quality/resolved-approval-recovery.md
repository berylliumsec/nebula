# Resolved approval recovery

Contract before implementation: reload paused conversation > read durable approval
decision > restore transcript/status without throwing a reload-loop error > check
status or explicitly stop waiting. No replay, reapproval, continuation or live
decision is authorized by merely opening/reloading the conversation.
Core approval owns decision; turn owns progress; React owns notice/focus. A resolved
approval is not a pending operator decision, even if the turn remains paused.
Other pending requests must stay discoverable. Old Core versions need a narrowly
ID-scoped UI fallback. Controls share composer width and remain visible in short
windows; long diagnostics scroll separately. Preserve drafts, files and model data.

Planned tests: catch-up projection/API, component and permanent production-browser
desktop/mobile stale-approved/rejected states, reload and short-window geometry.
Use disposable state only. Physical desktop packaging, devices and live harness
continuation are unavailable gates; report partial verification explicitly.

## Implementation and evidence

Root cause: restoration threw for a durable resolved approval before reconnecting
to the waiting turn. Catch-up also counted the waiting turn as a pending decision.
Restoration now distinguishes the recorded decision from worker progress. Core
omits resolved decisions; a narrowly ID-scoped UI fallback handles older Cores
without hiding other requests. Progress clears the paused notice, not that fallback.
The compact notice shares the composer track and offers explicit status/stop
controls. Short windows allow scrolling to both the notice and composer controls.
The product rule is to recover from authoritative state in place, without requiring
reload loops or putting recovery controls in a clipped diagnostics region.

Selected checks (five-minute ceiling per test command):

- Core: `PYTHONPATH=src /home/agent/nebula/.venv/bin/python -m pytest -q tests/v3/test_chat_catchup.py --maxfail=1`: 11 passed. Disposable SQLite HTTP requests cover pending and five resolved statuses, repeated catch-up reads, and unchanged durable revisions/statuses.
- Components: `npx vitest run src/components/ChatCatchUp.test.tsx src/components/ResolvedApprovalNotice.test.tsx`: 6 passed. Covers independent requests, stale cards, explicit callbacks, busy controls, and unsupported stopping.
- `npm run build`: passed (existing chunk-size and Tauri mixed-import warnings).
- Browser selection: `tests/interface.spec.ts`, grep `pending approval restores and reviews (resolved|stale catalog)`, eight projects recorded in `.github/test-selection.json`, 24 cases. Production preview uses `http://192.168.1.155:1442`, API/WebSocket fixtures, two workers. Covers approved/rejected reload, progress, stale-count suppression, explicit stop, existing pending review, mobile Chromium/WebKit profiles, 844x430 short-window scrolling, 44px controls, composer alignment, and scoped axe checks.
- Browser result: all 24 passed in 1.5 minutes on the final production build.
- Screenshots/results: `/tmp/nebula-resolved-approval-final-verified`. The desktop short-window screenshot was visually inspected; reachability of composer settings and recovery controls is asserted separately.
- Ruff and `git diff --check`: passed.

This is partial product verification: browser requests are mocked, while real Core
persistence is tested separately. There is no end-to-end live Core/harness browser
run, physical mobile/touch or screen-reader run, installed native-package rebuild,
or live Grok continuation evidence. Native app and live server are unchanged; no
live approval was replayed, approved, stopped or resumed. A production web build
does not prove that the installed app contains this change.
