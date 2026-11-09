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

Update this checkpoint with exact file paths, evidence, test selections, commits,
and remaining blockers as work proceeds. Do not replace unknowns with inferred
success.
