# Failed-only runtime retry

The user explicitly requires pushes to select only specific failed cases, with
passing cases excluded. This record supersedes the earlier combined test union
for retry selection; it does not erase that run or expand its passing receipts.

## Eight native retries passed at `48eb7a36`

Browser run `42690527522` passed all eight exact native-instruction title/profile
pairs, one case per job, with no retries. Gate job `129412046757` passed. The
desktop/compact, Chromium 320/390/430 and WebKit 320/390/430 cases all retain the
same instruction identity/content, compaction, reconnect and replacement checks.
The Python/package/frontend/Rust layers were omitted on this push.

These eight passing cases must never be selected again. The sole remaining
failed case is packaged native continuity. Its next retry will be exclusively
that exact node after reviewed fixture preparation and failure diagnostics;
no browser case or prior passing test/check belongs in that receipt.

## Single packaged retry after reviewed preparation repair

The fixture repair is integrated from `1413cc2a`; see
[the preparation contract and verification limits](packaged-gui-initial-setup.md).
It removes the unobserved Ctrl+R dependency by reopening the bootstrap window
after seeding and before the first chat/draft setup. Before any connector or
reopen, exact Core, API/execution worker, Project fence and held-turn evidence
must remain unchanged while that initial window is closed. All later automatic
selected-chat/draft restoration, rollback, completion and shutdown checks remain.
Separate final static review passed, including bounded private-data-free failure
diagnostics. Product source trees are unchanged from `48eb7a36`.

The next receipt selects exactly the single still-failed packaged node in its
artifact job. Every portable, frontend, Rust and browser test is excluded. The
two genuine fixture packages are necessary build dependencies. No passing case
was executed to prepare this repair; its actual native result remains pending
at this commit. PR #960 records subsequent CI and merge results without requiring
another push of passing cases.

## Latest results at `2b4ff750`

Browser run `42685160535` completed with both selected tools cases passing
(desktop and compact, one case each) and all eight native-instruction cases
failing. All eight tools profiles now have passing CI receipts across the two
retries and must remain excluded. The packaged case in CI run `42685160515`,
job `129390041028`, failed after both packages built and after the original
typed-admission prerequisite succeeded. Its native GUI helper timed out finding
the Project control during baseline `initial_setup`. That separate failure is
under causal investigation and is deferred from the eight-browser-only push;
it is not a passing or completed gate.

Bounded diagnostics confirm the same first-turn native readback rejection in
all eight profiles. The exact current system head is a single regular UTF-8
file under the byte cap, with one occurrence of the expected text. Its
filesystem-assigned modification time is slightly earlier than the preceding
wall-clock barrier, although the fixture creates its fresh native UUID and
writes the file after the session request. The guard rejects that timestamp,
the turn is interrupted, and no native prompt is sent. Independent causal review
confirmed the predicate and ordering; the precise lower-level clock mechanism
is not established by this trace.

A same-filesystem freshness repair passed independent final static review.
It retains stale-artifact refusal, exact account/session identity and bounded
readback. Probe and RPC cleanup are joined; grouped cancellation preserves the
existing gateway cleanup and queued-turn settlement paths. No passing case was
rerun to prepare this repair. The next selection contains only the eight failed
native browser pairs. The single packaged case will have a separate retry after
its cause and repair are reviewed; passing browser pairs must stay excluded.

## Current retry after `3c55e4b8`

All five portable Python retries passed, as did frontend diagnostics and macOS
UI compilation. Six tools-browser mobile profiles passed. The remaining CI
failures are the native standing-instruction title in eight profiles, the tools
title in desktop/compact, and the single packaged native case: **11 cases**.
The next receipt excludes every passing case and the passing checks.

The two tools failures were idle fine-pointer composer hit testing; normal hover
restores the real interaction. Their exact two local retries passed once, with
all assertions retained. Independent source review also confirmed that the native
helper had moved full durable completion into an optimistic-row five-second
wait. One shared sixty-second answer budget now covers persistence and matching-ID
visibility, retaining the original checks; no passing native case was rerun.
Privacy-safe diagnostics remain available because the actual CI backend cause
is still unconfirmed.

The packaged case reached the held chat through legacy delivery before its new
Project had an execution owner; its required typed binding was therefore absent.
The driver now observes that empty Project through an authenticated scoped read
before its single held-chat POST. The failed case still needs genuine CI proof.

Remaining host/global source repairs are now integrated; see the
[source integration and verification limits](host-global-route-integration.md).
Their new regression sources are excluded from execution under the user's
restriction. The older sections below preserve the preceding retry history.

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
  in `/tmp/nebula-ci-provider-tools-final-receipt.json`: one Android Chromium 320
  case passed in 1.5 minutes, including unchanged Axe checks and the historical
  fork. Five effects, one load and exact old/new worker identities were observed;
  no passing journey was rerun. Three exited zombie entries awaited PID 1 reaping
  after all live fixture workers and the supervisor were gone.
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

At the previous checkpoint, further host/global route repairs were preserved:
host operator commit `1094d45b`, its frontend routing commit `0758945d`, the
unfinished host Library worktree, and `codex/global-resource-catalog`. Setup,
folder, Library/artifact and resource-discovery ownership now have integrated
source repairs. Their additional runtime acceptance remains unverified under the
current test restriction; source integration is not complete acceptance evidence.

PR #960 remains unmerged until applicable checks and review gates permit merge.
Existing installations, live providers and deployments are untouched.
