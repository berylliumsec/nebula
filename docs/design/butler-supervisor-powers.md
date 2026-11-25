# Butler supervisor powers

Status: implemented behind an operator kill switch. Off by default.

The operator asked for Butler to take over the job of the external
supervisor that keeps many long-running goals moving. That
job needs more than Core's goal API:

- host commands;
- SSH to lab hosts;
- process signals;
- runner restarts;
- a repository publish flow;
- cross-goal repository forensics;
- independent closure review;
- deterministic goal hygiene.

This change adds those powers as audited Butler capabilities. It keeps the
safety rules in code, not in prompt text.

## Operation names

All of these are reached through Butler's existing gateway
(`nebula.capabilities.search/describe/read/change`).

| Operation | Effect |
|---|---|
| `nebula.supervisor.shell` | Runs one command on an allowlisted lab host: the Core host, or a lab host over SSH. |
| `nebula.supervisor.signal` | Sends one signal to one process. The process's command line is re-checked at signal time. |
| `nebula.supervisor.runner_control` | Starts, stops or restarts the runner service, or starts or stops the ingress service. It runs `sudo -n <script> <verb>` with the operator's configured control scripts. |
| `nebula.supervisor.repo_publish` | Runs the operator's configured prepare or publish script. |
| `nebula.supervisor.run_gate` | Runs one deterministic gate script. The gate must be committed and unmodified, and sit under an allowlisted root. It returns `gate.passed`. |
| `nebula.supervisor.repo_audit` | Finds three kinds of commit: wide commits, foreign deletes, and stale-blob reverts of paths outside the committing goal's project. |
| `nebula.supervisor.repo_restore` | Restores exact paths from a known-good commit and commits only those paths. The repository's hooks run, and nothing is pushed. |
| `nebula.supervisor.independent_review` | Core runs a fresh closure review on a reviewer model that differs from the goal's own model, then records and applies the verdict. |
| `nebula.supervisor.hygiene_tick` | Runs the deterministic goal hygiene: null budgets, resume after transient pauses, and staged Work items. |
| `nebula.supervisor.must_do_resolve` | Closes one review must-do with a verified artifact: a commit `git cat-file` confirms, or a file under the evidence roots. Butler cannot waive. |
| `nebula.supervisor.recover_stuck_turn` | Lists goal turns with no progress past the threshold, and cancels then resumes one as a single action. |
| `nebula.supervisor.post_goal_note` | Posts a supervisor note with a release condition, optionally restarting the goal's turn so the note takes effect at once. |
| `nebula.supervisor.probe` | Runs one fixed, read-only probe on an allowlisted host, or `verify_blocker` to test a goal's claimed blocker. |
| `nebula.supervisor.tmp_cleanup` | Finds idle full-repository exports under the cleanup roots; deletes only after a dry run. |
| `nebula.supervisor.log_writer_status` | Reads the health of Core's file log writer. |
| `nebula.supervisor.request_operator_action`, `list_operator_actions`, `close_operator_action` | Raise, list and close typed requests for things only the operator can do. |
| `nebula.supervisor.route_goal_runtime` | Routes one goal's runtime under the operator's stage-based routing policy: a route name, `auto` or `release`, with `dry_run`. See [Stage-based runtime routing](#stage-based-runtime-routing). |

**Where the tools are available:**
- Interactive project Butler.
- The global periodic check.
- Not on the home observer.

They run only when the switch is on, and each call re-reads the policy. Under the global
periodic check, the hygiene tick runs in code before the model case starts.

## Configuration (operator)

All configuration is through environment variables in Core's service environment.

| Variable | Meaning |
|---|---|
| `NEBULA_BUTLER_SUPERVISOR_ENABLED` | `1` turns the tools on. Clearing it is the **kill switch**: tools refuse on their next call, with no restart. |
| `NEBULA_BUTLER_SUPERVISOR_HOSTS` | A JSON allowlist, for example `{"core":{"local":true},"responder":{"address":"…","user":"…","identity_file":"~/.ssh/…"}}`. Add `"rf_lab":true` only for hosts inside the shielded lab. |
| `NEBULA_BUTLER_SUPERVISOR_ALLOW_SUDO` | `1` allows general `sudo` in `shell`. **On a host where the Core user has `NOPASSWD: ALL`, this is unrestricted root.** It is off by default, and `runner_control` does not need it. |
| `NEBULA_BUTLER_SUPERVISOR_AUDIT_LOG` | The audit log path. Default: `$NEBULA_V3_DATA_DIR/butler-supervisor-audit.jsonl`, mode 0600. |
| `NEBULA_BUTLER_SUPERVISOR_PROTECTED_PATHS` | Pull-only paths, such as a production mirror checkout. Default: none. |
| `NEBULA_BUTLER_SUPERVISOR_OPERATOR_ONLY_PATTERNS` | Extra operator-only regular expressions, one per line, such as a vendor's report or account endpoints. A malformed pattern fails closed. |
| `NEBULA_BUTLER_SUPERVISOR_PRIVATE_STRINGS` | Comma-separated strings, such as the operator's email address. A command containing one is refused, and the string is redacted from output and the log. |
| `NEBULA_BUTLER_SUPERVISOR_EXTERNAL_HOSTS` | Non-lab hosts that network tools may reach. Default: `github.com`. |
| `NEBULA_BUTLER_SUPERVISOR_RUNNER_CONTROL`, `…_INGRESS_CONTROL` | The sudoers control scripts. Without them, `runner_control` refuses. |
| `NEBULA_BUTLER_SUPERVISOR_PUBLISH_SCRIPT`, `…_PREPARE_SCRIPT` | The repository publish scripts. |
| `NEBULA_BUTLER_SUPERVISOR_GATE_ROOTS` | Git checkouts whose committed scripts may run as gates. |
| `NEBULA_BUTLER_SUPERVISOR_FORENSICS_REPOS` | Repositories that `repo_audit` and `repo_restore` may read and write. |
| `NEBULA_BUTLER_REVIEW_PROVIDER_ID` | A provider profile for the independent reviewer. Set this or the harness variable below, not both. |
| `NEBULA_BUTLER_REVIEW_HARNESS_PROFILE_ID` | A harness profile for the independent reviewer, such as a Claude harness on the host's existing login. Each review is one turn in a fresh session with no tools, no MCP server and no Core gateway. |
| `NEBULA_BUTLER_REVIEW_MODEL` | The reviewer model. Default: `claude-opus-5-5`. It must differ from the reviewed goal's model. |
| `NEBULA_BUTLER_REVIEW_CEILING_POLICY` | A file appended to the reviewer instructions, such as an impact-ceiling policy. |
| `NEBULA_BUTLER_REVIEW_EVIDENCE_ROOTS` | Directories the reviewer may read evidence bytes from. |
| `NEBULA_BUTLER_HYGIENE_PROJECT_IDS` | Projects the hygiene tick covers. |
| `NEBULA_BUTLER_HYGIENE_UNLIMITED_BUDGETS` | `1` nulls the token, time and step budgets of live goals in those projects. |
| `NEBULA_BUTLER_HYGIENE_DEFAULT_STAGE` | The stage given to a newly created Work item. Default: `triage`. |
| `NEBULA_BUTLER_CLOSURE_REQUESTS_PER_DAY` | Closure requests a goal may make per rolling day. Default: 3. |
| `NEBULA_BUTLER_REVIEW_EVIDENCE_REPOS` | Repositories in which a must-do commit artifact is verified. Default: the forensics repositories. |
| `NEBULA_BUTLER_STUCK_TURN_MINUTES` | Minutes without progress before a goal turn counts as stuck. Default 12, clamped to 10–60. |
| `NEBULA_BUTLER_STUCK_TURN_AUTO` | `1` lets the periodic check cancel and resume stuck turns (at most 3 per goal per hour). |
| `NEBULA_BUTLER_NOTE_RELEASE_ROOTS` | Extra roots an `until_path_exists` release condition may name. |
| `NEBULA_FABRICATION_GUARD_REPOS` | JSON map of project id (or `*`) to repositories in which cited commit hashes must resolve. |
| `NEBULA_FABRICATION_GUARD_MODE` | `off`, `flag` (default) or `reject` for agent Work check-ins citing unknown hashes or forged sign-offs. |
| `NEBULA_LOG_HEARTBEAT_SECONDS` | Log-writer heartbeat interval. Default 300; `0` turns it off. |
| `NEBULA_BUTLER_SUPERVISOR_LEASE_STATE_COMMAND`, `…_LEASE_STATE_HOST` | Read-only command, and host, for the `lease_state` probe. |
| `NEBULA_BUTLER_SUPERVISOR_HEALTH_PATHS` | Paths `host_health` checks. Default `/`. |
| `NEBULA_BUTLER_SUPERVISOR_CLEANUP_ROOTS`, `…_CLEANUP_HOST`, `…_CLEANUP_MIN_BYTES`, `…_CLEANUP_IDLE_MINUTES` | Where `tmp_cleanup` may look, the size that marks a repository export (default 2 GiB) and the idle limit (default 20 minutes). Without roots it refuses. |

The check interval is unchanged and already configurable: `GET` and `PUT
/api/v1/butler/check-interval`, from 1 to 1440 minutes.

## Guardrails enforced in code

All of these are tested in `tests/v3/test_butler_supervisor_powers.py`.

- **Lab hosts only.**
  - Unknown hosts are refused.
  - Network tools (curl, ssh, nmap, ping, git push and others) may name only lab
    addresses or the configured external hosts.
  - Radio tools are refused on any host that is not `rf_lab`.
- **Production mirror is pull-only.**
  - Commands that mention a protected path, or run with a working directory inside it, must be read-only.
  - Allowed: read-only git subcommands, plus `cat`, `ls`, `sed -n` and similar.
  - Refused: writes, redirections, `sed -i`, and git commit, push, reset and checkout.
  - The forensics restore refuses protected paths.
- **Operator-only actions are refused.** These are:
  - platform integrity settings (`csrutil`);
  - firmware and boot (`nvram`, `bless`, restore and DFU tools);
  - hardware power (`reboot`, `shutdown`, `uhubctl`);
  - account and credential changes (`sysadminctl`, `dscl` password changes, keychain password writes);
  - submitting reports: mail senders, `gh issue/pr/release/gist/api`, and any upload (`curl -d/-F/-T`, `-X POST`, `wget --post`, `scp`, `rsync`) to a host outside the lab, even an allowed external host;
  - anything matching the operator's configured patterns.
- **No self-approval.**
  - `ControlAssistantWork.goal_action` refuses Butler's `complete` on a goal with
    `completion_requires_review` unless Core holds an independent review that:
    - was written by Core's reviewer;
    - is bound to a digest of the current completion proposal;
    - has an approving verdict;
    - was made by a model other than the goal's own.
  - Butler has no route that writes goal metadata, so it cannot forge a review.
  - The operator's own route is unchanged.
- **Private data.** Configured private strings may not appear in a command, and
  are redacted from returned output and the audit log. An email address in a
  push or HTTP call to an external host is refused.
- **No git worktrees.** `git worktree add` and `git worktree move` are refused.
- **No obfuscation.** These are refused:
  - piping decoded text into a shell (`base64 -d | sh`);
  - `eval`;
  - a trailing pipe into a shell.

Screening shell text is a guardrail, not a sandbox. A determined model could
still find a form the screen does not recognise. The durable audit log records
every attempt, and the kill switch stops all tools at once.

## Audit

Every call appends one JSON line to the audit log, whether it ran, timed out or
was refused. The line holds:

- the actor (the Butler case or turn);
- the project, tool and host;
- the redacted command and the reason;
- the outcome, exit code and duration;
- the stdout and stderr SHA-256 digests and byte counts.

Host operations also append a `butler.supervisor_action` event to the project's
Butler event stream.

## Independent review flow

1. Butler calls `nebula.supervisor.independent_review` with the goal's session
   and its evidence paths. Evidence cited in the held proposal is added automatically.
2. Core checks two things. The reviewer model must differ from the goal's model.
   The reviewer profile must be enabled, allowlisted (on a harness: offer the
   model after **Check**), and permit sensitive data.
3. Core sends one tool-free request. On a harness profile this is one turn in
   a new session: an empty scratch directory, every native capability off, no
   MCP server or Core gateway, every permission request denied, and any tool
   event ends the review with an error. It contains:
   - the completion criteria;
   - the proposal;
   - the evidence bytes, read only from the evidence roots, bounded and redacted;
   - the reviewer instructions, including the ceiling policy and the MUST-DO prompt.

   Butler's conversation and notes are not included.
4. The verdict (ACCEPT, ACCEPT_WITH_RESIDUALS or REJECT) is recorded in goal
   metadata, together with the packet digest and the reviewer identity: the
   provider id or the harness profile id, the model and the review id.
5. The verdict is applied:
   - ACCEPT completes the goal.
   - REJECT adds the must-dos as a supervisor note and resumes the goal.
   - ACCEPT_WITH_RESIDUALS completes the goal and starts a follow-up goal with
     null budgets and `completion_requires_review`, or, with `on_residuals=feed`,
     feeds the residuals back to the goal instead.

## Enabling (operator steps)

1. Create the reviewer runtime: a Claude harness on the host's existing login
   (see "Running Butler on Claude"), or an Anthropic provider profile allowing
   `claude-opus-5-5`.
2. For a provider profile only: attach its API key through the browser
   credential handoff.
3. Verify it: **Check** and **Test turn** for a harness, tool verification for
   a provider.
4. Optionally, select it as Butler's model with `POST /api/v1/chat/control-assistant/session`.
5. Set the environment variables above, starting with the host allowlist,
   private strings, reviewer runtime and evidence roots, and leaving
   `…_ALLOW_SUDO` off.
6. Set `completion_requires_review=true` on the goals that need independent review.
7. Deploy through the normal deployer.
8. Turn on `NEBULA_BUTLER_SUPERVISOR_ENABLED=1`, first with Butler at Watch or
   Steer authority (shadow), then Act.

## Completion gating

- Every review verdict other than ACCEPT (NOT_READY, REJECT,
  ACCEPT_WITH_RESIDUALS) carries must-dos, stored on the goal. Butler's
  `complete` and the goal's own `goal.complete` are refused while any is open.
- Butler cannot approve completion of a goal it steered (goal actions, goal
  rewrites, Butler notes). Approval must come from Core's independent review,
  which records its provenance: provider or harness profile, model, review id
  and packet digest.
- Goal-written supervisor or reviewer sign-off lines are refused in completion
  proposals and redacted from review packets.
- A goal may request closure at most `NEBULA_BUTLER_CLOSURE_REQUESTS_PER_DAY`
  times per rolling day.
- A completed goal that a review rejects is reopened in place.
- Operator routes: `POST /api/v1/chat/sessions/{id}/goal/reopen` and
  `POST /api/v1/chat/sessions/{id}/goal/must-dos/{item_id}/waive`.

## Notes with release conditions

A Butler note that stops or gates work must carry a `release_condition`:
`until_time`, `until_path_exists`, `until_work_item`, `until_goal_status` or
`until_note_superseded`. Core re-evaluates conditions whenever it assembles a
goal turn, and retires satisfied notes so they stop outranking later guidance.

## Operator action requests

Butler raises a request for something only the operator can do as a
`Needs you:` Work item with a typed record and an auto-check. It must give
verification evidence first, a request whose check already passes is refused,
and an open request for the same action and device is returned instead of a
duplicate. The periodic check closes requests whose check passes and resumes
the waiting goal. Open requests appear at the top of the Butler board.

Goal agents raise the same requests with `goal.request_operator_action`
(`goal_request_operator_action` for provider goals; a native gateway tool for
harness goals). It is only for operator-only physical or account actions:
replug, plug in or power-cycle a device, restart a host, attach a credential,
approve an account change, or a physical check. Operator autonomy is in force,
so goals decide research questions themselves. Core links the request to the
calling goal (project, goal chat and the turn in `created_by`), keeps the
verification, auto-check and de-duplication (same action, device and goal)
rules, and refuses: a turn that does not own the running goal; Core-only
actions (`review_model_refusal`, `answer_question`); a physical action whose
device or host is a placeholder ("device", "the host"); a reason that asks for
a decision ("should I", "which ... should"), or any question for `other`; and a
fourth open request for one goal. `block_goal: true` blocks the goal on
`operator-action:<id>`, and the periodic sweep resumes it when the request
closes.

Opening and closing a request notify the operator. Every open (Butler, Core or
a goal) adds the `Needs you:` Work item, which the Butler board lists at the
top, and opens a Butler incident (`source=operator_action`, event
`incident.opened`) in the project's oversight feed; closing resolves it.
Nebula has no push channel: the iOS app is a web view of the same UI, and the
web app's service worker does not register for push.

A Claude harness model in a headless Nebula turn can call `AskUserQuestion`.
Core offers it only to answer it: the PreToolUse hook denies it with Core's
reply. A research decision gets "decide this yourself"; a question that asks
the operator to act with hands or account (replug, power-cycle, unlock, enter
a passcode, approve an account change) opens an `answer_question` request and
blocks the goal; a turn that is not a goal is told to ask in its reply.

## Running Butler on Claude

Butler, its subagents and the independent reviewer can all run on the Claude
harness with the host's existing Claude CLI login. No Anthropic API key or
provider profile is needed.

1. **Sign the CLI in as the Core user.** Core starts the Claude CLI (the copy
   bundled with the Claude Agent SDK, or the profile's CLI override) as a child
   process of Core. It runs as the same OS user, with Core's `HOME`. On Linux the
   login is stored in `$HOME/.claude/.credentials.json`, or in
   `$CLAUDE_CONFIG_DIR` if Core's service environment sets it. For a systemd
   user unit, the Core user is the unit's owner; in the container image, it is
   the image's `nebula` user. On the Nebula host, as that user and with the
   same `HOME` (for example `sudo -iu <core-user>`), run `claude auth login`,
   then `claude auth status`. Turns blank `ANTHROPIC_API_KEY` and every other
   ambient variable except a small allowlist, so only the stored login is used.

   **Separate Claude accounts.** A Claude profile can set an **Account folder**
   (Settings → Agent harnesses → Account folder), like Codex and Grok. Core
   then runs the CLI for that profile's turns, Check (`claude auth status`),
   Test turn and user skills/settings with `CLAUDE_CONFIG_DIR=<folder>`. The
   value is built per process and never written to Core's own environment, so
   it does not reach other profiles or their subagents, and profiles with
   different folders can run at the same time. Sign each folder in as the Core
   OS user with `CLAUDE_CONFIG_DIR=<folder> claude login`. A folder that does
   not exist on the Nebula host fails Check and turns with "Account folder is
   unavailable on the Nebula host". A profile without a folder keeps the
   default behaviour above exactly. Once a chat has used the folder it cannot
   be changed; add another profile to switch accounts.
2. Settings → Agent harnesses → **Add Claude**. Set **Authentication** to
   *Existing session*, default model `claude-opus-5-5`, and allow sensitive data
   and tool-result sharing. Leave native **Shell** off. Butler sessions never
   get the native shell even if the profile enables it, because the Core user
   may hold host privileges. Bash stays disallowed and the sandbox keeps its
   network restrictions.
3. Run **Check**. It runs `claude auth status` the way turns run the CLI. If
   the CLI is not signed in, Check fails and names the OS user, `HOME` and
   login location to fix (for a profile with an account folder, the folder and
   its `CLAUDE_CONFIG_DIR=<folder> claude login` command). Then run **Test turn**. A sign-in failure there
   gives the same explanation.
4. Reserve Butler:

   ```json
   POST /api/v1/chat/control-assistant/session
   {
     "engagement_id": "<project id>",
     "backend": "harness",
     "harness_profile_id": "<Claude harness id>",
     "model": "claude-opus-5-5",
     "subagent_harness_profile_id": "<Claude harness id>",
     "subagent_model": "claude-opus-5-5",
     "max_active_subagents": 3,
     "allow_cloud_tool_results": true
   }
   ```

   `subagent_harness_profile_id` and `subagent_provider_id` are mutually
   exclusive, and each needs `subagent_model`. Butler's children run in Butler
   mode, with Butler's Core tools through the gateway, as control-assistant
   harness sessions with no native shell. Background checks honor the saved
   choice. An existing Butler chat is switched with
   `PATCH /api/v1/chat-sessions/{id}/assistant-config`
   (`subagent_harness_profile_id`, `subagent_model`, `allow_subagents: true`).
5. To run the independent reviewer on Claude while goals stay on another model,
   set `NEBULA_BUTLER_REVIEW_HARNESS_PROFILE_ID` to the Claude harness id. Leave
   `NEBULA_BUTLER_REVIEW_PROVIDER_ID` unset; setting both makes reviews refuse.
   The reviewer model (default `claude-opus-5-5`) must still differ from the
   goal's model.

## Stage-based runtime routing

A research goal's main agent can run on a harness profile (for example the
Claude login above) while its Work stage is a validation-class stage, with its
subagents on a provider model. Bulk stages stay fully on the provider. The
operator sets the routes in `NEBULA_GOAL_RUNTIME_ROUTING` (JSON) and turns them
on with `NEBULA_GOAL_RUNTIME_ROUTING_ENABLED`, which is off by default.

- Routing changes only goals it owns. A goal whose current or pending settings
  already match its route is recorded as in policy and is not switched again.
  A goal whose runtime differs, for example from a manual choice, is recorded
  as out of policy and left alone until an operator or Butler route action
  (`POST /api/v1/chat/sessions/{id}/goal/runtime-route`,
  `nebula.supervisor.route_goal_runtime`).
- A switch is the Assistant settings writer's audited change, and it keeps the
  goal's MCP servers. It is evaluated at the turn boundary before the goal's
  next turn and never while a turn runs. Core refuses a harness profile
  without a verified Check/Test turn.
- A harness turn that stops at a usage or rate limit runs on the route's
  fallback runtime. The harness is tried again after a cooldown.
- The independent reviewer must differ from the goal's current main model and,
  after routing, from the model family of every main model the goal ran on.

The config shape, turn-boundary rule, fallback and rollout plan are in
[goal-runtime-routing.md](goal-runtime-routing.md).
