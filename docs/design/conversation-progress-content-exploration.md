# Interim assistant content: conversation mockups

Status: design exploration for review. This branch changes no application behavior. All names, elapsed times, counts, and child states in the mockups are illustrative.

## Why the current screen is still noisy

The screenshot's four process paragraphs are the in-progress assistant turn's `content` in Core's `chat_turns` record. The turn was in `waiting_callback` when inspected. `ChatTranscriptRow` renders `message.content` as full assistant Markdown before its compact `ActivityLedger`; the previous cleanup grouped structured commentary and activity, but it did not change this in-progress content path. The separate “Thinking…” disclosure adds another row above it.

This means the design decision is about **how to present a nonterminal assistant content stream**, not merely about spacing or font size. The source text must remain available, and a completed answer must appear in full.

## Alternatives

| Option | Default while running or waiting | Advantage | Cost |
| --- | --- | --- | --- |
| **A · Status only** | One state line and concise current-work label. Interim content and thinking live behind **View work**. | Strongest separation between progress and answer; one scan shows whether to wait or act. | A useful interim sentence takes one click to read. |
| **B · Latest update** | Same state line plus one explicitly labeled, short preview of the newest update. Earlier text and thinking live behind **View work**. | More context without opening work history. | The preview can still read like an answer, especially when a provider writes long narration. |

Recommendation: **A** for a nonterminal turn, with a state-specific label such as “Working” or “Waiting for delegated work.” Show requests for approval, input, failure, or retry outside the disclosure. When the turn completes, show the full final answer in the transcript and keep prior process text behind **View work**. If a failed or cancelled turn has partial content, label it as partial and keep it directly reachable.

### Figma and local previews

| View | Figma | Preview |
| --- | --- | --- |
| A · Subagent, status only | [Figma frame](https://www.figma.com/design/3UzfLAS0vL7gZKBvAesXVF?node-id=12-2) | ![Status only](conversation-progress-mockups/status-only-desktop.png) |
| B · Subagent, latest update | [Figma frame](https://www.figma.com/design/3UzfLAS0vL7gZKBvAesXVF?node-id=12-32) | ![Latest update](conversation-progress-mockups/latest-update-desktop.png) |
| A · Parent, child state and action | [Figma frame](https://www.figma.com/design/3UzfLAS0vL7gZKBvAesXVF?node-id=12-62) | ![Parent status](conversation-progress-mockups/parent-status-desktop.png) |

The frames reuse the existing Nebula mockup styling and Geist typography. Figma has no Nebula component instances, local variables, or Code Connect mappings in this file, so these are visual explorations rather than a component specification.

## Operator contract for a later implementation

- **Entry and authority:** Opening a parent or subagent conversation selects the URL route and reads the authoritative turn, transcript, activity, child state, and pending requests from Core. React owns only disclosure state.
- **Stream and wait:** A nonterminal turn has one visible status. Its interim content remains readable in order under **View work**. A pending callback or child wait must not look like a finished answer.
- **Action:** An approval, question, failure, or retry remains visible and actionable in the transcript. The parent shows a child request only when the decision belongs to the operator; the action opens the authoritative child interaction.
- **Complete:** The final answer is full-size content. Earlier process updates remain available without preceding or visually outweighing the answer.
- **Replay:** Refresh, reconnect, and historical replay reconstruct the same state and work history without duplicate paragraphs or lost actions.
- **Responsive:** The status, disclosure, action, and composer remain reachable by keyboard and touch at 320–430 px. A selected direction still needs a mobile mockup and real workflow validation before implementation can be called complete.

## Questions for review

1. Is the status-only version calm enough, or do you want the single latest-update preview?
2. Should “Thinking” appear as a separate disclosure, or only inside **View work** during nonterminal turns?
3. For a waiting turn, does “Waiting for delegated work” convey enough, or should the specific child or callback source be named when Core has it?
