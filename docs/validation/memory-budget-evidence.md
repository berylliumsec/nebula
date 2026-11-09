# Memory-budget implementation checkpoint

This checkpoint covers the memory addition to the deployment architecture. It
does not claim the full deployment architecture or every runner integration is
complete. No existing installation, live provider/SSH service, deployment or
unrestricted full test suite was used.

## Implemented and observed

- One SQL policy/reservation ledger across controller instances, with revisioned
  saves, immutable job policy snapshots, administrator ceilings, environment and
  permitted profile precedence, explicit swap and Core/service reserve.
- Transactional aggregate reservations include RAM, swap and required 64 MiB
  network helpers. Unknown cleanup remains charged. A lower aggregate drains
  existing reservations. Live changes reserve their largest intermediate amount.
- OCI creation precedes start. A protected, immutable guard verifies cgroup-v2
  limits, zero capabilities and no-new-privileges before releasing computation.
  Immutable container identities and job labels guard readback and live updates.
  Cleanup verifies workload and helper removal before freeing capacity.
- Launch ownership records the Linux process namespace/birth and the runner's
  configuration fingerprint and stable Docker daemon identity. Immutable main
  and helper IDs commit before start and survive measurements and live updates.
  Both normal and lost-owner cleanup validate authority before releasing capacity.
  Owner-mapped tools and injected PDF executors cannot bypass the boundary.
- Actual small bounded allocations prove below-limit completion, over-limit OOM,
  child sharing, cancellation, aggregate queuing, persistent workers, helper
  accounting, explicit lower job caps, measured usage/peak and acknowledged live
  reductions. Signal/exit status alone never establishes a memory-limit failure.
- Settings and typed Butler actions use the same authenticated Core API, read
  back saves, reject concurrent revisions, audit trusted actors, and distinguish
  configured policy from observed worker application. Browser drafts survive a
  conflict until reload. Durable failure records survive refresh.
- Catalog, Butler workflow inventory, operator guidance and the packaged product
  index are updated. The focused real Chroma check verifies current source digest
  synchronization and replacement of a stale collection digest.
- Charged jobs appear before queued work and completed history. Settings and
  Butler indicate when their bounded page may omit other jobs.

## Recovery and lifecycle review

An independent source review and inert fixtures identified and verified repairs
for stale handoff checkpoints after controls, the claim/retirement lock race,
unverified process births, failed-start slot leakage, retired-candidate retargeting,
retained-root symlinks and unbounded IPC response buffering. The subsequent
164-case SQLite/PostgreSQL lifecycle/memory selection passed 162, skipped one
SQLite-inapplicable PostgreSQL lock-wait case and exposed one subprocess fixture
import error. The repaired real crash case passed in the next bounded selection.

The 28-case recovery/guard/product-help selection passed, including an actual
32 MiB-contained worker whose controller was killed with SIGKILL. A reconstructed
controller left the live owner alone, then removed the immutable group and freed
capacity only after verified cleanup. Synthetic ledger cases cover foreign PID
namespaces, changed daemons, immutable helpers and uncertain intermediate limits.
The final normal-cleanup authority change passed all 28 unaffected cases; four
new tests required their missing workspace fixture field. The 19-case targeted
repair/API/visibility/index selection then passed, as did all five Settings
component cases and the production UI build. No unrelated suites were repeated.

## Focused checks

The final memory integration selection collected 126 Python cases: ten actual
OCI/guard cases, forty SQLite/PostgreSQL policy cases, ten Core API/Butler cases,
three boundary cases, six unsupported-launch cases, fifty-four sandbox cases and
three targeted product-index/Chroma cases. The run passed 124 and found two test
fixture errors; targeted repairs and reruns passed the remaining cases and the
restored original index assertions. All selected cases have passing results.
Four Settings component tests passed. Ruff and mypy checks passed for the touched
memory, API and runner modules. Production UI build and the Butler workflow
registry check passed with 601/601 UI call signatures covered.

Commands and test receipts are bounded in `.github/test-selection.json` during
each substep; no full-suite approval was requested or inferred. Real allocation
tests explicitly select `/usr/local/bin/docker`, the local disposable
`unix:///var/run/docker.sock` daemon and cached `postgres:16-alpine`. This rootful
fixture certifies its kernel observations, not production rootless isolation.
PostgreSQL uses only the disposable local fixture on port25432 with unique
temporary schemas. SQLite uses temporary database files.

## Production LAN journey

Origin: `http://172.26.91.238:19486`, disposable Core, synthetic projects and
credentials, normal paired-device authentication. Candidate production assets
were checked against the fixture's index SHA256. Installed Chromium154 was used;
the browser matrix is emulation, not physical-device testing.

