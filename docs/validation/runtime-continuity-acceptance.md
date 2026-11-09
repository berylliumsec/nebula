# Runtime continuity acceptance

| Journey | Observable invariant | Authority | Regression entry |
| --- | --- | --- | --- |
| Source API replacement | One accepted turn and producer survive API promotion, retirement and rollback | Durable command, stream cursor, execution lease | `test_retained_source_api_replacement_and_code_rollback_keep_one_turn` |
| Orchestrator crash | Each committed handoff phase resumes without another transfer or producer | SQL handoff and exact process birth | `test_actual_supervisor_crash_resumes_each_committed_handoff_phase` |
| Participant restoration | A due schedule stays held before activation and runs once; prior pauses survive transfer | Durable occurrence, goal and schedule records | `test_actual_participant_handoff_runs_one_due_schedule_and_preserves_prior_pauses` |
| Cold reserve recovery | Verified production UI and authenticated budgets work before any API can start | Host release selection, auth and memory policy | `test_zero_api_recovery_serves_verified_ui_and_admits_after_reserve_readback` |
| Running allocator | API replacement still routes a live reduction to the original physical workload | Job ID, original controller, immutable group and kernel readback | `test_live_apply_after_api_replacement_reaches_original_physical_allocator` |
| Client loses acceptance | Reload reconnects the same request without submitting it again; device draft remains | Core/principal identity, pending request and URL | `runtime-continuity.spec.ts` |
| Launcher continuity | Short-lived connectors leave one stable service; candidate and rollback preserve its identity | Retained release plus persistent service | `test_actual_runtime_service_connector_updates_rolls_back_and_stops` |
| Packaged desktop | Close/update/reopen retains one accepted turn and candidate-era data; explicit shutdown stops Core | Installed artifact identities plus stable service | `scripts/test_packaged_runtime_continuity.py` |

## Isolation

The source fixture copies a Python interpreter, standard library, installed
dependencies, Nebula source and production UI into independently retained
version directories. It removes their replaceable staging directories before
launch. A standalone probe confirms Core, SQLAlchemy and Chroma import from the
retained version, with an intentionally unusable inherited `PYTHONPATH`.
Host operating-system ABI libraries remain prerequisites; this is not a portable
distribution or packaged compatibility certification.

Only provider output, explicit timing barriers and the selected disposable
allocator adapter are synthetic. API, host authentication, SQL, command delivery,
stream journal, project construction, supervisor and execution processes remain
real. Each test owns fresh data, credentials and sockets. Cleanup signals only
the exact PID/birth pairs in that test's SQL registry. The browser controller
creates a fresh installation per case and never replaces application routes.

The physical allocator requires `NEBULA_TEST_MEMORY_RUNTIME`,
`NEBULA_TEST_MEMORY_IMAGE` and optionally `NEBULA_TEST_MEMORY_SOCKET`. No image is
downloaded automatically. Its adapter certifies the explicitly selected local
cgroup-v2 daemon only, not rootless production isolation or another platform.

## Observed source results

Against baseline source `47a1d27b`, the initial focused execution collected two
cases and passed both in 71.13 seconds: independent retained imports and SQLite
API replacement/rollback with one producer. The latter retained a project edit
committed under the candidate API and left a third version staged.

The next execution collected five SQLite crash cases and passed all five in
256.96 seconds: requested, finishing, transferable, transferred and verified.
Each killed the actual supervisor, kept the exact worker processes alive,
reconstructed the host, retained the correct ownership fence and replayed one
complete turn with no pause, cancellation or second provider execution.

Raw receipts: `/tmp/nebula-acceptance-initial-selection.json`,
`/tmp/nebula-acceptance-initial-selection-receipt.json`,
`/tmp/nebula-acceptance-initial-tests.txt`,
`/tmp/nebula-acceptance-crash-selection.json`, and
`/tmp/nebula-acceptance-crash-tests.txt`. Root owns the final diff-bound selection;
these narrowly scoped development receipts are not a final-candidate receipt.

The initial version labels contain the same source revision. They prove process
replacement and independently retained assets, not compatibility of two released
code versions. Run the final fixture against the assembled candidate, and use
the packaged driver with distinct real artifacts for that separate gate.

## Pending gates

The dedicated Playwright config permanently inherits desktop Chromium 1440/1024,
Pixel 5 Chromium 320/390/430 and iPhone 13 WebKit 320/390/430. Its three
journeys collect 24 cases; the physical live-apply journey requires an explicitly
selected disposable runtime and cached image. Collection is not execution evidence.

Physical allocator routing, final runtime-service launchers and the complete
production LAN matrix remain pending. The managed browser cache has no WebKit runtime; previous official
downloads were rejected with HTTP 403 `Domain forbidden`. No bypass is used.
Physical devices and packaged desktop continuity have not been exercised. These
missing gates prevent an unqualified product-completion claim.

## Integrated source findings

The UI built successfully from a detached Git worktree at `af44af34`, including
the 601/601 workflow registry check. `web-build.json` records that exact commit
and `dirty: false`. An earlier archive omitted registry test inputs and Git
metadata; these were build-fixture errors, not product failures.

The initial integrated source selection found three independently validated
fixture faults: an unhandled expected memory-admission refusal closed a test
HTTP connection, the allocator directory inherited mode 0700 under the private
umask, and parent/worker PostgreSQL pools did not share explicit settings. The
fixtures now handle only the expected refusal, explicitly make the synthetic
allocator directory traversable by its non-root workload and use one bounded
pool configuration. The restoration journey now follows its accepted command
receipt rather than requiring a ChatTurn before admission.

