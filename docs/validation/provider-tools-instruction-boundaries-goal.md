# Preserve standing authority when rendering provider tools

Continue the complete [standing-instruction goal](standing-instructions-goal.md):
each active instruction version appears once in effective context, survives
compaction/reconnect/restart/goals, edits supersede predictably, failed/cancelled
delivery and concurrency cannot lose authority, and stateless requests retain
their required full instructions. Keep the causal checkpoint durable and reload
it with this goal after compaction or resumption.

This repair addresses the independently confirmed raw global BUTLER.md path.
Only replace a Core-owned managed tool section using durable request provenance;
never strip arbitrary saved instruction text because it resembles delimiters.
Preserve the exact original prefix and genuine appended instruction text. Keep
one actual canonical managed section, byte-identical on unchanged rendering;
updates replace the owned section and hints. Support JSON snapshot round trips,
restart, compaction, preparation, routing, synthesis, resume and recovery.

Use bounded internal provenance/hash/range state. ModelRequest.metadata is sent
to OpenAI Responses verbatim, so do not place base instructions or large ownership
data there. Revision/digest metadata alone is not ownership: an operator may
quote an exact valid rendered section. Account safely for snapshots created by
the previous implementation without deleting arbitrary text or inventing lost
historical content. Do not change JSON escaping, native harness delivery, or
required per-request provider instructions.

Validate real `_global_instructions` plus `ChatService.prepare_async`, paired
and unmatched literal section/hints markers, subsequent application and updates,
genuine instruction prefix/suffix changes, JSON/restart/resume/compaction, and
provider wire exclusion. Use the existing 60 relevant provider-context cases
plus narrow new tests with a reviewed diff-bound local plan; no full suite, live
providers, broad builds, shared selection or CI edits.

Worktree `/workspace/nebula-tools-boundaries`, branch
`codex/provider-tools-instruction-boundaries`, baseline `b924f9e9`. Root owns final
review, integration, final shared selection and CI. Main ownership is
`provider_tool_manifest.py`, focused tests and this validation checkpoint; a
minimal internal ModelRequest field may require a model-only providers.py hunk.

| Journey | Observable invariant | Authority and evidence |
| --- | --- | --- |
| Periodic Butler preparation | Exact saved raw mandate remains in actual requests, even when it quotes tool markers | BUTLER.md bytes, real loader/global instructions and ChatService preparation |
| Tool selection and updates | One Core-owned section changes without deleting quoted authority or appended instructions | Durable request proof and versioned tool manifest |
| Resume and compaction | Proof and exact authority survive JSON, split snapshots, restart and lossy history summaries | Core snapshot parts and real inert-provider lifecycle |
| Failure or legacy ambiguity | No provider request, recovery activation or tool receipt mutation before proof validation | Durable paused turn and explicit conflict |
| Vendor delivery | Internal proof does not alter any provider payload or token estimate | Five actual adapter assemblers with inert transport captures |

This repair adds no operator control, setting, source catalog entry, native
harness protocol or browser layout. Root owns combined public production-LAN
acceptance and required CI; local contract checks alone do not establish those
gates or installed vendor conformance.
