# Native standing-instruction CI checkpoint

Reload the complete [standing-instruction goal](standing-instructions-goal.md),
[standing-instruction checkpoint](standing-instructions-checkpoint.md), and this
checkpoint after compaction or resumption. Preserve the complete goal and all its
criteria. This repair owns one failing browser journey only.

Status: CI head `2b4ff750` supplies the actual native failure in all eight
profiles: a fresh valid head is rejected by the high-resolution wall clock versus
filesystem-mtime comparison before any prompt. Independent causal, repair-design
and final static validation passed. The advancing filesystem barrier is ready;
runtime verification remains pending the same eight failed CI nodes. No prior
pass is repeated. Older records below are historical; the original `eee508bc`
mechanism is not retroactively proven.

## Scope and operator contract

- Worktree: `/workspace/nebula-ci-native-survival`; branch:
  `codex/ci-native-standing-survival`; base:
  `08f996a3741866d78cf86f20bc24a4f747e4e01a`.
- Failure: `native standing instructions survive lossy compaction, reconnect and
  API replacement without transcript injection`,
  `ui/tests/runtime-continuity.spec.ts:413`, `model-chromium-320`.
  Original CI build: `eee508bc`, run 42666867639, job 129332908052.
- Root owns shared specification/configuration/CI selection, push, review and
  merge. The initial assertion repair changed only this native subsection and
  checkpoint. The second-CI follow-up adds fixture observations described below;
  production behavior, native guard results and helper-test assertions stay unchanged.
- Latest user scope permits only this exact failing title/profile on retry.
  Python, frontend-unit and native-unit selections are empty. No other journey,
  profile or suite executes. No skipped assertions, deadline/retry changes,
  memory protection changes, installed vendor, live account, existing
  installation, deployment, push or release.

| Step | Observable invariant | State authority |
| --- | --- | --- |
| Discover/select | Saved native profile and same chat are visible | Core catalog and URL |
| Initial use/stream | New reply is visible; one standing snapshot in native head, none in prompt | Core snapshot and actual native channel |
| Lossy compaction | Summary omission preserves standing authority once | Native head and Core snapshot |
| Reconnect | Same native identity/head reload without another copy | Core receipt and native session |
| API replacement/refresh | Same Core chat and retained native context; one submission per turn | Core, URL and retained execution |
| Saved edit | BETA supersedes ALPHA in a fresh native context while Core chat survives | Core decision revision and native channel |

Interrupt, deletion, fork and live vendor conformance are outside this failure's
journey. Chromium Android 320 is emulation; physical devices are unverified.
Repository troubleshooting/product-quality, test selection, operator workflow,
interface and cloud-runtime instructions were read before work.

## Causal evidence and independent gates

1. Original durable raw CI log:
   `/tmp/nebula-ci-eee508bc-chromium320.log`. The first send creates an optimistic
   assistant bubble, but no markdown appears in 60 seconds. It never reaches
   native capture or `checkHead`. Artifact 13697727289 is retained in FileService;
   its local download is policy denied and no bypass was attempted. An optimistic
   bubble does not establish actual native readiness or prompt delivery.
2. Unchanged root-base production reproduction delivers ALPHA, then fails the
   digest assertion: 1 failed in 39.7 seconds. Raw data/trace/screenshot:
   `/tmp/nebula-native-survival-baseline`; log:
   `/tmp/nebula-native-survival-browser-baseline.log`. Unchanged exact `eee508bc`
   source/UI comparison independently delivers ALPHA and reaches the same
   assertion: 1 failed in 36.7 seconds. Evidence:
   `/tmp/nebula-native-survival-eee-baseline`; log:
   `/tmp/nebula-native-survival-browser-eee.log`.
3. Fresh-context `validate_native_browser_failure` independently confirms a
   test-only digest contract error before repair. Actual 1,302-byte
   `session/new._meta.rules` hashes to the full-channel receipt `fe490a5d...`;
   the exact 587-byte standing snapshot hashes to `1136ed12...`. The full rules
   contain the snapshot once, the head contains the rules once, and the prompt
   has no standing-rule injection. Core intentionally stores the full developer
   channel digest in `_connection` / `_mark_instruction_channel`, not its subset.
4. After that narrow repair, the same exact title reaches all five native
   replies, including visible BETA, then fails an assistant DOM-count assertion
   (expected 5, observed 4 then 3). Evidence:
   `/tmp/nebula-native-survival-fixed`; log:
   `/tmp/nebula-native-survival-browser-fixed.log`. Messages HTTP 200 returns all
   ten unique sequential records, including BETA at sequence 10.
