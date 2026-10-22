# Workspace provenance for native hooks

Nebula adds a time-bounded `workspace_provenance` receipt to project-native hook
events. The contract lets a hook distinguish dirty paths changed by the current
agent from pre-existing, other-agent, unknown, or concurrent state without
putting metadata into user files.

The receipt schema is `nebula.workspace-provenance/v1`. Its stable fields are:

- `observation_id`, `scope_kind`, and `scope_id` identify the durable Core
  observation.
- `actor_id` is `chat:<session-id>` for an ordinary chat,
  `subagent:<subagent-id>` for a Core-managed child, or the normalized runtime
  owner for another surface.
- `supported` and `unsupported_reason` state whether attribution was possible.
- `confidence` is `exact`, `uncertain_parallel_turns`, or `unsupported`.
- `mutations` contains changed dirty-path fingerprints, never file contents.
- `attribution` partitions the current dirty paths into `owned`, `other`,
  `preexisting`, `unknown`, and `uncertain`.

The top-level hook envelope also contains the normalized `actor`. Existing v1
hooks remain compatible because both fields are additive.

## Storage and observation boundary

The authoritative observation is stored in Nebula Core's existing database as
a `workspace_provenance_observations` entity. Nebula does not create a metadata
file inside the workspace and does not require a repository-specific directory.
Only Core's hook runtime writes observations: the API exposes no route that
creates, edits or deletes one, so a client cannot forge or rewrite a receipt.

For Git workspaces, Core asks Git for its dirty-path list and fingerprints only
those paths. It does not walk the workspace. Non-Git workspaces and snapshots
that exceed the 10-second wall-clock budget return an unsupported receipt
rather than a false or partial attribution. There is no fixed path-count or
dirty-byte limit. The `unsupported_reason` reports
`snapshot_time_limit_exceeded:10s` when the budget expires.

Before/after observations are recorded around selected chat lifecycle hooks and
automatically discovered `tool.before`/`tool.after` hooks. A chat turn records
its observation only when a selected hook subscribes to a `chat.turn.*` event;
tool hooks keep their own per-call observation. Core takes snapshots off its
event loop, so a slow checkout delays only the turn or tool call it belongs to.
If another actor's observation overlaps, matching mutations are marked
uncertain. Hooks must not treat uncertain, unknown, or unsupported paths as
owned by the current actor.

## Policy boundary

The receipt is evidence for local hook policy, not permission to modify, stage,
discard, commit, or delete a path. A repository may use actor-scoped attribution
for per-agent Stop behavior while retaining a separate global cleanliness gate
for merge, release, or deployment.
