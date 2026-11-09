# Integration diagnostic boundary review

Reviewed baseline: `3f67ca70` (the same 78 Python findings reproduced on
`cea8b38e`). The command was
`python scripts/audit_diagnostic_blind_spots.py --python --rust`.
It reported 57 handlers and 21 task creations across 32 files. There were no
Rust findings. The audit recognizes local syntax, not structured protocol
results, later aggregate raises, or explicit task ownership.

## Acceptance contract

| Journey step | Observable invariant | State authority | Evidence layer |
| --- | --- | --- | --- |
| Failure | Unexpected worker, forwarding, and host-effect failures leave a safe local diagnostic | Diagnostic manager | Focused Python regression |
| Stream/interrupt | Expected close, end-of-stream, and cancellation preserve current transport cleanup | Owned tasks and connection state | Focused Python regression |
| Refresh/reconnect | A host effect still becomes uncertain after a lost result and is never replayed by this repair | Runtime command row | Focused Python regression |
| Credential/content privacy | Diagnostics contain fixed messages and exception types/stack locations, never exception messages or inputs | Diagnostic manager allowlist | Focused Python regression |
| Entry/create/select/delete | No controls, routes, saved entities, or authorization contracts are added | Existing Core/UI owners | Not applicable to diagnostic classification |

Independent review and the integrating owner confirmed these causal faults
before implementation:

* `execution_worker.ProjectExecution.watch_lease` catches every `Exception`,
  fences the project, and discards the cause. A genuine database/runtime failure
  is indistinguishable from ordinary lease loss. Keep fencing; record unexpected
  failures separately from the expected `LeaseLost` branch.
* `runtime_http.RuntimeHttpProxy._forward_websocket` waits for a pump to finish,
  then discards every `gather(return_exceptions=True)` value. An unexpected pump
  exception never reaches the enclosing transport catches. Keep both pumps joined
  and the close response; classify their returned failures.
* `runtime_host_delivery.HostEffectDelivery._run` converts all failures into an
  uncertain receipt and discards the cause. Its done callback also retrieves and
  discards a failure of uncertainty persistence. Record the original failure and
  supervise the accepted task without changing receipt or replay semantics.
* `remote_knowledge.RemoteKnowledgeIndex._background` consumes and discards task
  exceptions in its done callback. This uses `loop.create_task`, which the audit
  does not recognize. Supervise that background task as well.

`runtime_host_services` also owns a listener task that propagates a failure only
during startup or eventual close. Supervise its completion to make an unexpected
failure observable during the service's lifetime.

## Reviewed expected boundaries

The implementation uses individual `diagnostic-expected:` rationales at these
sites, without modifying audit rules or adding blanket exemptions.

