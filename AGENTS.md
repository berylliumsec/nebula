# Nebula agent guidance

## Temporary operator override for command access

On 2026-11-10 the operator explicitly authorized a temporary bypass of the
`memory-runner/v1` admission hold for Host and SSH chat commands in the Apple Bug
Bounty project while a protected runner is developed. Core may enable this only
by setting `NEBULA_TEMP_UNMANAGED_COMMAND_PROJECT_IDS` to that project's exact ID.
The default remains held; do not extend the exception to other projects or to
other compute paths. Record enablement and removal in the deployment receipt.
This override is authorization, not evidence of memory containment or cleanup.

Read and follow these repository rules before working in Nebula:

- [Branch, validation, and PR workflow](.codex/rules/change-workflow.md) for every change,
  including the standing bypass when the public commit guard's Codex review is unavailable.
- [Focused test selection](.codex/rules/test-selection.md) before running tests or opening a PR. The detailed procedure is in [docs/TEST_SELECTION.md](docs/TEST_SELECTION.md).
- [Operator workflow rules](.codex/rules/operator-workflows.md) when changing a feature an operator uses.
- [Code troubleshooting](.agents/skills/code-troubleshooting/SKILL.md) for defects, regressions, and failing behavior. Identify the root cause and have a separate subagent validate it before attempting a fix.

For any operator-visible interface, mobile, LAN, chat/session lifecycle, provider,
harness, workspace, or API-to-UI change, read and follow the
[nebula-product-quality skill](.agents/skills/nebula-product-quality/SKILL.md)
**before editing**, including for fixes and reviews. Apply the
[interface principles](docs/design/interface-principles.md) to touched controls.
The skill and its quality-gates reference define the acceptance evidence. If a
required gate is unavailable, report the work as incomplete or partially verified
and name the missing evidence. Never claim an untested workflow is complete.

When adding an operator feature or changing a setting, update the dedicated
Nebula settings assistant's destinations, action discovery, and product guidance
in the same change. Update `ui/src/settingsCatalog.ts` and
`docs/design/nebula-settings-assistant.md` where applicable, regenerate and verify
the bundled product knowledge index, and verify that Core synchronizes its current
source digest into the Chroma product-help collection. Add a focused regression
check so the assistant can discover and use the feature or setting. Do not call a
browser action saved until Core or the device reports the observed result.

## Choose the Core deployment path

Before promoting a release, verify the live deployment mode, exact source
release, authenticated `/api/v1/deployment/status`, and installed service
configuration. A source checkout or `deployment_mode=supervisor` alone does not
establish that the running processes can survive a rolling deployment.

For a compatible supervisor-mode promotion, ordinary provider chat turns do not
need to stop or pause before deployment starts. The deployer replaces only the
foreground supervisor, preserves execution workers, promotes the API, and then
requests per-project handoffs. A source worker lets admitted provider turns
finish, holds new root admissions, and parks queued turns for restoration. The
project can remain `finishing` until its admitted turns end; do not cancel a
turn to force transfer. Before host activation, verify that the *deployed source*
supports retained workers and bounded host-service reconnect, the service uses
`KillMode=process` and `ExitType=main`, migrations are additive and release
manifests compatible, and both execution versions support project handoff and
any participant restoration that the project requires.

A live harness or vendor session, mission, terminal, notebook, debug session,
unconfirmed effect, or other non-restorable resource can keep a project on its
old worker during handoff. The operator's goal is that a deployment never stops
on such a project. The deployer gives each source worker a grace period
(`HANDOFF_GRACE_SECONDS`, 180 s) to reach a safe handoff boundary. After that it
stops the source with the `evict_execution` operation, which is a planned crash.
Core's expired-owner recovery then settles what the worker left: it releases
claims, marks unfinished turns and commands interrupted, closes resident
handles, records effects as possibly applied, and replays nothing. Core then
restores the projects on the target version, and the deployer adopts that
recovery from the `recovery` field in deployment status. Expect interrupted
long commands, terminals, notebook kernels and vendor sessions, and goals that
may repeat a step. Never record an effect as not having happened. The
deployer may still return `waiting_for_handoffs` (for example, when recovery
does not verify within `HANDOFF_RECOVERY_WAIT_SECONDS`) or
`api_promoted_execution_staged`; these are partial promotions. Read Core's project phases, owners, resident versions, and
blockers before claiming completion. A third execution version can remain
staged until an older one retires. If the public listener is not held across
the supervisor swap, report the client connection gap and verify stream
reconnect and durable turn state before claiming viewer continuity.

### Hold projects for a stop or required project wind-down

Use this procedure for a legacy or whole-Core stop, a non-additive migration,
the first promotion from a source lacking the rolling safeguards above, or a
pinned project whose old execution version must retire. Core enforces the
stop itself with a project hold (`docs/design/project-holds.md`); do not rely
on asking Butler or agents to wind down, which does not hold: subagent
reports and Butler's own checks keep starting work.

Place the holds with the deployer: `python3 -m nebula_deployer hold --reason
"<why>"` holds every project that has an execution worker, or pass
`--project <id>` once per affected project. `--grace-seconds` (default 180)
is how long running turns may finish before Core stops them. The command
returns once no held project has chat work, and saves each hold in
`project-holds.json` beside the deployer state. A host restart for older
workers places and releases its own holds automatically.

While a project is held, no chat turn starts from any source, its running
goals are paused with the hold's mark, and only that hold's release resumes
them. Before stopping or retiring anything, read `GET
/api/v1/engagements/{project}/hold` for each project and verify `held` with no
active turns. Recheck at the stop boundary. Record each hold's
`paused_goals`, `interrupted_turns` and `stopped_subagents` in the deployment
record; they come from Core, not from an agent's summary. Goals that were
paused before the hold are not marked and are never resumed by it.

After the new or restored Core passes deployment health checks, or the
affected project's target owner is verified, run `python3 -m nebula_deployer
release-holds --reason "<why>"`. It resumes exactly the goals each hold
paused and reports `restored_goals`. A goal whose budget is spent, or that
cannot resume for another reason, stays paused without the hold's mark;
report it for operator resolution. A release Core did not confirm stays in
`project-holds.json`; run the command again. Then verify in Core that the
restored goals run. Never put Core authentication secrets in the deployment
record or PR.

Cores older than project holds (before #1232) answer the hold route with 404,
and the deployer refuses `hold` on them. For those only, find or create each
affected project's Butler conversation through authenticated `GET` or `POST`
`/api/v1/chat/control-assistant/session`, ask Butler through `POST
/api/v1/chat/completions` with `control_assistant: true` to pause the
project's goals and queues, verify the pause in Core, and ask Butler to
resume exactly that work afterwards. Do not substitute stop, cancel, or
approval for pause, and do not treat Butler's acknowledgement alone as proof
of state change.

Take a deployment backup only when the operator explicitly asks for one
(`promote --backup` or `cycle --backup`); never take one on your own initiative.
After a healthy legacy promotion that created a coherent rollback snapshot,
keep only the current snapshot under `rollback-backups`. Verify the retained
path matches the deployer state, record the removed snapshots, and report any
incomplete cleanup. Supervisor-mode code rollback preserves live data; never
restore a snapshot over continuing work. Never delete the current rollback
snapshot or prune during an unverified cutover.
