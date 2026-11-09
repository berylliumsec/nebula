# Standing-instruction removal CI checkpoint

Reload the [complete durable goal](standing-instructions-goal.md), the original
[standing-instruction checkpoint](standing-instructions-checkpoint.md), and this
checkpoint after compaction or resumption. Preserve the full goal and all its
acceptance criteria. This follow-up owns one original failed Python case only.

## Scope and contract

- Worktree: `/workspace/nebula-ci-standing-remove`; branch:
  `codex/ci-standing-remove`; base: `c4fb0ff56e20da6227ebc4f42f61e9db7701f89b`.
- Exact case:
  `tests/v3/test_standing_instruction_continuity.py::test_edit_or_remove_replaces_old_native_instruction_authority[True]`.
- Original CI build: `eee508bc9c1c493cc019da95ca5feb398869ebfa`; run
  `42666867652`; job `129332934530`. Raw log:
  `/tmp/nebula-ci-eee508bc-python.log`, lines 1923–1947.
- Removing a saved constraint must create a fresh native context, preserve the
  same Core conversation, omit the removed rule, and leave the remaining full
  current instruction snapshot authoritative. Core owns saved decisions and
  exact turn snapshots; the native session owns its accepted system head.
- Only the exact failed `[True]` case may execute. The `[False]` variant, the
  full file, passing contracts, browser/native suites, broad CI, live providers,
  deployments and pushes are excluded. Root owns shared selection and publishing.
  No product, adapter, fixture, timing, native freshness, channel receipt,
  stateless delivery, retry or acknowledgement behavior changes.

## Evidence and independent gate

1. The original assertion reports only `any(error) == True` after the second
   send. The first send's events are discarded, so the trace does not establish
   that initial installation succeeded. The structured error's message, reason
   code and diagnostic identity are absent from the failure output.
2. Relevant source is unchanged between the failed CI build and this base:
   `harnesses.py`, `harness_instructions.py`, the owned test and asynchronous
   fixture cleanup have no intervening diff.
3. A temporary inert reproduction exercised only the assigned removal scenario
   through the actual harness runtime and adapter, with an observational wrapper
   preserving the production readback's return value. Both sends succeeded;
   `synthetic-native-1` rolled to `synthetic-native-2`. Both system heads exactly
   matched the submitted channel once, with correct hashes and fresh artifact
   mtimes. Removal retained the nonempty default assistant-scope snapshot.
   Script: `/tmp/nebula-standing-remove-diagnostic.py`; raw result:
   `/tmp/nebula-standing-remove-diagnostic.json`. The owned temporary resources
   were closed and removed.
4. An empty post-removal channel and base-source drift are ruled out. Wrong
   account paths, duplicate text and corrupt heads were not observed locally.
   Separate fresh-write observations on tmpfs and overlayfs all had positive
   mtime/open deltas; they do not establish CI filesystem behavior. Timestamp,
   OS, database and first-send failures remain unresolved alternatives for CI.
5. Root's fresh-context `validate_standing_remove_failure` independently traced
   the original raw failure, source and observational reproduction. It confirmed
   the diagnostic-loss mechanism and supported only materializing structured
   errors from both sends and asserting initial success before reading the
   native identity. The behavior-changing repair gate did **not** pass: the
   original runtime cause remains unknown. No behavior-changing repair was made.

The owned test now asserts both successful sends and includes complete structured
error events on failure. Existing native rollover and authority assertions remain.
No assertions are skipped or weakened.

## Focused verification

- Python 3.12.14; collection: **1 test collected in 2.04 seconds**. Execution:
  **1 passed in 2.73 seconds**, zero failures/skips/retries. Runtime warnings and
  pytest unraisable-exception warnings are errors; the process has a 60-second
  bound. Only the exact failed `[True]` node executed, once.
- Command, from this worktree:
  `PYTHONPATH=src:. timeout 60s /workspace/nebula/.venv/bin/python -m pytest -q
  -W error::RuntimeWarning -W error::pytest.PytestUnraisableExceptionWarning
  'tests/v3/test_standing_instruction_continuity.py::test_edit_or_remove_replaces_old_native_instruction_authority[True]'
  --junitxml=/tmp/nebula-standing-remove-junit.xml`.
- Collection: `/tmp/nebula-standing-remove-collection.log`; execution:
  `/tmp/nebula-standing-remove-test.log`; JUnit:
  `/tmp/nebula-standing-remove-junit.xml`.
- External reviewed selection and receipt:
  `/tmp/nebula-standing-remove-selection.json` and
  `/tmp/nebula-standing-remove-selection-receipt.json`. The final plan is bound
  to this complete diff against the exact base. Shared CI selection is untouched.
  Machine execution receipt: `/tmp/nebula-standing-remove-execution-receipt.json`.
- `git diff --check` passes. No public Core, browser, native process, account or
  deployment started. The fixture's runtime/database cleanup completed normally.

## Remaining gaps

The original CI runtime error remains unexplained. A local pass demonstrates
non-reproduction, not a repaired original runtime fault. The next exact-node CI
retry must retain any failing event payload before a behavioral repair is chosen.
No installed-vendor, public HostAPI/LAN, browser, physical-device, deployment,
publication or merge result is claimed by this diagnostic-only change. Root owns
the original durable goal's remaining acceptance and integration work.
