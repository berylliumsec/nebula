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

Each project has a stage:

- `off` (the default): nothing is curated.
- `shadow`: completed goals are curated into cards and links. Each turn
  records which cards it would have received, but nothing is added to any
  chat and no model sees the cards.
- `inject`: as `shadow`, and Core adds up to three matching cards to each
  turn in the project (see [Entry cards](#entry-cards)). Turning it on needs
  explicit confirmation.

A later release adds `recall` (tools agents call to search and follow cards).

Only goals that complete **after** a project is first enabled are curated.
Cancelled, paused and blocked goals, child goals, and chats without a goal are
never curated. Harness chats inside a goal are skipped in this release.

## Turn it on and inspect it

There is no Settings screen. Use the CLI against a running Core:

```bash
nebula-core linked-memory set --project <project-id> --stage shadow
```

```bash
nebula-core linked-memory set --project <project-id> --stage inject --confirm-inject
```

```bash
nebula-core linked-memory status --project <project-id>
```

```bash
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
  process (state, memory use, priority, restarts);
- entry cards: turns checked, cards injected or recorded in shadow, turns
  with no match, and the slowest turn-start check in milliseconds.

## Entry cards

In `shadow` and `inject`, every provider turn in the project checks its new
message against the project's cards:

- **Turn start reads memory only.** The project worker keeps an index of the
  project's active cards in memory. The check compares the new message's
  content words with each card, with no database, model or index call, and
  stops at 5 ms; past that the turn gets nothing. Each word counts once,
  whatever its form. Nebula's own words (goal, chat, turn, run, project and
  the like), the project's ID and words most of the project's cards share do
  not count. A card needs at least two remaining words in common with the new
  message, or one shared ID such as a goal's, so small talk such as "thanks,
  continue" or "how many goals are running?" gets nothing.
- **Prefetch helps the next turn.** After a turn's answer is saved, the
  curation helper ranks the cards against that exchange with MiniLM. On the
  chat's next turn, a card the previous exchange made relevant needs only one
  word in common. Prefetch is skipped, never delayed, when the host is busy
  or the helper is curating.
- **What the model sees.** At most three cards (title, summary, when it
  applies, kind and authority), about 300 tokens, are added after the
  operator's message as untrusted reference data that may be outdated, next
  to any project knowledge. The stored message never contains them.
  Quarantined cards are never added. A cloud provider receives cards only
  under the same rules as project knowledge: the provider must permit
  project data and the turn must allow cloud knowledge.
- **Audit.** Each turn's request snapshot has a `linked_memory` record: the
  stage, whether cards were injected, which cards with their scores, the
  reason when none were, and how long the check took.

## How cards are curated

Each goal is curated in chunks of about 40 messages, oldest first, with the
model the chat itself used. The prompt marks the transcript as untrusted data.
A proposed card is kept only if it quotes a passage that appears word for word
in the transcript. Its authority comes from who wrote that passage (the
operator, the agent or a tool), never from the model.

- Only the operator's own messages carry operator authority. Core's goal
  prompts, a goal's resume instructions, Butler recovery cases and a
  subagent's task count as agent text. Core's fixed goal prompts and its
  notes for failed or stopped turns are never evidence.
- A tool call that a project hook, approval or access rule denied is not
  evidence.
- A card is **quarantined** when its only evidence is output from an
  open-world tool, such as a web page, or when it pairs a refusal with a way
  around it, such as another tool, route or host. A quarantined card is
  stored for review but never added to a turn and never replaces another
  card.

A card may replace (`supersedes`) an older one only with equal or stronger
authority; otherwise the link is recorded as a proposed `contradicts`. When a
Core update tightens these rules, each project's saved cards are checked again
when its curation starts, and cards the rules now reject are quarantined.

## Live work goes first

Curation must not slow down anything an operator is waiting on:

- The project's execution worker only reads, calls the model and saves. Prompt
  building, verification and embedding run in a separate **curation helper**
  process at the lowest CPU priority and the idle I/O class, with a memory
  limit. The helper stops after two idle minutes.
- Before every chunk, curation pauses while an operator's turn in the
  project is running or ended in the last 30 seconds, host pressure is above
  50% (65% once a goal run is under way), or the project is handing off or
  shutting down. A paused run resumes from its saved position, and goals that
  complete meanwhile still get their runs.
- Goal, subagent, Butler and queued turns do not pause curation: in a busy
  project they can run for days. They share the limits below with it.
- A curation model call starts only while no operator turn is queued or
  running, a background provider slot is free, and the host is not busy. Only
  one curation call runs on the host at a time. A rate-limit error pauses the
  goal's run for five minutes.
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
| `NEBULA_LINKED_MEMORY_QUIET_SECONDS` | Quiet period after an operator's turn (default 30). |
| `NEBULA_LINKED_MEMORY_START_PRESSURE` | Host pressure below which a goal run may start (default 0.50). |
| `NEBULA_LINKED_MEMORY_PAUSE_PRESSURE` | Host pressure above which a running goal run pauses (default 0.65). |
| `NEBULA_LINKED_MEMORY_EMBEDDING_DIR` | Directory with the MiniLM `model.onnx` and `tokenizer.json`. Defaults to the copy Core's knowledge index downloads; without it, the helper ranks cards by shared words. |
