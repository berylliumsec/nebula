# GUI activation of host CPU workers

Entry: Settings → Setup → CPU budget. Core owns the host-local enabled flag,
revision, worker processes, routing and observed affinity. The browser owns only
an unsaved draft; saving requires Core readback. The default remains disabled.

| Journey | Observable invariant | Authority | Evidence |
| --- | --- | --- | --- |
| Discover | The UI and Butler expose activation and its PDF-only scope | Core capabilities and Settings catalog | Component, discovery and product-help checks |
| Enable | A saved enable request starts real owned processes and reports running only after acknowledgement and affinity readback | Core worker lifecycle and kernel affinity | Real-Core API, LAN browser and frozen Linux Core |
| Configure | Whole worker CPUs are bounded by available host capacity and the four-worker pool limit; Automatic uses roughly half with a minimum of one worker | Host capacity and kernel affinity | API and real process checks |
| Use | Report PDF work runs in the pool; Core retains durable snapshots, callbacks and artifacts | Report service and owned worker pool | PDF artifact/replay and CPU load checks |
| Resize | The old pool stops admitting work, applies a reduced affinity, drains accepted jobs, exits, then the replacement starts | Core manager | Held jobs and live load |
| Disable | Accepted jobs finish, processes exit, and the UI reports disabled only after readback | Core manager | Held jobs, process absence and browser |
| Restart | The saved enabled policy restores real workers without spawning another Core | Host-local policy and freezer entry point | Source and frozen Core restart checks |
| Failure/retry | Startup or affinity failures remain visible and retryable; stale revisions cannot overwrite a policy | Core lifecycle and host revision | API and browser failure cases |
| Unsupported | A host without Linux CPU affinity, or without the default report renderer, explains why activation is unavailable | Core capability | API/component cases |
| Interface | Controls are usable at desktop and mobile widths and survive reload | Browser and Core | Production bundle, LAN, Chromium and WebKit matrix |

This activates the real PDF execution path. Native harnesses, arbitrary external
tool processes, LLMs and GPUs are outside this pool. CPU settings use whole
workers and observed Linux affinity, not a fractional kernel bandwidth quota.
The existing externally injected benchmark quota backend remains independently
supported and cannot be activated through this new control.

## Behavior and ownership

The normal Core app now supplies the report service with `ReportCpuExecutor`.
`enabled` is stored beside the CPU policy, defaulting to disabled for existing
files. Omitting it on a budget update preserves its value. Core serializes the
requested transitions without holding the Settings request open while work
drains. New submissions during a transition receive an explicit retryable
failure; already admitted work finishes in its original pool. A lower limit
repins the old workers first. The old pool exits before a replacement starts.
Core reads live worker PIDs and their kernel affinity before calling it running.

When workers are disabled, PDFs retain their standard thread execution path.
Its actual work stays tracked after caller cancellation. Enabling waits for
those threads before starting a process pool. Startup failure remains visible;
saving again retries, or disabling returns to standard PDF generation. Renderer
shutdown is registered before restoring an enabled policy, so later Core startup
failure also removes restored workers. The frozen entry point calls
`multiprocessing.freeze_support()` before dispatching the CLI or MCP shim.

The pool accepts at most the worker count plus six queued jobs, with an 8 MiB
per-snapshot and 32 MiB aggregate snapshot bound. These are admission limits,
not a total process-memory quota. Whole CPU affinity is separate from the
fractional cgroup quota used in the earlier synthetic shared-executor model.
Externally injected quota callbacks retain their existing budget behavior and
explicitly do not offer Core-managed activation.

## Selected verification

The final selection collects 73 Python cases, 70 frontend cases and eight
browser cases. Python runs on 3.11/3.12/3.13 in CI. No full suites, real provider
turns, external tools, GPU work or production deployment are selected. The
conditional frozen-Linux CI job builds the actual Core executable and runs
exactly one owned activation/PDF/resize/restart/disable journey; native Rust
suites are not selected.

Local evidence:

- The first 61 API/worker/report cases passed. The added Butler activation/readback
  case passed. Two legacy-thread supervision/shutdown regressions and the
  existing legacy transition case passed; the later-startup cleanup case passed
  after correcting its fixture callback. These form 65 distinct feature cases.
  The additional affinity-read failure/recovery case and three exact catalog,
  operator-help and entry-shim cases passed after workspace reconnection. Four
  selected packaged-source/search/Chroma cases run in CI with regenerated guidance.
