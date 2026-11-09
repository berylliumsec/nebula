# Standing-instruction work checkpoint

Reload [the complete durable goal](standing-instructions-goal.md) and this file
after compaction or resumption before continuing.

- Confirmed root cause: independently validated by root's fresh-context
  `validate_standing_instructions` agent. A disposable Core
  harness trace accepted two turns on one retained session/connection; both
  actual prompts carry the same saved operator decision and assistant scope.
  A separate ACP capture shows `session/new` followed by `session/load` for the
  same external session, each sending identical developer text. Raw synthetic
  requests/events: `docs/validation/standing-instructions-baseline.json`.
- Affected paths traced: `harnesses.py::prepare_chat` appends decisions, selected
  skills, goal skills and assistant scope to every retained user prompt;
  `GrokAcpConnection` tracks developer delivery per connection only. Codex and
  Claude already expose separate developer/system instruction channels.
  Provider `chat.py` rebuilds project instructions outside compacted history;
  no stateless delivery repair is established or authorized by this evidence.
- Implementation decisions: no behavioral patch before independent causal
  validation. Preserve stateless delivery and all execution/approval safeguards.
  The independent gate passed before product edits. The implemented repair moves
  native static authority from retained user prompts to a separately installed
  instruction channel; a turn stores the exact full snapshot, with a durable
  external-id/hash receipt after channel acceptance. Legacy/edited channels
  require a new native context with saved conversation handoff, preserving the
  Core chat. No provider changes or schema migration. Edits currently touch
  `harnesses.py`, new `harness_instructions.py`, exact instruction fields in
  `project_instructions.py`/`skill_catalog.py`, and focused new tests.
  The guarded native channel was verified from primary source and implemented:
  Grok `session/new` supports `_meta.rules`; the system head survives load and
  compaction. Source pin: `xai-org/grok-build` commit
  `2bdd1d6a6369de0e8c68132ea4539e9abd9e14a8`, files
  `crates/codegen/xai-grok-pager/docs/user-guide/15-agent-mode.md`,
  `crates/codegen/xai-grok-shell/src/session/acp_session_impl/prompt_build.rs`,
  `crates/codegen/xai-grok-shell/src/session/compaction.rs` and
  `crates/codegen/xai-chat-state/src/compaction_utils.rs`. Source inspection
  establishes protocol intent; installed vendor execution remains unverified.
- Completed validation: raw baseline capture on `a9066936`, two accepted Core
  harness turns (one connection) and initial/reloaded ACP requests. First
  diagnostic cleanup called nonexistent `close()` (corrected to `shutdown()`).
  Existing FakeAdapter lacks memory-managed opener, so the initial Core run was
  correctly refused; retained its raw rejection. The successful reproduction
  uses an explicitly inert in-process fixture opener, launching no worker and
  changing no product memory policy. Provider wire captures now cover both
  OpenAI chat and Responses assembly: initial, subsequent and forced-compaction
  requests each contain the synthetic project instruction exactly once. Seven
  actual synthetic compaction requests contain no project instruction text.
  `standing-instructions-contract.md` records the operator journey and gates.
  First focused run: 25 passed in 10.86s. Expanded collected selection: 35;
  34 passed and the exact goal-skill test failed in 14.37s. The failure occurs
  before delivery: original AGENTS.md and skill bodies with leading/trailing
  whitespace are stripped by their value models while their recorded digest
  still identifies the original bytes. Raw evidence is in
  `standing-instructions-exact-text-baseline.json`; a fresh independent causal
  review passed before disabling trimming only on ProjectInstructions.content
  and SkillSnapshot.instructions. Metadata normalization and blank-skill refusal
  remain. Expanded selection59:57 passed/2 fixture setup failures in17.77s;
  those two were corrected and passed in5.39s. No product failures remain in
  that selection. Final reviewed selection: **74 passed in18.24s**, including
  13 UTF-8 cap/digest/one-read cases and the new cold-process/combined-input-limit
  guards. Strengthened unchanged restart/reconnect cwd assertions separately:
  2 passed in3.08s. Ruff and diff whitespace checks pass. LAN Core streamed
  initial/subsequent and edited instructions
  through real authenticated HTTP at `http://172.26.91.238:38589` on the final run, with
  deliberately lossy inert ACP
  compaction; unauthenticated completion was refused401. No UI/browser or live
  installed-vendor result is claimed. Ruff and diff whitespace checks pass.
  Source-only native caveat confirmed:
  memory enablement reconciliation can replace the system head, and Grok applies
  `_meta.rules` only at creation. Managed ACP now forces its own cold process and
  confirms a fresh exact native system-head artifact before sending a prompt;
  ignored/unknown rules fail visibly, and a rewritten head on load starts a fresh
  context with full current instructions. Original per-connection delivery stays
  available for unmanaged ACP compatibility. The readback uses bounded bytes plus
  UTF-8 decode, without newline normalization or symlink traversal. Synthetic
  unchanged reload, ignored rules, stale receipt and memory-head rewrite cases
  pass. Installed-vendor execution remains unverified.
  LAN raw calls/events are durably captured in
  `standing-instructions-lan-capture.json`, with synthetic gateway credential
  redacted. Listener stopped and exact owned synthetic LAN data, native artifact
  workspace and gateway folder removed after capture. A separate partial-UTF8
  AGENTS hash mismatch is reproduced in
  `standing-instructions-truncated-text-baseline.json`; root's fresh
  `validate_instruction_utf8_digest` agent independently confirmed both the
  hash-before-transform defect and complete U+FFFD loss. The validated repair
  keeps one cap+1 read, uses the incremental UTF-8 replacement decoder with
  final=not-truncated, and hashes the exact final UTF-8 body. It preserves the
  malformed-interior replacement policy and the raw64KiB cap.
  Rebase onto exact integration base `698d82a0` completed; only the selection file
  conflicted and was recomputed while retaining74 cases. Eight relevant
  Kernel/storage/native-channel/Butler/persisted-goal checks passed in8.03s after
  rebase. Latest authenticated LAN origin: `http://172.26.91.238:37839`; its updated
  sanitized capture includes the integration base and source SHA-256 identities.
  Final code commits: `c8f5ecc8` exact fields, `d758fc0f` UTF-8 boundary/digest,
  `61507cf6` native channel; baseline/contract `897ca20a`, durable goal `05d202be`.
