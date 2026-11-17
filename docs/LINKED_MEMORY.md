# Linked memory

Linked memory is a background service in Core. When a top-level goal
completes, it reads the goal's chat and child chats and curates **memory
cards**: short, sourced records of decisions, facts, outcomes, constraints,
lessons and open questions. Cards are joined by typed **links** such as
`supersedes`, `updates`, `contradicts` and `depends_on`, so a later agent can
follow how the work changed. The design is in
[design/linked-memory.md](design/linked-memory.md).

"Linked memory" is not RAM or a memory budget, and it is separate from Butler's
Memory panel.

## Stages

Each project has a stage. This release has two:

- `off` (the default): nothing is curated.
- `shadow`: completed goals are curated into cards and links. Nothing is
  injected into any chat, and no agent or model sees the cards.

Later releases add `recall` (agents can search and follow cards) and `inject`
(Core adds a few relevant cards to a turn).

Only goals that complete **after** a project is first enabled are curated.
Cancelled, paused and blocked goals, child goals, and chats without a goal are
never curated. Harness chats inside a goal are skipped in this release.

## Turn it on and inspect it

There is no Settings screen. Use the CLI against a running Core:

```bash
nebula-core linked-memory set --project <project-id> --stage shadow
nebula-core linked-memory status --project <project-id>
nebula-core linked-memory cards --project <project-id> --limit 20
```

`set` reads the current revision, saves the new stage with that revision, and
reads it back. Or ask Butler in the project: it uses the
`nebula.settings.linked_memory` and `nebula.settings.set_linked_memory` actions
and reports Core's read-back.

`status` reports:

- the stage, its revision and whether the kill switch is on;
- `backlog`: completed goals still waiting for curation;
- card and link counts, including quarantined cards;
- model spend in tokens and estimated cost;
- recent goal runs with their state (`pending`, `running`, `paused`, `done`,
  `capped`), the reason for a pause, and the last error;
- the curator (`idle`, `running` or `paused` and why) and the curation helper
  process (state, memory use, priority, restarts).

## How cards are curated

Each goal is curated in chunks of about 40 messages, oldest first, with the
model the chat itself used. The prompt marks the transcript as untrusted data.
A proposed card is kept only if it quotes a passage that appears word for word
in the transcript. Its authority comes from who wrote that passage (the
operator, the agent or a tool), never from the model. A card whose only
evidence is output from an open-world tool, such as a web page, is
**quarantined**: it is stored but can never be used to replace another card.
A card may replace (`supersedes`) an older one only with equal or stronger
authority; otherwise the link is recorded as a proposed `contradicts`.

## Live work goes first

Curation must not slow down anything an operator is waiting on:

- The project's execution worker only reads, calls the model and saves. Prompt
  building, verification and embedding run in a separate **curation helper**
  process at the lowest CPU priority and the idle I/O class, with a memory
  limit. The helper stops after two idle minutes.
- Before every chunk, curation pauses while the project had a live turn in
  the last 30 seconds, host pressure is above 50% (65% once a goal run is
  under way), or the project is handing off or shutting down. A paused run
  resumes from its saved position.
- A curation model call starts only while no live turn is queued or running
  for the provider, and only one curation call runs on the host at a time. A
  rate-limit error pauses the goal's run for five minutes.
- Each goal run is capped at 200 chunks and 400,000 tokens.

## Diagnostics

Linked memory records its diagnostics under the `knowledge` feature with
`linked_memory.*` event codes, for example `linked_memory.goal_run_created`,
`linked_memory.chunk_curated` (with card, link, token and timing counts) and
`linked_memory.goal_run_done`.

## Configuration

These environment variables are read when Core starts:

| Variable | Meaning |
| --- | --- |
| `NEBULA_LINKED_MEMORY_DISABLED=1` | Kill switch: turns linked memory off for every project. |
| `NEBULA_LINKED_MEMORY_QUIET_SECONDS` | Quiet period after a live turn (default 30). |
| `NEBULA_LINKED_MEMORY_START_PRESSURE` | Host pressure below which a goal run may start (default 0.50). |
| `NEBULA_LINKED_MEMORY_PAUSE_PRESSURE` | Host pressure above which a running goal run pauses (default 0.65). |
| `NEBULA_LINKED_MEMORY_EMBEDDING_DIR` | Directory with the MiniLM `model.onnx` and `tokenizer.json`. Defaults to the copy Core's knowledge index downloads; without it, the helper ranks cards by shared words. |
