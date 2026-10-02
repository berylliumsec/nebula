# Chat navigation performance

## Operator contract

| Journey | Visible invariant | Authority | Focused check |
| --- | --- | --- | --- |
| Select a conversation | The latest saved messages appear first; earlier messages remain reachable. | URL identity and Core transcript | Long-history browser journey and paged API test |
| Open a side chat | It inherits the saved boundary without another parent-history request; only explicit Close discards it. | Core fork, URL `sideChat`, local draft | Side-chat browser matrix and real-Core reload/reply journey |
| Read long work | Mounted transcript rows stay bounded; collapsed reasoning is fetched when opened and can be retried. | Core message and reasoning route | Long-history browser and disclosure component tests |
| Navigate and stream | A saved reading position survives a switch; the active answer stays reachable while it grows. | Device preview plus Core reconciliation | Conversation-switch and harness-scroll browser journeys |

The side pane retains the full composer and approval workflow. It reads only its own session rather than duplicating the project conversation catalog and sidebar polling. The embedded page is memoized so parent typing and stream updates do not render its transcript; it also skips the parent workbench's MCP and hook catalogs, tool-model verification, and assistant draft snapshot. Extracting the shared chat controller from `SessionsPage` would remove the remaining dormant page hooks.

## Measurements on 2026-10-02

A read-only SQLite query against the deployed Core database found a 454-message conversation with 22.06 MiB of stored message payload, including 11.61 MiB of reasoning. Its newest 61 records contain 2.97 MiB of raw payload, or 1.21 MiB after removing reasoning. These are database JSON sizes, not measured HTTP transfer sizes. The deployed Core is still the prior build; this branch was not deployed for that measurement.

The production UI build, exercised with a mocked 150-message conversation, mounted 6–8 transcript rows across desktop Chromium and 320/390/430 px Chromium and WebKit profiles. A 61-record initial request and one older-page request were observed. Chrome's existing `nebula.chat_switch.authoritative` mark ranged from 122 to 432 ms across the eight profiles in one four-worker run. Mock request timing and host contention make that range diagnostic rather than a live latency target. The focused Playwright test emits `CHAT_PROFILE` and attaches `chat-history-profile.json` with switch, row, DOM-node, and resource timings.

The side-chat open path now emits `nebula.side_chat.open` from the toolbar action to a ready saved transcript. One real-Core production-bundle LAN run with a two-message parent measured 350 ms in desktop Chromium at `http://192.168.1.155:33413`. That single small-conversation sample does not establish large-history or percentile latency. The real-Core test emits `SIDE_CHAT_PROFILE` and attaches the timing for later comparisons.

An isolated real-Core test with 454 messages and about 15 MiB of varied, wrap-safe stored text and reasoning measured a 360 ms authoritative switch in desktop Chromium. Three side-chat opens took 511–900 ms; the fork request took 197–593 ms and the side transcript page 87–112 ms. The side pane mounted zero inherited rows while collapsed. These are diagnostic local runs, not percentiles. A separate pathological fixture with a 19,000-character unbroken token triggered a 2.7-second browser layout task, so long unbroken content needs its own UI treatment if it occurs in operator transcripts.

A synthetic SQLite fork of 454 messages with roughly 22 MiB of source text and reasoning took about 0.69 s after batched insertion. The prior per-message copy stage took about 1.30 s versus 0.77 s with one transaction; the complete fork took about 0.85 s with that transaction but per-row flushes. These are local synthetic runs, not production percentile measurements.

## Remaining latency to investigate

1. The side chat still mounts a second page controller. Extracting shared chat state from workbench-only state would remove dormant hooks and further reduce its bundle and mount work; retain attachments, edits, queue, approvals, harness interaction, and reconnect behavior when doing so.
2. The side-chat fork still materializes inherited records. The isolated real-Core fork was below a second, but a busy database needs a latency distribution before choosing a more complex snapshot design.
3. Profile real operator transcripts with long unbroken strings and repair their layout cost without changing exact text or copy behavior.
4. Full-history export intentionally reads all messages. Other normal selection, completion, and reconciliation paths now request recent pages; inspect future callers before adding another unbounded read.

A database migration or GPU is not justified by the current evidence: the indexed SQLite lookup was already fast in the initial investigation, while payload size, UI row count, and fork writes were concrete costs. Revisit storage only if the real-Core trace shows sustained database time after these changes.
