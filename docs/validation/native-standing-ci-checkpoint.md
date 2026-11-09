# Native standing-instruction CI checkpoint

Reload the complete [standing-instruction goal](standing-instructions-goal.md),
[standing-instruction checkpoint](standing-instructions-checkpoint.md), and this
checkpoint after compaction or resumption. Preserve the complete goal and all its
criteria. This repair owns one failing browser journey only.

Status: the exact native Chromium 320 journey passes locally. The original CI
missing-first-reply mechanism remains unknown and requires the next exact-title
CI retry. The later independently confirmed assertion faults are repaired.

## Scope and operator contract

- Worktree: `/workspace/nebula-ci-native-survival`; branch:
  `codex/ci-native-standing-survival`; base:
  `08f996a3741866d78cf86f20bc24a4f747e4e01a`.
- Failure: `native standing instructions survive lossy compaction, reconnect and
  API replacement without transcript injection`,
  `ui/tests/runtime-continuity.spec.ts:413`, `model-chromium-320`.
  Original CI build: `eee508bc`, run 42666867639, job 129332908052.
- Root owns shared specification/configuration/CI selection, push, review and
  merge. The patch changes only this native test subsection plus this checkpoint.
  No product, native-fixture or helper-test behavior changed.
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