5. Fresh-context `validate_native_reply_count` independently confirms the second
   test-only fault before repair: trace call 152 reads four mounted assistants;
   call 158 expects five, while its virtual list mounts only indices 4-9 (three
   assistant rows), with BETA visible. `SessionsPage.tsx` uses a virtualizer with
   overscan four and end following, rendering only `getVirtualItems()`. Exactly
   five submissions, five managed prompts and five native events contradict
   transcript loss or duplicate execution. Removing the count alone could match
   a stale ALPHA. The saved BETA UUID equals its visible row's DOM UUID.

The native test now hashes the unique actual managed session/new rules for the
active native ID, verifies full rules and standing snapshot each occur once, and
keeps the separate snapshot/head digests and receipt identity checks. Its send
helper polls for exactly one newly persisted assistant, verifies its new ID and
exact content, then asserts the same ID's visible markdown. Submission counts,
native execution assertions and existing deadlines remain. A pending-turn-null
read is not treated as proof of acceptance.

## Focused execution receipt

- Exact title/profile recollected: **1 test**. Final execution: **1 passed**,
  no failures, skips or retries; 1.2-minute test, 1.5-minute runner.
- Command: `npm --prefix ui run test:e2e -- tests/runtime-continuity.spec.ts
  --project=model-chromium-320 --grep 'native standing instructions survive lossy
  compaction, reconnect and API replacement without transcript injection'`.
  Runtime environment pins this worktree's source and production UI, the shared
  Poetry interpreter, `/usr/bin/chromium`, LAN host `172.26.91.238` and port 19881.
- External diff-bound plan and reviewed receipt:
  `/tmp/nebula-native-survival-selection.json` and
  `/tmp/nebula-native-survival-selection-receipt.json`. Root's shared CI plan
  remains untouched. Final arrays: Python/frontend/native empty; one exact
  browser title in `runtime-continuity/model-chromium-320`.
- Production build passed. Log: `/tmp/nebula-native-survival-build.log`.
  Served index SHA256:
  `45f1e2233bba870160b6045349688b3015bfe4486a9bb386dbaf00568b59b410`.
- Authenticated real-Core origin: `http://172.26.91.238:19881`; actual public
  HostAPI/IPC and Grok adapter, inert in-process ACP with zero vendor workers.
  Five submissions/prompts/events, instruction copies `(1,0)` four times then
  `(0,1)`. Same native ID/head through compaction/reconnect/API replacement;
  BETA gets a different native ID/head. Fourteen actual ACP calls, including a
  genuine session/load. All managed prompts remain in retained acceptance-v1
  source/Python and contain neither standing rule.
- Full raw JSON/screenshot:
  `/tmp/nebula-native-survival-final/runtime-continuity-native--39d5d-ithout-transcript-injection-model-chromium-320/`.
  Machine receipt: `/tmp/nebula-native-survival-execution-receipt.json`.
  Final log: `/tmp/nebula-native-survival-browser-final.log`.
  Raw JSON SHA256: `a659d8e682207160384bf5de1837ae1f706b283d87dd95e3acdcf0231db52854`.
- Production journey's accessibility, composer viewport and horizontal-clipping
  assertions passed. The owned temporary Core data/bundles were removed, and
  both owned listeners (19881/19882) are stopped.
- Before the stricter failed-only steering, the two native-helper checks passed
  in 7.34 seconds; two strengthened checks passed in 6.69 seconds. Their test diff
  was removed. They were not rerun after steering. The original missing-head
  refusal remains unchanged. A separate diagnostic of 30 fresh exact artifacts
  all passed the production head-readback guard; it does not prove CI freshness.

## Remaining work and limits

Root must integrate this assigned subsection, bind the shared CI selection to
only the exact failed title/profile, and retry CI. Preserve the original CI
missing-first-reply uncertainty; the passing local receipt does not identify
that mechanism. Installed vendor/live account/native GUI, physical devices and
other browser profiles remain unverified and were outside this failed-only run.
Root owns final diff/CI review, publication and merge of the complete durable goal.


## Second CI head 3c55e4b8: active failure investigation

Reload the full durable goal and both checkpoints before resuming this follow-up.
Worktree `/workspace/nebula-ci-native-reconnect`, branch
`codex/ci-native-reconnect`, exact base
`3c55e4b8416e81965490b73f8202e0bbebe91460`. Root owns push/merge, global CI
inventory, packaged-native failure and the shared failed-case selection.

