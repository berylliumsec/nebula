# Browser harness primitives

Nebula removes Intruder, built-in payload sources and generation, guided browser
assessment profiles, and candidate-specific validation grants. Browser navigation,
interaction, interception, Repeater, crawling, traffic inspection, HAR utilities,
and evidence capture remain available to operators and generic agent automation.

Removed API families: `browser-attacks`, `browser-assessments`, and
`browser-issue-candidates`. Their stored types and discovery entries are retired.
There are no compatibility aliases or archived workflow views.

Risk values change from `active_scan` to `network_action`, `exploitation` to
`high_impact_action`, `persistence` to `persistent_change`, and `destructive` to
`destructive_change`. Update callers and saved policies before using this build;
old values are rejected. Approval and scope semantics remain unchanged.
The browser adapter contract is version 2 and uses `automation_id` with
`/v1/automation/{automation_id}/{action}` for generic pause, resume, and stop.

Retained browser validation also corrected asynchronous evidence reads, live
proxy authorization, and cancellation/revocation races exposed by focused tests.
