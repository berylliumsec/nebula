# Butler Memory UI contract

Butler saves useful preferences and lessons automatically. The operator opens
Memory from the Butler panel only when they want to review or manage it. Opening
the section is the first read; saving never interrupts a chat or Voice turn.

Butler decides what is worth keeping during its model turn, including Voice
requests delegated to the saved Butler conversation. An explicit request to
remember an item should call the memory capability and report the saved record;
ordinary turns should produce a memory only when the model judges the item
useful beyond the immediate exchange. Core checks an exact quote against the
current or recent operator messages, rejects unsafe content, persists the item
in Butler's SQLite database, and returns its ID. Chroma is a rebuildable search
index. The next turn and a reopened Memory panel read Core's saved record.
If the save fails, Butler must report that failure and must not imply it kept
the item in long-term memory. Project lessons stay in the selected project;
global preferences and transferable lessons can be recalled elsewhere.

| Journey | Observable invariant | State authority | Test layer |
| --- | --- | --- | --- |
| Open Memory | No request occurs before the operator opens it; a compact list and search appear on demand | React owns open state; Core owns items | component, production browser |
| Remember from text or Voice | Explicit requests yield a saved ID and later recall; model-selected useful items save without a prompt | Butler model selects; Core validates quote and owns persistence | capability integration, real Core |
| Butler turn completes while Memory is open | The list refreshes from Core and shows any new record without closing the panel | Core database; React only triggers the read | component, real Core |
| Find a memory | Search uses the selected project and displays scope, source, and last update; fallback search quality is labelled | selected project and Core search/index status | component, real Core |
| Review a result | Full content and source reference, when visible, expand without crowding the Butler chat | Core scoped read; React expansion | component, mobile browser |
| Edit or delete | A saved revision is sent, destructive deletion requires an explicit second click, and the list reloads from Core | Core revision and database | component, real Core |
| Switch project, reconnect, or retry | Prior project's rows disappear; a reopened section reads the new project; failed reads have Retry | selected project, Core connection, Core read | component, real Core, production browser |

The component is mounted in the Butler panel. It does not create memories or
prompt before Butler's automatic saves, and it does not store memory in the
browser. Core redacts source references for global lessons outside their source
project; this view only displays references Core returns. Production LAN browser
cases exercise the mounted view against real Core instances.