CI run 42674828799 reports all eight native title/profile jobs failed at the new
five-second durable-reply poll, `runtime-continuity.spec.ts:514`. Complete raw
WebKit 320 log: `/tmp/nebula-ci-3c55e4b8-native-webkit320.log`, job 129353417722.
It reaches the third turn after native reconnect, expected 3 saved assistants,
received 2. The connector supplied the other seven complete native job logs
without accessing blocked artifact URLs:

| Profile | Job | First failing durable count |
| --- | --- | --- |
| Desktop | 129353417796 | expected 1, received 0 |
| Chromium 320 | 129353417827 | expected 1, received 0 |
| Compact | 129353417850 | expected 1, received 0 |
| WebKit 390 | 129353417921 | expected 1, received 0 |
| Chromium 390 | 129353417951 | expected 1, received 0 |
| WebKit 430 | 129353418083 | expected 1, received 0 |
| Chromium 430 | 129353418414 | expected 3, received 2 |

Those logs are `/tmp/nebula-ci-3c55e4b8-native-job-<job>.log`. No raw turn/error
or native artifact timing is printed in them. A reconnect-only cause is therefore
unproved; six profiles fail on their first turn. Preserve the original eee508bc
missing-first-reply uncertainty separately.

Only the exact still-failed native title in `model-chromium-320` will execute
locally. It collects exactly one case. Local WebKit binaries are absent; system
Chromium is available. No passing helper/contracts or other journey/profile may
be rerun. No behavior/assertion/deadline/guard repair before the fresh causal gate.
Root permits one fresh `fork_turns:none` validator, read-only and no tests/services,
after the evidence packet is ready; no duplicate validator exists yet.

Production UI build passed (`/tmp/nebula-native-reconnect-build.log`), served index
SHA256 `3b1b7369de7b9702009cfc72795a24f177bfa7c4dafb4e796d65311d3de22582`.
Product knowledge was built and verified exactly as in CI, 418 passages, source
`946375111ee3`; log `/tmp/nebula-native-reconnect-knowledge-build.log`. Product,
fixture and spec behavior were unchanged for that baseline execution. Its
one-case review receipt is
`/tmp/nebula-native-reconnect-selection-baseline-receipt.json`. The final external
plan/receipt `/tmp/nebula-native-reconnect-selection{,-receipt}.json` has all local
execution arrays/counts empty under the later user instruction; root owns the
next CI failed-node selection.

The one unchanged Chromium 320 case completed: **1 passed**, zero skips/retries,
1.1-minute test and 1.4-minute runner. All five actual native submissions/prompts/
events completed with `(1,0)` four times then `(0,1)`. Raw evidence:
`/tmp/nebula-native-reconnect-baseline/runtime-continuity-native--39d5d-ithout-transcript-injection-model-chromium-320/`;
log `/tmp/nebula-native-reconnect-browser-baseline.log`; machine receipt
`/tmp/nebula-native-reconnect-execution-receipt.json`. Temporary fixture data/bundles
were removed and ports 19881/19882 stopped.

Fresh-context `validate_native_second_ci` independently reviewed all eight raw CI
failures, the successful local artifact, owners/callers and competing mechanisms.
Its causal gate is **unresolved**. The CI count reads use all durable messages;
virtualization is not the cause. A browser POST count proves outgoing submission,
not acceptance. Delayed acceptance/open/finalization and failed or rejected
native delivery remain consistent with the evidence. No deadline, memory or
readback repair is justified.

Root relayed the latest user restriction after that local pass: **do not rerun
any previously passed case**, including this local Chromium 320 case. No more
local tests are permitted. Next CI is root-owned and may select only its ten
currently failed browser nodes (eight native, two provider-tools); six passing
tools nodes and all passing Python/frontend/Rust/checks are excluded. Root owns
the packaged-native case separately. No helper/contracts were rerun here.

### Diagnostic-only patch for the next failed CI attempt

- Native fixture wraps the original readback function once, passing exactly the
  original arguments and returning its original boolean. Observation and even
  observation-error logging are best effort. No guard result is overridden.
- The fixture observes metadata **after** the original guard, with descriptor-
  relative `O_NOFOLLOW` traversal rooted at the owned synthetic data, at most
  4,096 parent entries and 2,000,001 bytes. It records result, exact decimal-string
  nanoseconds, observed candidate count/file type/size/UTF-8 validity/occurrence
  count and error stage/errno. No instruction body or account path is printed.
- Native request/event timestamps and native/Core session IDs correlate activity.
  An optional capture flag reads only the last three durable turn summaries,
  their status/revision/timestamps/final message identity and safe failure reason.
