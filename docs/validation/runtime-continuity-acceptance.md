# Runtime continuity acceptance

| Journey | Observable invariant | Authority | Regression entry |
| --- | --- | --- | --- |
| Source API replacement | One accepted turn and producer survive API promotion, retirement and rollback | Durable command, stream cursor, execution lease | `test_retained_source_api_replacement_and_code_rollback_keep_one_turn` |
| Orchestrator crash | Each committed handoff phase resumes without another transfer or producer | SQL handoff and exact process birth | `test_actual_supervisor_crash_resumes_each_committed_handoff_phase` |
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
Pixel 5 Chromium 320/390/430 and iPhone 13 WebKit 320/390/430. Its first two
journeys collected 16 cases. Collection is not execution evidence.

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
