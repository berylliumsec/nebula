# Chat navigation performance

## Operator contract

| Journey | Visible invariant | Authority | Focused check |
| --- | --- | --- | --- |
| Select a conversation | The latest saved messages appear first; earlier messages remain reachable. | URL identity and Core transcript | Long-history browser journey and paged API test |
| Open a side chat | It inherits the saved boundary without another parent-history request; only explicit Close discards it. | Core fork, URL `sideChat`, local draft | Side-chat browser matrix and real-Core reload/reply journey |
| Read long work | Mounted transcript rows stay bounded; collapsed reasoning is fetched when opened and can be retried. | Core message and reasoning route | Long-history browser and disclosure component tests |
| Navigate and stream | A saved reading position survives a switch; the active answer stays reachable while it grows. | Device preview plus Core reconciliation | Conversation-switch and harness-scroll browser journeys |

The side pane still uses an embedded `SessionsPage` for the full composer and approval workflow. It now reads only its own session rather than duplicating the project conversation catalog and sidebar polling. Extracting its chat controller into a dedicated pane remains an architectural follow-up.

## Measurements on 2026-10-02

A read-only SQLite query against the deployed Core database found a 454-message conversation with 22.06 MiB of stored message payload, including 11.61 MiB of reasoning. Its newest 61 records contain 2.97 MiB of raw payload, or 1.21 MiB after removing reasoning. These are database JSON sizes, not measured HTTP transfer sizes. The deployed Core is still the prior build; this branch was not deployed for that measurement.

The production UI build, exercised with a mocked 150-message conversation, mounted 6–8 transcript rows across desktop Chromium and 320/390/430 px Chromium and WebKit profiles. A 61-record initial request and one older-page request were observed. Chrome's existing `nebula.chat_switch.authoritative` mark ranged from 122 to 432 ms across the eight profiles in one four-worker run. Mock request timing and host contention make that range diagnostic rather than a live latency target. The focused Playwright test emits `CHAT_PROFILE` and attaches `chat-history-profile.json` with switch, row, DOM-node, and resource timings.

A synthetic SQLite fork of 454 messages with roughly 22 MiB of source text and reasoning took about 0.69 s after batched insertion. The prior per-message copy stage took about 1.30 s versus 0.77 s with one transaction; the complete fork took about 0.85 s with that transaction but per-row flushes. These are local synthetic runs, not production percentile measurements.

## Remaining latency to investigate

1. Profile a real-Core large-conversation switch in Chrome with the production bundle. Use `nebula.chat_switch.*`, `Server-Timing`, resource timing, and a Performance trace to separate Core reads, transfer, React work, and layout.
2. The side chat still mounts a second page controller. Extracting the chat-specific state and UI would remove duplicated hooks and catalog work beyond the requests already suppressed here.
3. The side-chat fork still materializes inherited records. The synthetic fork is under a second, but a large real history and a busy database need a measured latency distribution before choosing a more complex snapshot design.
4. Full-history export intentionally reads all messages. Other normal selection, completion, and reconciliation paths now request recent pages; inspect future callers before adding another unbounded read.

A database migration or GPU is not justified by the current evidence: the indexed SQLite lookup was already fast in the initial investigation, while payload size, UI row count, and fork writes were concrete costs. Revisit storage only if the real-Core trace shows sustained database time after these changes.