On assembled source `78e9577a`, five selected cases finished in 220.45 seconds:
three passed (cold reserve recovery, prepared restoration holding queued work,
and PostgreSQL API replacement/rollback), while two exposed remaining gates.
The physical allocator ran successfully, but its live-apply command remained
queued and returned an accepted receipt. The actual clean runtime-service
launcher failed before its listener because it requested PostgreSQL pool
environment variables for SQLite. These failures remain visible; neither is
recorded as a passing workflow. Raw results and the source/test selection are
`/tmp/nebula-acceptance-rerun-78-tests.txt` and
`/tmp/nebula-acceptance-rerun-78-selection.json`. Per-case databases and logs are
retained in `/tmp/nebula-acceptance-rerun-78e9577a`.

The five PostgreSQL crash-phase cases passed on `78e9577a` in 266.57 seconds,
including actual source/target processes and host reconstruction. Results and
selection are `/tmp/nebula-acceptance-pg-phases-tests.txt` and
`/tmp/nebula-acceptance-pg-phases-selection.json`.

The first LAN attempt reached a proxy `Domain forbidden` page before pairing;
its trace is preserved at `/tmp/nebula-acceptance-lan-first-failure`. Independent
review established that the browser was proxying its own LAN address. The
dedicated fixture now bypasses that proxy only for addresses verified to belong
to this machine and loopback, preserving the inherited proxy for every other
destination. It uses config metadata for its private auth file, loopback
fixture controls, graceful shutdown and awaited per-case cleanup.

The desktop Chromium 1440 cold-recovery journey subsequently passed in 51.9
seconds at `http://172.26.91.238:19689`, using source `78e9577a` and the clean
`af44af34` production UI. A fresh paired device saved/read back the reserve
before any API worker existed, admitted an actual API, retained the saved value
on reload, and passed axe, focus/44px and no-clipping checks. The screenshot was
visually inspected. Evidence is in `/tmp/nebula-acceptance-lan-passed`; no
physical-device result follows. Every spawned worker was verified gone.

The desktop Chromium 1440 provider journey also passed (41.6 seconds in the
case; one minute including fixture setup). It paired normally, submitted one
provider turn, lost its initial response, replaced the API, recovered the same
durable assistant message, and preserved the URL and unsent draft on reload.
An independently validated fixture timing fault had run axe while the reloaded
conversation was still hydrating; the test now waits for the durable message
before scanning the same selector. Evidence is in
`/tmp/nebula-acceptance-provider-lan-passed` and
`/tmp/nebula-acceptance-provider-lan-rerun-tests.txt`. Subsequent runs write
pre-cleanup HTTP failures, health and bounded worker logs to a separate JSON
artifact, so teardown errors cannot stand in for live product state.

On source `48077146`, a three-case diagnostic run failed all three selected
cases in 129.12 seconds. The original physical job accepted a live reduction:
the API returned verified 16 MiB and independent container inspection observed
that exact limit. Its final long-running submission returned a saved request
receipt, while the fixture still required an immediate final response. The
clean SQLite runtime service started, its first deployment read reported no
promoted API, and a later read showed a ready promoted API. A subsequent explicit shutdown succeeded and removed
the exact service and worker processes. The Butler overview independently
reproduced HTTP 500, with a `CoordinationConflict` from a scoped count read in
`execution_scope._execute`. These are diagnostic observations, not passing
acceptance results. Raw output, SQLite backups and logs are retained in
`/tmp/nebula-acceptance-final-480-evidence`.

The first process participant execution reached verified target activation and
completed exactly one due schedule on that target. Its next requested version
remained staged with an explicit two-resident-version refusal; the
fixture had not yet retired the unowned older worker. The entire case remains
failed pending independent review and corrected sequencing. Its raw result is
`/tmp/nebula-acceptance-participant-tests.txt`. This journey does not establish
actual-process approval or callback continuity.

After independent review of those three fixture faults, the focused five-case
run on `17ec988e` passed all five cases in 351.42 seconds:

- Physical live apply on SQLite and PostgreSQL changed the original workload to
  16 MiB, independently read that limit from its actual group, completed with
  exit code zero, and verified group removal and zero reserved bytes. Saved
  receipts include the original launch identity and group.
- The actual clean SQLite `runtime_service` launcher created and listed a
  project, created an inert provider, automatically admitted its first chat,
  explicitly staged/promoted a candidate API, reconnected to the same Core,
  preserved candidate-era data through rollback, and verified explicit shutdown.
- Actual process participant transfers on both SQL dialects held one due
  schedule until target activation, completed that occurrence once, preserved
  the prior paused goal and disabled schedule, verified the required third
  version staging refusal, retired the positively unowned older worker, and
  completed a further transfer without another occurrence.

Raw results are `/tmp/nebula-acceptance-final-17-tests.txt`; its selection is
`/tmp/nebula-acceptance-final-17-selection.json`. Per-case evidence is retained
under `/tmp/nebula-acceptance-final-17ec988e`. This is source-runtime evidence;
it does not establish frozen or GUI package continuity. The Python acceptance
module now collects 26 cases, including two Butler overview regressions that
still require their separate product fix.