| Boundary | Sites | Why no failure diagnostic is needed |
| --- | --- | --- |
| Absent/revoked authority or optional linked data | `authentication:97`, `chat:11725`, `runtime_delivery:406`, `runtime_host_settings:159`, `runtime_global_reads:140,164` | Exact `NotFoundError` becomes denial, filtering, or an explicit unavailable dashboard state; database failures still propagate. |
| Input probes and untrusted display/history data | `api_worker:416`, `authentication_api:253`, `runtime_browser_bridge:164`, `runtime_http:204`, `provider_tool_manifest:226`, `runtime_global_settings:134` | Invalid input fails closed, is validated downstream, or sets the explicit partial display flag. |
| Optional files and concurrent publication | `harness_instructions:63`, `runtime_launcher:96,134`, `runtime_service:49`, `runtime_vault:76,140` | Missing candidates/markers are normal; exclusive publication races and idempotent discard are deliberate. |
| Capability and readiness probes | `remote_knowledge:99,139`, `runtime_bootstrap:65`, `runtime_integration_credentials:185`, `runtime_launcher:288`, `runtime_processes:183,520,593,728` | No running loop selects the synchronous path; bounded polling, nonblocking I/O, missing availability, and lost shutdown acknowledgments have explicit owners and outcomes. |
| Expected ownership and retained blockers | `execution_worker:230,407,442,527`, `runtime_project_handoff:212`, `runtime_processes:675` | Expected lease loss fences admission; quiescence, handoff, and reconciliation return or persist actionable blockers. |
| Structured transport errors | `runtime_host_services:226`, `runtime_http:431,463,566,574` | Known local connection/authentication/capacity failures become bounded 503/close responses. Never copy private addresses, headers, or exception messages. |
| Structured domain errors over private RPC | `runtime_memory_control:178,184`, `shared_memory_rpc:116,123`, `shared_knowledge_rpc:260,394,400,406,412` | Domain denial/validation/stale-index conditions become typed errors or filter stale hits. Remote adapters raise domain errors from the response; arbitrary exceptions still escape. |
| Ordinary completion and timer wakeups | `runtime_chat_stream:225`, `runtime_memory_recovery:90` | Iterator completion and an expired polling interval are expected control flow. |
| Deferred cleanup propagation | `runtime_processes:548,553,557,561`, `supervisor:701,717` | Every caught exception enters a list that is subsequently raised as a `BaseExceptionGroup`; cleanup continues so all resources are attempted. |

Line numbers in this table identify the pre-repair findings, not permanent anchors.

The other task findings have explicit ownership: capacity acquisition is shielded
and drained; close tasks are shielded and joined by `finish_cleanup`; SSE results
are read through `pending.result()`; heartbeat failure requests server exit and is
awaited; execution/admission/handoff/recovery loops already catch and record their
unexpected operation failures. Event waiters are cancelled and joined. These
sites retain their ownership model and receive individual explanatory annotations.

## Privacy and scope

Pydantic validation can contain memory candidates, knowledge chunks, model
metadata, settings bodies, or credential references. Native harness readiness
reads instruction text and private account paths. Startup/bootstrap paths own
database URLs and administration credentials. Raw exception text, request data,
headers, commands, native artifacts, and private startup output must not be logged.
The repair uses `record_caught_exception` and `create_diagnostic_task`, whose safe
exception serialization omits messages and locals, with fixed event messages.

No full suites, providers, operational commands, deployment, or real data are
in scope. Browser/LAN product acceptance is not supplied by this static audit or
the isolated regression tests; final integrated workflow acceptance remains a
separate gate owned by the integrating agent.

## Local validation

Worktree: `/workspace/nebula-integration-diagnostics`, branch
`codex/integration-diagnostics`, based on `3f67ca70`.

* `PYTHONPATH=src /workspace/nebula/.venv/bin/python -m pytest --collect-only -q tests/v3/test_runtime_diagnostic_boundaries.py`:
  11 cases collected.
* `PYTHONPATH=src /workspace/nebula/.venv/bin/python -m pytest -q tests/v3/test_runtime_diagnostic_boundaries.py`:
  11 passed in 2.80 seconds. Coverage includes both pump directions, normal
  disconnect/transport/cancellation, persisted uncertainty without replay,
  uncertainty-write failure, remote background correlation, and unexpected
  lease-monitor failure versus normal lease loss. Each failure check verifies
  private canary text is absent from the diagnostic error records.
* `/workspace/nebula/.venv/bin/python scripts/audit_diagnostic_blind_spots.py --python --rust`:
  zero unclassified handlers or ignored results; the audit implementation is
  unchanged.
* Focused Ruff checks/format validation and `git diff --check`: passed.
* Focused Mypy on the five source files with behavioral AST changes: passed.
  The repository configuration does not check every untyped function body.
* AST comparison against the baseline confirmed the remaining 27 changed source
  files contain annotations only.

No real Core, production/LAN origin, browser engines, emulated mobile profiles,
or physical devices were exercised. This repair is locally verified for
diagnostic classification; integrated operator journey acceptance is still
required before an unqualified product-completion claim.
