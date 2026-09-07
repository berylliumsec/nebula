# Phase 5 — Explicit decisions and constraints

Operator journey: promote a message or exact selection into an editable conversation entry, save/revise it in Context, inspect its history, supersede/remove it or explicitly promote it to the project. Entries have source provenance and optimistic revisions. Forks copy applicable conversation entries with original provenance. Native and harness turns snapshot current active entries; queue entries resolve them at dispatch. Submitted snapshots remain unchanged when an entry is edited later.

Validation: 48 focused Python tests pass across chat/API/workspace/queue/decisions, including source validation, stale writes, project promotion, fork boundaries, dispatch-time revisions, immutable submitted snapshots and harness instructions. The seeded-editor component regression passes under StrictMode. Frontend diagnostics and Ruff checks pass; production build succeeds with existing chunk warnings.

All 8 permanent real-Core production LAN browser projects pass (Chromium desktop 1440/1024; Chromium Android and WebKit iPhone emulation at 320/390/430). The journey saves, edits and promotes an entry, runs queued work after tab detachment, and checks both the recorded context and actual provider routing/final-synthesis requests. A WebKit initialization race discovered during validation is covered by the component regression. Trace-enabled browser evidence, origin and bundle identity are retained at `/tmp/nebula-assistant-evidence/phase05` on the implementation host.

Migration: additive `chat_decisions` documents and optional request/message snapshot fields. Existing conversations require no generated backfill. Managed bookmark, queue and decision records are excluded from generic CRUD so callers must use their validated workflow routes. Conversation deletion removes conversation decisions; promoted project entries remain explicitly project-owned.

Physical-device keyboard testing and live native-vendor acceptance remain final verification gates. Production is unchanged at this phase.
