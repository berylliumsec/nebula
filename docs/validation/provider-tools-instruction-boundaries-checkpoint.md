# Provider instruction boundary checkpoint

Reload the [complete repair goal](provider-tools-instruction-boundaries-goal.md)
and original [standing goal](standing-instructions-goal.md) after resumption.

- Confirmed cause: `without_tools_section` strips the first textual managed
  delimiter pair or hints line from any request instructions, even on first
  application without ownership metadata. Global periodic Butler authority is
  raw: `read_butler_instructions` -> `_global_instructions` XML body -> private
  request recovery instructions -> ChatService preparation -> tool renderer.
  Paired markers lose mandatory content, unmatched start raises
  CorruptRecordError, and a literal hints line is removed.
- Independent gate: root's fresh `validate_tools_instruction_boundaries` ran
  actual `_global_instructions` and `ChatService.prepare_async` and confirmed all
  three cases before repair. Ordinary project/skill/decision/goal JSON paths
  escape literal LF delimiters and are safe; the initial project-path suspicion
  was ruled out. Synthetic pure-loader/renderer evidence is in
  `provider-tools-instruction-boundaries-baseline.json`.
- Affected paths: provider_tool_manifest.py replacement, ModelRequest ownership
  persistence, and request copies used by preparation/routing/synthesis/resume/
  recovery. Complete tracing confirms instruction prefix/suffix additions,
  timed-goal reminder replacement and authoritative bare-base reconstruction.
- Design constraints: no delimiter-only ownership inference, no ownership in
  wire metadata, bounded persisted proof, exact surrounding text, unchanged
  canonical rendering, safe ambiguity/legacy handling. Root explicitly warned
  that existing revision/digest fields do not prove ownership.
- Implementation milestone: ModelRequest now persists a typed bounded range,
  owned-text SHA256 and full original instruction length/SHA256 outside metadata.
  Exact original-window proofs relocate ordinary prefix/suffix copies; delimiter
  shape alone never proves ownership. Goal reminder replacement uses a named base
  transform; context recovery explicitly resets proof for its authoritative base.
  Resume validates before activation/manifest mutation and derives the new raw
  base from proof. All request snapshots persist the field, including split parts;
  explicit provider payload assemblers and token estimates enumerate fields.
- Legacy trace: initial `model_request` capture already follows tool rendering;
  `base_instructions` and source_request are only ephemeral PreparedChat fields.
  Snapshot parts store that same rendered request, not an extra raw base. New
  proof-bearing snapshots, pre-feature requests, fresh turns in old sessions and
  old renderers proven to have emitted no text initialize without a pause.
  Proofless nonempty old tool output cannot be safely separated from arbitrary
  quoted authority; those requests retain their paused state and tool receipts
  and get an actionable chat conflict before activation. Do not infer or invent
  missing historic instruction bytes from current files or a digest.
- Completed checks: causal diagnostic, Ruff format/check and exact collection.
  Original 60 provider-context selectors from the parent-reviewed union plus 26
  new boundary cases collect as exactly 86. First bounded execution: 81 passed,
  1 optional PostgreSQL skipped, 4 newly authored fixture failures in 27.91s.
  Three regressions incorrectly persist inputs a second time after prepare_async
  has already saved the active turn; the initial-context-rejection fixture's
  health response has no exact model limits. Raw output is retained at
  `/tmp/nebula-tools-boundaries-tests.log`; parent and fresh validator received it.
  Fresh independent validator confirmed both setup mechanisms and corrections:
  remove only duplicate test persistence calls and supply the inert provider's
  exact model descriptor. The four corrected cases were separately collected
  and passed in 3.93s; raw output is `/tmp/nebula-tools-boundaries-retest.log`.
  Selected outcome is 85 passed and 1 optional PostgreSQL skipped, combining the
  first run and this four-case rerun. No product guard was relaxed. All original
  60 compatibility selectors passed or hit the existing optional PostgreSQL skip.
  The five whole-payload checks execute actual adapter assemblers twice with
  inert transports and prove exact equality between proof-bearing and proof-free
  requests, equal token estimates and bounded persisted state.
  Full selection and corrected-fixture rerun plans/receipts are
  `/tmp/nebula-tools-boundaries-plan.json`, `-plan-receipt.json`,
  `-retest-plan.json` and `-retest-plan-receipt.json` with the same prefix.
  Root owns shared selection. No broad tests, native/vendor changes, live
  services, root edits or CI dispatch.
- Independent legacy review confirms no separately recoverable durable raw base
  for proofless nonempty feature snapshots. It also rules out an unproven causal
  claim: resumed no-tool-history context recovery has source_request=None and
  refuses before reading an empty PreparedChat.base_instructions; tool-history
  recovery preserves existing instructions. Setting the proof-derived resume
  base restores an invariant, not a demonstrated empty-base delivery defect;
  the existing source_request safety guard remains unchanged.
- Independent final design review found strict full original-window proof
  sufficient for all remaining generic copies; no unique-fragment fallback or
  hypothetical older-revision migration is needed. Actual manifest changes save
  model_request and current MANIFEST_KEY together. Three committed sanitized
  captures in `provider-tools-instruction-boundaries-requests.json` use actual
  global Butler preparation and inert provider calls; paired, unmatched and hints
  bodies and their complete assembled authority are exact in all three requests.
- Integration boundary: core_tools_section is present in model_request snapshots,
  including zero-length ownership for empty renders. Large requests reside in
  ChatSnapshotPart.value and the turn references snapshot_parts.model_request;
  scanners must account for both inline and split locations. Root is adding
  provider.tools-context-ownership/v1 compatibility fencing so old delimiter-only
  readers cannot consume new proof-bearing turns. No shared compatibility files
  are changed here.
- Remaining: root review/integration, the ownership capability fence and final
  combined CI/public production-LAN acceptance. PostgreSQL was unavailable;
  installed vendor/GUI conformance is not established by these local checks.