- Remaining work: Root owns final diff review, public-host
  and browser/installed-native acceptance decisions, draft PR publication and
  merge when repository gates permit. No deployment or existing installation
  was touched. Old trimmed immutable skill content cannot be reconstructed.
- Follow-up public-host acceptance (helper ready): root requested a dedicated
  retained-bundle helper after reviewing `e18e08a2`. Work is isolated on
  `codex/standing-native-acceptance-fixture`; the reviewed branch remains at e18.
  New files are `tests/v3/standing_instructions_native_fixture.py` and its focused
  `test_standing_instructions_native_fixture.py`. Acceptance owns all shared
  supervisor/bundle/browser/spec/config edits. The helper seeds one synthetic
  native profile and saved Alpha constraint, binds only an inert in-process ACP
  transport into each actual retained execution's Grok adapter, and preserves
  the production open/run/readback guard. Authenticated project controls expose
  actual captures, deliberately lossy native history compaction, and genuine
  adapter disconnect/reload. Zero native workers are launched; this cannot prove
  installed Grok, native memory readiness, or GUI support. The ordinary saved
  decision API changes Alpha to Beta, followed by visible chat delivery; no
  existing saved-decision editor exists in the current UI, and root accepted
  the API edit for this evidence.
  Two helper checks were collected. Initial2 failed in5.41s because the narrow
  test omitted `default_runtime_platform`, which production execution_worker
  normally supplies. Adding that real platform retained the workspace guard.
  Next1 passed/1 failed in5.91s: the inert transport wrote to the profile account
  while a Core-planned context correctly reads its isolated settings account.
  Root's fresh `validate_integration_diagnostics` independently confirmed both
  fixture causes from code and actual captured session/new rules; the same body
  passes readback at the wrong fixture path and fails at the required isolated
  path. The approved helper repair carries the selected account home per native
  session and validates the same account on load. After it, the positive journey
  passes; the second check failed in6.83s because its no-write monkeypatch did
  not accept the new account_home keyword. The same fresh validator independently
  confirmed that exact persisted error before the test-only stub was corrected.
  The two checks then passed in6.98s. Acceptance requested an observational
  `turn_active` capture field matching the real409 precondition; it was added
  and the same2 checks passed in7.09s, bounded60s. Actual managed events record
  copies `(1,0),(1,0),(1,0),(0,1)` across initial, lossy compaction, genuine
  reconnect/load, and Beta edit. Thirteen actual ACP calls create two native
  contexts; the unchanged Alpha context loads successfully and the compaction's
  system-head SHA256 remains identical. Production missing-head readback still
  refuses the turn before any session/prompt. Exact helper SHA256:
  `0be64f41cf5e63eade3c1815c78bf425c1870489268eb542463586a5b1a2469d`.
  The shared
  `.github/test-selection.json` is untouched under root ownership; local2-case
  diff-bound `--plan` and receipt are in `/tmp/standing-native-fixture-*json`,
  with a60s run bound. No full suite or live provider was invoked. Root/acceptance
  still own the combined public HostAPI/IPC production LAN/browser run, final
  source/UI pin, shared selection, review and merge. This helper check is
  authenticated Kernel evidence and must not be labeled public HostAPI/browser
  or installed-vendor evidence.

Update this checkpoint with exact file paths, evidence, test selections, commits,
and remaining blockers as work proceeds. Do not replace unknowns with inferred
success.
