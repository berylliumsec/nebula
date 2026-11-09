# Standing-instruction continuity evidence

Managed native chats install one current standing-instruction channel, with an
exact durable turn snapshot and an accepted receipt scoped to the actual external
session and channel hash. Static saved context, assistant scope, planning guidance
and selected/goal skills no longer accumulate in retained user prompts. Goal skill
snapshots are deduplicated by path, with the goal's pinned snapshot authoritative.
Dynamic goal progress, selected attachments and working memory remain turn data.

Instruction updates and legacy contexts start a fresh native context with a saved
conversation handoff while retaining the Core chat and Core goal. Full current
instructions come from their own durable snapshot, independently of the handoff or
a lossy summary. Existing combined input limits, approvals and tool boundaries stay
in place. No migration or provider request suppression was introduced.

Stateless provider requests continue carrying full current instructions on every
request. Actual chat/Responses payload captures show one project instruction on
initial, subsequent and forced-compaction requests; all seven compactor inputs
exclude it. Anthropic, Gemini and Bedrock assembly was inspected and left unchanged.

Fresh independent causal reviews established retained native prompt accumulation,
instruction-body trimming before delivery, and the UTF-8 cap/hash mismatch plus
complete-U+FFFD loss. Raw synthetic baselines are linked from the checkpoint.
Exact-body fields now preserve whitespace, CRLF and Unicode without changing
metadata normalization or blank-skill refusal. The bounded project reader withholds
only an incomplete UTF-8 tail, preserves the malformed-interior replacement policy,
and hashes the exact delivered text.

Grok uses its documented creation-time `_meta.rules` channel only for managed
snapshots. Before any model prompt, a bounded no-symlink byte read must confirm the
exact channel once in the freshly written native system-head artifact for that
external session. Managed connections use their own cold local process, so a
supported unchanged load refreshes the artifact without appending instructions.
Ignored metadata, stale/missing artifacts and unsupported layouts fail visibly.
A rewritten system head on load causes deterministic fresh-context restoration.
Unmanaged ACP callers retain existing per-connection delivery tracking.

Protocol intent was checked against [grok-build source revision
2bdd1d6a](https://github.com/xai-org/grok-build/tree/2bdd1d6a6369de0e8c68132ea4539e9abd9e14a8):
the agent-mode guide, `acp_session_impl/spawn.rs`, `prompt_build.rs`,
`acp_session.rs`, `session/compaction.rs` and `xai-chat-state/compaction_utils.rs`.
The installed runtime is checked by its observed artifact, not an invented version
floor. Previously discarded whitespace in immutable saved skills is not recoverable
from a digest alone; this change does not invent it.

Validation: **74 selected tests passed in 18.24s**. The committed
[selection](../../.github/test-selection.json) records exact targets and count.
Strengthened unchanged restart/reconnect cwd checks separately passed 2/2 in 3.08s.
Ruff and `git diff --check` passed. Targeted mypy still reports the preexisting
`None not callable` at `_open_memory_managed`; that unchanged guard is outside this
repair.

The task branch was rebased onto integration base `698d82a0`; only the selection
file conflicted. Its 74-case selection remains bound to that exact base. The eight
relevant Kernel/storage/native-channel/Butler/persisted-goal integration cases
passed after rebase in 8.03s; unchanged source checks were not broadened.

The authenticated disposable execution-owned Core ran at
`http://172.26.91.238:37839` on the post-rebase check. Real HTTP exercised initial delivery, a subsequent turn
after deliberately lossy inert ACP compaction, a saved decision edit, and readback.
An unauthenticated completion request was refused with 401. The
[sanitized capture](standing-instructions-lan-capture.json) contains actual assembled
requests, harness events and source SHA-256 identities. Native/provider fixtures
were inert; gateway credentials are redacted. The listener, gateway and owned data
were stopped/removed after capture.

Public HostAPI-to-worker routing, visible Context controls, a candidate production
bundle, browser/device profiles and installed-vendor execution were not exercised
by this worker-LAN check. No physical device or deployment is claimed. Root owns
those remaining integration acceptance gates and the requested PR/merge.
