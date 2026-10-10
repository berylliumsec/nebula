# Focused test selection for agents

Never run a full test suite merely to be safe, finish a PR, cut a release, retry
a failure, or satisfy the product-quality matrix. Default to the smallest set
of feature-specific tests that proves the changed journeys and integrations.
The browser/device matrix applies to those selected journeys, not every test.

Before execution, state the changed features, exact test files/filters/projects,
expected test count (use collection/listing), runtime boundaries, and exclusions.
Record the selection and rationale in `.github/test-selection.json`, bound to
the current diff using `scripts/test_selection.py`. Review the uploaded receipt.
Missing baseline, shared files, or uncertain impact means stop and review the
selection; it NEVER authorizes automatic full coverage. Diagnose a failing
selected test without expanding to unrelated suites.

Full suites should almost never run. They require a fresh, explicit user request
approving that full run and a recorded reason. General requests such as “test it,”
“make sure it works,” “commit/PR/merge,” and “release” are not full-suite approval.
Do not set approval flags yourself, enumerate every project to evade the gate,
bypass guarded npm commands, or dispatch a broad run as a shortcut. Check workflow
triggers before pushing so CI cannot accidentally broaden the chosen coverage.
See [the test selection guide](../../docs/TEST_SELECTION.md) for the focused
workflow and rare full-run procedure.