- The existing reply-count assertion keeps its original predicate and deadline.
  On failure, the native test prints an allowlisted bounded summary of POST
  status/receipt, counts/IDs, turn state and recent readbacks/calls/events, then
  rethrows the original assertion. Successful capture artifacts include the new
  diagnostics without creating another test. Other journeys remain untouched.
- Fresh independent static review found and corrected an intermediate-symlink
  observation path, missing correlation fields and a logging exception boundary.
  Its final review found no blocking safety/result issue. The causal gate remains
  unresolved. Metadata from the second read is not the original guard's exact
  rejection branch, and added file/lock work may affect timing despite preserving
  the original result.
- These diagnostics were added **after** the local baseline pass. They have not
  been executed because the user forbids repeating that passed case. Do not cite
  the baseline pass as runtime verification of this diagnostic patch.

Static-review record: `/tmp/nebula-native-reconnect-static-review.json`. The
original guard arguments/result, poll deadline/predicate and assertion rethrow
were independently reviewed; no remaining blocking safety/result issue was found.
The diagnostic patch is runtime-unverified under the user's no-repeat rule.

Next: commit the statically reviewed diagnostic-only diff without a push, and
have root collect the next exact failed-node CI evidence. Preserve both unknown
CI mechanisms and do not infer a cause from the five-second count symptom.


## Source-contract gate: one native answer-completion budget

Root obtained a new fresh-context source review from
`validate_native_completion_budget` before authorizing this repair. The review
independently established a test-contract defect without claiming the actual CI
backend cause: the original mounted-row prerequisite could observe an optimistic
empty assistant before Core acceptance, then wait 60 seconds for answer text.
The substituted durable-count predicate instead observes `_complete_owner`'s
final assistant transaction. Placing it under the former default five-second
row poll imposed a new five-second completion requirement before the unchanged
60-second text wait. All eight CI failures stop at that prerequisite, but their
underlying delivery/timing mechanism is still unknown.

The source-consistent six-second completion counterexample proves the contract
restriction; it is not an observed CI timing trace. Neither a production memory,
freshness, instruction-channel nor delivery repair is authorized by this proof.

Follow-up is based directly on diagnostic commit
`1fde06732c1e587fed859691e1c30177d8d87e7a` in the same isolated worktree/branch.
Root already integrated that diagnostic commit separately. Only the native send
helper and this checkpoint change in the follow-up:

- Keep the outgoing-submission count under its original default five-second poll.
- Start one monotonic `performance.now() + 60_000` deadline immediately afterward.
  Remove the initial post-submission GET outside the completion phase by setting
  `current = previous` before the budgeted poll.
- Give the durable-count poll only `floor(deadline - now)` milliseconds. Preserve
  exact-one/new-ID uniqueness and exact saved-content assertions between the
  count and visible-text checks.
- Give the same saved ID's visible markdown only the remaining time from that
  same deadline. A nonpositive remainder throws before passing a zero timeout,
  so no unlimited timeout or second 60-second allowance is introduced.
- Preserve Stop-hidden, inactive capture, all diagnostics/rethrow, the overall
  300-second journey and every production memory/readback/channel guard.

The same fresh source validator statically reviewed the final diff and found no
blocker in deadline accounting. All completion GETs are inside the budgeted poll;
ID/content checks and visibility share that one phase. Failure diagnostics may
consume time after a failed phase but cannot turn it into success. This restores
the source-proven completion boundary, not an established CI backend fault.

No tests, profiles, helper/contracts, collection, checks, builds or services were
executed for this follow-up. Every previously passed local native case stays
excluded. Root will verify only its eight still-failed native title/profile nodes
in CI; passing tools, Python/frontend/Rust/checks and other journeys remain
excluded. Runtime verification is explicitly pending.

Static receipt: `/tmp/nebula-native-completion-budget-static-review.json`.
External local selection: `/tmp/nebula-native-completion-budget-selection.json`
(all execution arrays/counts empty under user instruction). Root owns the shared
CI plan, push, review and merge. Preserve both the current CI backend uncertainty
and the separate original `eee508bc` unknown on resumption.


## Confirmed readback failure: CI head 2b4ff750

Reload the full standing-instruction goal and both checkpoints after resumption.
New isolated worktree `/workspace/nebula-native-readback-freshness`, branch
`codex/native-readback-freshness`, exact published base
`2b4ff750617b9637eabec12778fc8a536f7d91aa`; all combined global source is preserved.
Shared CI selection and workflows remain root-owned and unchanged here.

