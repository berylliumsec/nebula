# Local test selection for agents

Nebula has no CI test workflows and no committed test-selection receipt. Tests run
locally at the discretion of the agent or human who makes the change.

Choose the tests that prove the changed journeys and integrations, and prefer focused
runs over full suites. A general request such as "test it", "commit/PR/merge" or
"release" does not by itself call for a full suite. Diagnose a failing test rather
than widening the run to unrelated suites.

Before the merge, report in the PR description what you ran, the outcomes, and what
you chose not to run. Never present an untested workflow as verified. See
[the local test guide](../../docs/TEST_SELECTION.md) for commands, fixture journeys and
the local full-run guard.