- All 70 distinct frontend cases passed across the four scoped files; the final
  CPU component file passed all 16 cases after adding unknown-count recovery.
- Production UI build, Ruff, diagnostic classification and mypy over 234 Core
  files passed. The Butler registry links 594/594 signatures with no gaps.
- The committed production-bundle LAN browser journey passed in five Chromium
  profiles at `http://172.26.91.252:54217`: desktop 1440/1024 and Android
  emulation 320/390/430. It verifies normal pairing, explicit activation, real
  distinct PIDs/affinity, PDF export, reload, revision conflict, held-job drain,
  failed start/retry, unsupported host, accessibility and 44 px controls.
  Screenshots were inspected at 1440 and 320. The five cases passed in 1.8 minutes.
  Three permanent WebKit iPhone profiles are selected in CI; physical devices
  were unavailable.
- The source CLI smoke passed enable, PDF, resize 1→2, restart restoring two
  workers, and disable to zero. It removes its temporary data and records the
  controlled SIGINT exit code 130. The frozen-binary smoke remains a separate
  required CI result; source CLI success is not substituted for it.

Commands:

```sh
PYTHONPATH=src:. .venv/bin/python scripts/benchmarks/core_worker_activation_smoke.py \
  --binary .venv/bin/nebula-core --output /tmp/cpu-activation-source-cli.json
```

The browser invocation uses `tests/host-cpu-budget.spec.ts` with exactly
`model-desktop`, `model-compact`, `model-chromium-320`, `model-chromium-390` and
`model-chromium-430`, one worker and no retries. The owned LAN IP is added to the
inherited command-local proxy bypass lists while external proxy routes remain
unchanged. CI adds the three selected WebKit profiles.

## Heavy load

```sh
PYTHONPATH=src:. .venv/bin/python scripts/benchmarks/core_worker_activation_load.py \
  --host 172.26.91.252 --paragraphs 2500 --output /tmp/cpu-activation-load-50.json
```

The owned Core receives 50 concurrent PDF export requests, with a large saved
source and 50 inert saved chat rows. No chat runtime is started. The initial
three-worker pool admits nine exports and explicitly rejects 41 with queue-full
failures. The driver lowers the CPU setting to one while work drains, verifies
the replacement, then disables during one additional real PDF export.

All nine admitted burst exports and the additional export complete. The load
phase takes 208.154 seconds, including the deliberately lower one-CPU limit.
The 12,486 ordinary reads have p95 8.739 ms, p99 11.934 ms and maximum 599.826 ms,
with zero HTTP failures or probe retries. Admission peaks at nine jobs and
snapshot reservations stay below 32 MiB. Samples show the old three-worker
pool sharing one CPU while draining. The final disabled state has zero workers;
the owned Core exits with code zero and all synthetic data is removed. This is
an overload/transition check, not a matched throughput comparison or evidence
that arbitrary chat/tool workloads are capped.

[Condensed receipts](gui-cpu-worker-activation-results.json) retain source and
bundle hashes, raw-receipt hashes, counts, observed limits and cleanup.

## Independent causal checks

`validate_activation_task_supervision` confirmed that a cancelled legacy caller
could leave a later thread exception unrecorded, and that cancelling a transition
could cancel its gathered task while the underlying thread still ran. Supervised
tasks plus shielding preserve both diagnostics and actual-work ownership; focused
held-thread regressions verify each case. `validate_activation_startup_fixture`
confirmed an incorrect `start` fixture target before any Core startup; correcting
it to `startup` permits the real cleanup check. `validate_activation_load_seed`
confirmed the initial load attempt failed before activation because its inert
saved chats omitted required model/provider fields. Its trace is preserved and
excluded from measured load; product model validation remains intact.

`validate_activation_unknown_counts` independently reproduced an affinity-read
failure with one live worker, one active job and one queued job. The API
previously returned fabricated zero counts while the same PID remained alive.
Unknown counts, PIDs and affinities now return null; the GUI displays worker
counts unavailable. Focused API and component cases verify failure and recovery.
