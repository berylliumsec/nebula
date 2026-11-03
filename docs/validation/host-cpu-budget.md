# Per-host CPU budget

The CPU budget belongs to one Core host's local data directory. Projects and
connected browsers share it; another Core host has independent configuration.
Automatic uses half the detected affinity/cgroup CPU capacity. Custom accepts a
positive finite CPU-equivalent limit up to the host capacity. The active
experimental executor applies the quota and reports the observed limit. An
ordinary Core without that executor saves the policy and reports it inactive.
Saving a policy never silently enables the experimental executor.

| Journey | Invariant | Authority | Verification |
| --- | --- | --- | --- |
| Discover | Setup and Butler expose the host CPU budget and its actual application state | Core capability/settings catalog | UI, capability and product-help checks |
| Save | Save uses host identity and revision; over-capacity/invalid values are rejected | Host-local atomic configuration | Authenticated API and UI checks |
| Apply | Only an observed executor quota is called applied; failed application remains visible and retryable | Executor readback | Kernel-limited LAN fixture and API checks |
| Share/reconnect | Projects/browsers on the same host observe one policy; another host remains unchanged | Core local file | Independent Core roots, reload and restart |
| Resize | Raising capacity admits more work; lowering throttles existing work and drains extra slots without cancellation | Kernel quota and model scheduler | Held jobs and live 2/3-CPU checks |
| Unsupported | A host without the executor reports saved/inactive rather than claiming an enforced whole-Core CPU cap | Core status | UI/API regression |
| Failure/conflict | Stale revisions or a switched host cannot overwrite the current policy; pending/failed application is explicit | Core identity/revision/readback | API and component checks |
| Interface | Controls remain usable on desktop and mobile and survive refresh | Core plus browser presentation | Production bundle, LAN and selected browser matrix |

The setting limits the experimental shared background executor. It does not
establish a quota for execution paths that have not been integrated with that
executor. Native/frozen activation remains a separate experimental gate.

## Implementation and scope

Settings → Setup → CPU budget reads authenticated `GET /api/v1/host-cpu-budget`.
A save sends the observed `host_id`, `expected_revision` and nullable `cpu_limit`
to the same route with PUT, then reads Core again. The local file is replaced
atomically with mode 0600. Restart reapplies the saved choice when an executor is
connected; a reduced host capacity clamps the effective limit while preserving
the operator's choice. Invalid values, a different host and stale revisions do
not apply a change. Executor failure leaves a saved policy with a visible retry.

Interactive Butler discovers `nebula.work.host_cpu_budget`; scheduled global
Butler discovers `nebula.global.host_cpu_budget`. Both read the current Core.
The Settings API provides the revision-checked mutation and observed readback.
The existing fresh `/api/v1/host-load` capabilities remain available.

The owned resource model connects this policy to Docker's aggregate CPU quota
and its fair scheduler. It can grow the dispatch limit or reduce it while current
jobs finish without cancellation. The model keeps at most four compute processes;
the CPU quota controls aggregate execution time, not a worker per chat. This is
not a whole-Core or arbitrary external-process CPU cap. Ordinary Core startup
has no executor applier and explicitly reports `inactive`.

## Selected validation

The reviewed branch selection collects exactly 105 Python cases, 62 frontend
cases and one CPU Settings browser journey in each of eight permanent profiles.
The Python matrix is 3.11/3.12/3.13. No full suites, real providers, security tools,
GPU work or production deployment are selected. The policy adds 16 API/service/
Butler cases; the model adds held-job grow/drain and two startup-boundary cases.

Local feature checks passed: 29 initial API/model cases, the added minimum-limit
case, both startup regressions and all 62 frontend cases. Ruff lint/format,
the diagnostic audit, mypy over 233 Core files, the production UI build and the
Butler registry check passed (159 reviewed actions; 594/594 signatures linked).

