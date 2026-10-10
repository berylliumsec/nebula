# Harness subagents acceptance contract

Entry: Assistant conversation settings → Subagents → choose a configured harness,
then ask the assistant to delegate work. Core owns the selected child runtime,
the child conversation and turn, lineage, reports, and stop state. The URL owns
the selected conversation; React owns only the open settings and inspector.

| Journey step | Observable invariant | Authority | Test layer |
| --- | --- | --- | --- |
| Discover | Enabled harnesses and their advertised models are selectable by name | Core catalog, UI | component, browser |
| Save | The chosen harness remains selected after refresh and reconnect | Core chat metadata | real Core |
| Start | Delegation creates one linked child on the selected harness; duplicate calls reuse it | Core subagent and harness turns | Python, real Core |
| Use | Child output and status appear in its conversation and the parent's Subagents pane | Core turns and transcript | browser, real Core |
| Stop | Stopping a child cancels its harness turn and reports the result | Core | Python, real Core |
| Retry | Messaging a finished child starts another turn in the same child conversation | Core | Python, real Core |
| Failure | Missing or disabled harnesses yield a visible error without orphan conversations | Core | Python, browser |
| Reconnect | Child and parent status and selection survive a reload without duplicate work | Core | real Core |
| Delete | Removing a conversation clears its linked child records and sessions | Core | existing deletion contract |

Planned layers: focused Core lifecycle tests, component settings tests, selected
desktop/mobile Chromium and mobile WebKit browser journeys, production bundle,
real-Core workflow, and LAN-origin check. A physical device is unavailable in
this workspace and must remain explicitly unverified.