Run 42685160535 diagnostics are saved in
`/tmp/nebula-native-failure-2b4ff750-<job>.json`. All eight profiles fail their
first send with interrupted Core/Harness turns, one submission, and no native
prompt/output event. Each actual guard returned false although its scoped head
is regular, unique, 1,361 bytes, within the cap, valid UTF-8 and contains the exact
full expected channel once. The measured mtime is 0.211-1.429 ms below the mark.
Source ordering puts `time.time_ns()` before session/new, whose inert RPC then
writes the head synchronously before returning. No later fixture head write
occurs before guard/observer. These facts exclude an actually older retained
head, wrong account, duplicate identity, missing body, cap or decoding failure
for these captured cases.

Desktop job 129390038676 records mark `1794253227718375563`, subsequent
session/new entry `1794253227718426320`, and freshly written head mtime
`1794253227716946717`: 1,428,846 ns below the mark. Actual guard false leads to
interrupted/invalid_input with no session/prompt. Complete derived evidence:
`/tmp/nebula-native-readback-freshness-causal-evidence.json`.

Fresh-context `validate_native_readback_freshness` independently confirmed this
actual rejection and the two-stage filesystem-barrier repair before edits.
[Linux v6.8 current_time](https://github.com/torvalds/linux/blob/v6.8/fs/inode.c#L2492)
uses `ktime_get_coarse_real_ts64` then filesystem granularity truncation. This
primary source explains the coarse-clock mechanism; the exact runner kernel
build is not established. CI ordering itself proves the invalid comparison.
Older CI failures without equivalent metadata remain separately unproven.

### Repair, protections and final static review

- `grok_channel_barrier` follows the trusted configured account-root symlink as
  before, but securely opens/creates sessions with O_DIRECTORY/O_NOFOLLOW.
- An exclusive random 0600 probe gets a real one-byte write and first fstat
  mtime. A one-second monotonic bound, one-millisecond pauses and real one-byte
  rewrites require a **strict** mtime advance. No utime, acceptance tolerance or
  wall-clock fallback exists. Inability or no advance refuses the open.
- Cleanup verifies the probe entry still matches its exclusively owned device/
  inode, unlinks it and closes all descriptors in finally blocks. This completes
  before returning the barrier and before session/new or session/load can start.
  Cleanup failure also refuses the open.
- Ordinary unmodified preexisting heads are at or below the first tick, which is
  strictly below the advanced barrier; they remain stale. Fresh new/load writes
  can share the advanced filesystem tick and pass. Clock rollback or unsupported
  nonadvancing storage fails closed.
- Readback keeps mtime >= barrier plus scoped identity, no-follow child/file,
  unambiguous regular head, bounded scan/read, UTF-8 and exact text once. It also
  binds sessions root and head to the probe device, rejecting another mount.
  Optional device compatibility preserves existing direct guard callers; existing
  guard tests/assertions are unchanged and were not rerun.
- `finish_cleanup(asyncio.to_thread(...))` shields and fully joins the probe even
  under repeated cancellation. BaseException cleanup joins RPC close. Original
  and close failures are retained together. A group containing CancelledError is
  re-raised as CancelledError **from the full group**, so unchanged caller gateway
  cleanup and queued-turn settlement run. Ordinary error/cancel classes remain.
- The public native fixture prepares only its scoped account directory, matching
  real `_connect`, and forwards expected device to the original guard exactly
  once. Diagnostics label filesystem clock/device without changing its result.
  The retained continuity fixture gets only account mkdir preparation; its
  previously passed cases and assertions are not selected or executed.

Fresh final static review from `validate_native_readback_freshness` passes. It
confirmed the expected-directory audit marker, configured-root compatibility,
joined probe/RPC lifecycle, grouped-cancellation propagation, exact guard/device/
cap constraints and genuine fixture setup. Root independently read and agreed
with the final diff before authorizing commit. No concrete defect remains in
those static reviews; runtime proof is still pending.

Root expressly allowed the formatter only as an edit on the four modified Python
files (one reformatted, three unchanged). No formatter check, pytest, Playwright,
collection, build, mypy, audit, probe execution or previous passing static check
ran. Existing installations, live accounts and installed vendors were untouched.
The probe bound stays within the unchanged native startup allowance; submission,
answer/Stop/capture and 300-second browser budgets remain unchanged. Root will
push only the same eight failed native nodes; both tools cases and every prior
pass remain frozen. The packaged GUI failure is a separate root-owned workstream.

Static receipt: `/tmp/nebula-native-readback-freshness-static-review.json`.
External local selection: `/tmp/nebula-native-readback-freshness-selection.json`
(all execution arrays/counts empty). Root owns integration, push, exact failed-node
CI, review and merge. Report the change as statically verified until that CI
provides runtime evidence; do not infer installed-vendor conformance.