The committed `host-cpu-budget.spec.ts` passed locally in desktop Chromium at
1440/1024 and Android Chromium emulation at 320/390/430. It uses the production
bundle at `http://172.26.91.252:54217`, normal pairing and Core authentication,
an isolated temporary database and an inert observable executor. The browser
creates its invitation through the fixture's loopback address and redeems it on
the LAN origin. The journey covers invalid input, observed save, reload,
concurrent-client conflict, draft retention, Automatic, failed application/retry,
inactive executor state, accessibility analysis and 44 px controls. Screenshots
were inspected at desktop and 320 px. All five passed in 38.0 seconds. Physical
devices were not used. All eight selected profiles subsequently passed in
[CI](https://github.com/berylliumsec/nebula/actions/runs/41732895398), including
WebKit iPhone emulation at 320/390/430. Its runtime is unavailable locally.

Local browser command (after adding only this owned LAN IP to the inherited
`NO_PROXY` and `no_proxy` lists for this command; external proxy routes preserved):

```sh
NEBULA_MODEL_TEST_HOST=172.26.91.252 NEBULA_MODEL_TEST_PORT=54217 \
NEBULA_TEST_PYTHON=/workspace/nebula/.venv/bin/python \
NEBULA_TEST_CHROMIUM_EXECUTABLE=/usr/bin/chromium \
npm --prefix ui run test:e2e -- tests/host-cpu-budget.spec.ts \
  --project model-desktop --project model-compact \
  --project model-chromium-320 --project model-chromium-390 \
  --project model-chromium-430 --workers 1 --max-failures 1
```

The [condensed receipts](host-cpu-budget-results.json) record the candidate bundle
hash, exact model source hashes, raw receipt hashes, kernel counters and cleanup.

The temporary isolated builder regenerated and verified 347 product passages
(655,538 bytes) from source digest
`62cb98ef697410a099e464e139b2106349ee3b8728cc31bd6057d58b0fdc50b5`.
Its [build receipt](https://github.com/berylliumsec/nebula/actions/runs/41732887104)
and uploaded `host-cpu-product-knowledge` artifact preserve that result. The
generated resource remains intentionally ignored in source control; package
builds generate it from maintained guidance. The focused Python CI selection
also verifies the current packaged digest, CPU-policy product search and actual
Core startup replacing a stale Chroma product collection with the current source.
The temporary builder workflow is removed from the final branch diff.

At candidate `e49d2e03f6ff723960a7e3bb0549e2b5fbc35bcc`, the selected
[backend/frontend CI](https://github.com/berylliumsec/nebula/actions/runs/41732895292)
passes 105 cases on each Python version and 62 frontend cases. The same run
checks lint, formatting, diagnostic classification, Core types and package
builds. The corresponding eight-profile browser run passes its required gate.
Final branch checks must also accept the updated diff-bound selection after
the receipts and temporary-workflow removal.

## Kernel and 50-chat check

The host exposes four CPU-equivalents through its outer cgroup. A four-chat
smoke saved a custom three-CPU policy through Core and observed
`cpu.max=300000 100000`; all eight CPU jobs and two PDFs completed. The follow-up
started 50 actual synthetic Core harness turns, each with 200 historical calls,
then released 200 jobs at 0.5 CPU-seconds each alongside four PDFs.

```sh
PYTHONPATH=src:. .venv/bin/python scripts/benchmarks/core_resource_budget.py \
  --host 172.26.91.252 --policy shared-budget --cpu-limit 3 \
  --chats 50 --calls 4 --history 200 --cpu-seconds 0.5 --pdfs 4 \
  --output /tmp/host-cpu-three-50.json
```

All 50 chats and four PDFs completed in 39.897 seconds. The observed quota was
three CPUs and at most three compute jobs executed together. During load, 1,306
ordinary reads had p95 27.516 ms, p99 48.202 ms and maximum 464.790 ms. There were
zero ordinary HTTP failures and zero polling retries. Durable tool pairs and
ordered replay passed; a fresh chat after drain completed in 0.191 seconds.
Queue, active-job and byte reservations returned to zero. Core exited with code
zero, and its owned container and synthetic data were removed. This smaller
workload is not a matched throughput comparison with the earlier 600-job runs.

## Causal checks during validation

Separate fresh-context validators checked each failure before its repair:

- `validate_cpu_settings_inventory`: the new GET/PUT signatures needed a reviewed
  registry action and inventory regeneration. The registry and build then passed.
- `validate_cpu_policy_startup`: a benchmark route named `host_load` replaced the
  capacity gate in a closure. Renaming that fixture callback restored startup;
  both policies now have isolated startup regressions and the kernel smoke passed.
- `validate_cpu_browser_probe`: no listener existed, but the inherited proxy
  returned 403 for the workspace IP and Playwright treated it as a live server.
  A command-scoped bypass for that exact owned local IP restored direct probes.
- `validate_cpu_pairing_origin`: invitation creation correctly rejects LAN
  clients. The test now creates it through loopback, checks the response, and
  retains the real LAN browser redemption and Settings journey. Product pairing
  policy was not changed.
