# Retained-reader compatibility for exact instruction ownership

Fresh independent validation pinned the delimiter-only reader to `55f5d414`.
That reader accepts a new request snapshot, discards `core_tools_section`, and
then removes quoted Butler authority on reassembly. Its existing
`provider.tools-context/v1` capability therefore cannot authorize consumption of
the new proof-bearing shape. Empty owned ranges need the same protection.

Canonical releases now advertise `provider.tools-context-ownership/v1`. The
snapshot splitter validates the typed proof and retains the bounded integer
`provider_tools_ownership_version` beside the turn's split references. Repeated
splitting preserves it. Central execution admission additionally recognizes
unprojected inline proofs and exact same-project/session referenced parts, so
previous proof-bearing snapshots cannot bypass the requirement. Historical
manifest knowledge continues to require the original tools capability; a plain
pre-feature request acquires no ownership requirement merely by existing.

Claims, handoff transfer revalidation and expired-owner recovery use the existing
central semantic guard. Incompatible readers leave the current owner and fence
unchanged. Worker startup accepts the implemented capability and checks the
provider manifest schema without applying migrations. The retained acceptance
bundle copies the same declared capability. No SQL migration or provider wire
field is added.

The focused selection collected and passed **58 cases in 51.82 seconds**, with
zero skips: 27 projection/admission cases, 18 existing semantic context cases,
12 actual worker startup/schema/refusal cases and one actual exact-authority
snapshot/restart/compaction regression. SQLite and isolated PostgreSQL schemas
both ran. The admission matrix covers claim, transfer and recovery for inline,
split, unmarked-inline and unmarked-split snapshots. It verifies the older
reader already advertises the original tools capability and that an unrelated
project remains unaffected.

The local diff-bound plan and receipt are
`/tmp/nebula-tools-proof-compatibility-selection.json` and
`/tmp/nebula-tools-proof-compatibility-selection-receipt.json`; raw output and
JUnit are `/tmp/nebula-tools-proof-compatibility-tests.log` and `.xml`.
Ruff and formatting pass. Final combined source/browser/native acceptance remains
separate; this receipt certifies the compatibility boundary only.
