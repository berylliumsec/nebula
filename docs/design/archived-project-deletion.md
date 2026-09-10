# Archived project deletion

Approved request: archived projects should be deletable; linked host folders must survive.

| Journey | Invariant | Authority | Verification |
| --- | --- | --- | --- |
| Discover | Archived projects offer Restore and Delete; active projects retain Archive | Core list / switcher | component + production browser |
| Confirm/cancel | Destructive confirmation explains deletion, folder preservation and retained audit records; cancel changes nothing | dialog state | browser |
| Delete | Archived-state and revision checks plus active-work guards precede one atomic deletion of project-owned mutable data | Core transaction / runtime locks | backend |
| Refresh/reconnect | Deleted project disappears from refreshed lists, including after reload; unrelated selection survives | Core / URL / local selection | real Core browser |
| Failure/retry | Active work or stale revision explains recovery; failed operation leaves data intact | Core | backend + browser |
| Files | No filesystem removal, mount cleanup, chmod or chown; shared artifact blobs and immutable event audit ledgers retained | host filesystem / audit stores | backend + real Core |

Remove owned entities, graph/schema/history, search projections, resource relations,
terminal history/preferences and run budget counters. Retain immutable run/operation
event ledgers as existing conversation/mission deletion does, along with physical
artifact blobs and workspace files. This is project removal, not secure erasure.
Block unfinished missions, chat/harness turns, executions, browser commands and
claimed/queued follow-ups. Terminal admission uses the existing workspace guard.
No provider operation, research command, streaming or fork is started by deletion.

Focused verification: new archived-deletion backend tests plus existing empty/owned
project deletion compatibility tests; API client regression; committed real-Core
production browser journey on LAN at desktop 1440/1024, mobile Chromium and WebKit
320/390/430, including confirmation/cancel, reload, accessibility and containment.
Physical devices are unavailable. No live user project will be deleted.

## Verification — September 9, 2026

- Focused Python: 8 passed (new archived deletion suite plus existing nonempty/empty
  project deletion compatibility checks).
- API client: 6 passed, including stale restore and retry after lost delete response.
- Production build: passed; existing large-chunk advisory remains.
- Committed real-Core journey: LAN `http://192.168.1.155:19438`, desktop Chromium
  1440/1024; emulated Android Chromium and iPhone WebKit at 320/390/430. Checks
  cancellation, in-place conflict/retry, actual deletion and reload, folder reuse,
  no horizontal overflow and Axe on the settled confirmation. Conflict presentation
  uses an intercepted response; backend busy rejection is tested against Core.
- Browser artifacts: `/tmp/nebula-project-deletion-browser-verified`; build log:
  `/tmp/nebula-project-deletion-build.log`. Physical devices were not available.
- An initial contrast check sampled the opening animation; the committed test waits
  for full opacity and zero blur before accessibility analysis.

## Follow-up: unused or stale harness sessions

Observed September 9: an archive with only terminal harness turns was blocked by
session envelopes left in `starting` since August 23. Sessions can be created before
any turn exists; their status is not the execution authority. Deletion must use
harness turn status (queued/running/waiting approval still block), not session status.
The existing archive/revision, chat, mission, execution and queue guards remain.
Verify unused starting sessions and stale running/waiting session envelopes delete;
an actual queued/running/approval turn must continue to block without partial cleanup.
Use disposable fixture sessions in the existing real-Core browser deletion journey.
No live user archive is deleted as part of verification; retry remains user-controlled.

Follow-up evidence: 14 focused backend tests passed, including three stale-session
states and three genuine unfinished-turn states. All 8 production LAN browser
profiles passed with an unused starting session seeded before deletion (52.2 s,
`/tmp/nebula-stale-session-browser-final`). Production build and Ruff passed.
An isolated SQLite copy of the affected archived project's actual records was
successfully deleted with the corrected guard. Its live records and folder were
not changed by that rehearsal. Physical devices remain unavailable.
