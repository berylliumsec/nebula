# Failed-only runtime retry

The user explicitly requires pushes to select only specific failed cases, with
passing cases excluded. This record supersedes the earlier combined test union
for retry selection; it does not erase that run or expand its passing receipts.

## Completed original CI

Candidate: `eee508bc9c1c493cc019da95ca5feb398869ebfa`.

- CI run `42666867652`: 964 Python cases passed, five failed, one skipped.
- Browser run `42666867639`: 32 cases passed, 16 failed, none skipped. Each of
  eight profiles had the same two failed titles and four passing journeys.
- Frontend diagnostics and macOS UI compilation failed before their tests ran.
- Native job `129332934450` built both independently retained packages, collected
  exactly one case, and its `native-journey` command exited 1. The inner traceback
  was retained in artifact `13699266079`; this environment cannot read its
  downloaded File Service URL. No particular native failure cause is claimed.

The original failed-case inventory and decoded logs are retained under
`/tmp/nebula-ci-eee508bc-failed-cases.json` and the paths recorded there.

## Exact retry scope

The reviewed receipt selects these five portable Python nodeids:

1. `test_api_worker_composition.py::test_actual_api_factory_keeps_receipts_and_global_writes_atomic[sqlite]`
2. The same function with `[postgresql]`.
3. `test_standing_instruction_continuity.py::test_edit_or_remove_replaces_old_native_instruction_authority[True]`
4. `test_shutdown_readmission.py::test_actual_core_shutdown_reopen_readmits_existing_project_and_preserves_pauses[sqlite]`
5. The same function with `[postgresql]`.

The single packaged case is
`test_packaged_runtime_continuity.py::test_real_packaged_windows_preserve_held_turn_and_draft_through_code_rollback`.
CI routes it exclusively to the native package job, avoiding another portable
skip. The receipt's Python total is six: five portable plus one packaged case.

The sixteen browser retries are only these two exact titles in desktop Chromium
1440/1024, Android Chromium 320/390/430 and mobile WebKit 320/390/430:

- `loaded provider tools survive compaction, reload, execution replacement and a historical fork`
- `native standing instructions survive lossy compaction, reconnect and API replacement without transcript injection`

Individual catalog entries carry title filters while preserving the continuity
fixture's runtime provisioning. Frontend and Rust test selections are empty.
Checks-only flags rerun the failed frontend diagnostic audit and macOS production
UI build. PostgreSQL and the readmission production assets are provisioned only
for the selected cases. No passing journey or file-wide suite is selected.

## Independently validated repairs and local results

- The composition fixture lacked the API worker's required peer secret and
  identity. Production assembly already supplies them. The fixture-only repair
  preserved actual API composition, receipt replay and rollback assertions;
  the two exact failed variants passed in 4.01 seconds.
- Standing removal lost first-send errors and collapsed second-send errors into
  a boolean. The diagnostic-only assertion repair retains structured errors at
  both boundaries. The exact failed `[True]` node passed in 2.73 seconds. Its
  original CI runtime cause remains unknown; no product guard was changed.
- Readmission's SQLite fixture stopped after turn completion while optional
  naming still had an authoritative write pending. Signed quiescence correctly
  refused. The dedicated fixture now observes real maintenance completion before
  shutdown. SQLite passed in 39.51 seconds. PostgreSQL passed its diagnostic run
  and was not repeated locally; its original CI IPC failure remains unlocalized.
- Native browser assertions compared a snapshot subset's digest to the complete
  native instruction channel and treated virtualized DOM rows as all replies.
  The repaired assertions verify the actual managed channel and each new durable
  reply ID. The exact Android Chromium 320 journey passed, with five submissions
  matching five managed prompts/events, full compaction/reconnect/replacement
  and instruction-edit evidence. The original CI first-reply timeout remains
  unexplained until the same exact-title CI retry runs.
- Tools browser navigation now brings the historical saved reply into the
  virtual transcript, waits for the new durable reply rather than repeated old
  text, and observes exact target execution readiness separately from durable
  restoration verification. The inert provider interprets only the current
  leading fixture command rather than archived command markers in retrieved
  transcript data. Repeated Work landmarks now have distinct human-readable
  message titles. Accessibility and all original manifest, effect, fork and
  continuity assertions remain enabled. Its final exact-case result is recorded
  in the worker receipt; no passing journey was rerun.
- macOS module resolution is repaired by the distinct component filename
  `NavigationContinuityBoundary`; frontend diagnostic handling uses fixed safe
  events or specific expected-response annotations. Earlier focused local checks
  passed and are deliberately excluded from the retry.

The native job now prints only its already-sanitized exported journey log, so a
remaining failure is diagnosable without the blocked artifact download. It must
rebuild its two fixture packages to execute its one failed case; no additional
packaged tests are selected. Logging does not repair or establish the missing
native result.

Independent static review of selectors, runtime guards and sanitized logging is
retained in `/tmp/nebula-failed-only-retry-final-audit.json`. Final code and
documentation changes require a fresh diff digest before the push.

## Continuing integration work

The exact-grant recovery reconciliation repair is integrated and has 30 distinct
local passes across phases, including two surviving-worker restart proofs. Those
passing cases are excluded from this retry.

Further reviewed host/global route repairs remain preserved outside this retry:
host operator commit `1094d45b`, its frontend routing commit `0758945d`, the
unfinished host Library worktree, and `codex/global-resource-catalog`. Setup,
folder, Library/artifact and resource-discovery ownership still require their
remaining integration and acceptance work before the entire architecture may be
called complete. These slices are not silently treated as merged or verified.

PR #960 remains unmerged until applicable checks and review gates permit merge.
Existing installations, live providers and deployments are untouched.