Five committed Playwright journeys passed: desktop1440/1024 and Android Chromium
320/390/430. Each exercised pairing, policy save/readback, refresh, a conflicting
revision with retained draft, runner probing, an actual32MiB-contained allocation
failure while Core answered health, persisted failure after reload, axe, no
horizontal clipping, focus and44px control targets. Screenshots are in
`/tmp/nebula-memory-model-*.png` in the task workspace.

After charged-job ordering and bounded-page metadata, the rebuilt production UI
passed the same five LAN journeys again in 45.3 seconds. The final targeted
selection passed 19 Python cases in 21.66 seconds and five component cases.
The regenerated product index contains 405 passages with source digest
`d478668c788e…`; verification and real Chroma synchronization passed.

WebKit320/390/430 collected successfully but could not run. Both official browser
download domains returned403 `Domain forbidden`. No physical-device or packaged
desktop update/reopen evidence exists. These missing gates block an unqualified
end-to-end or deployment-continuity claim.

## Remaining integrations

### Resident runtime reserve follow-up

Resident API/execution process declarations now consume capacity within the Core
reserve in addition to the essential-service floor. Atomic startup reservation
shares the memory-policy lock with edits and compute admission. Preflight is
rechecked at reservation; expired leases retain capacity until verified cleanup.
Saving a smaller reserve than resident requirements is rejected. Older unaccounted
runtime records hold new compute without changing existing job limits. Fresh
architecture defaults can reserve overlap capacity; existing saved policy is
preserved. This is declared service admission accounting, separate from each
compute job's kernel enforcement.

The follow-up passed 26 selected SQLite/PostgreSQL backend cases, four
API/Butler/product-guidance cases, six memory component cases and one exact
Settings-icon coverage case. The Settings page displays the observed minimum and
resident reservation and prevents an invalid lower save. The production build and
601/601 Butler API signature inventory passed. Product knowledge now has 406
passages, source digest `718ceebdad8f…`; verification and real Chroma sync passed.

Five rebuilt production-LAN journeys passed in 49.5 seconds at
`http://172.26.91.238:19486`: desktop Chromium1440/1024 and emulated Android
Chromium320/390/430. They include a real retained API process under the same memory
policy (3 GiB Core reserve, 2.25 GiB observed minimum, 0.25 GiB runtime reservation)
and an actual 32 MiB-contained allocator OOM while Core health remains responsive.
Save/readback, conflict preservation, refresh, persisted failure, axe, focus,
44px targets and no horizontal clipping passed. The disposable listener, API
worker and compute groups were removed afterward; only the separately needed
disposable PostgreSQL fixture remains. Raw outputs are
`/tmp/nebula-runtime-memory-*`; screenshots remain `/tmp/nebula-memory-model-*.png`.

A collection mistake is retained in the evidence: the npm test wrapper treated
`list` as another file filter and ran 22 tests in three files before the corrected
selection. Twenty-one passed; catalog coverage exposed two absent icon mappings.
Fresh independent diagnosis confirmed both missing entries and then the selected
icon's absence from Nebula's curated Lucide alias. The exact glyph case and build
passed after using an existing curated icon. Subsequent collection used Vitest's
read-only listing command directly; executions used the guarded npm commands.
Benchmark source inventories were updated for the new memory helper; no benchmark
workload ran. WebKit and packaged desktop gates remain missing as described above.

### Runner adapters still required

Host commands/Notebook kernels, native vendor harness adapters, CPU/PDF runners,
raw SSH agents and interactive PTY adapters need protected memory-runner support.
Their covered entry points hold/reject before launching, with regression checks
that forbid unrestricted subprocess or thread fallback. They are not supported
compute paths merely because policy can be saved. Persistent Docker workers,
their commands and Docker Notebook children share their worker's frozen budget.

Core currently owns the recovery loop; the continuing deployment extraction must
move its ownership to the supervisor. Automatic cleanup requires a comparable
Linux PID namespace and the original Docker daemon ID. Hidden/foreign namespaces,
Podman without a certified agent, changed/unreachable runners and unacknowledged
creation stay unknown and charged. Deployment, desktop and code-rollback gates
remain part of the continuing architecture implementation.

## Host lifecycle integration

The versioned supervisor now owns lost-controller reconciliation independently of
execution workers. [Host-owned recovery evidence](host-memory-recovery.md)
records the real 32 MiB controller-crash cleanup, immutable runner checks,
close/drain barrier and 50 focused passing cases. This does not change the
remaining platform-enforcement or full desktop-continuity acceptance limits.
