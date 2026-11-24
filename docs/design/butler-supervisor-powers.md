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
| `NEBULA_BUTLER_REVIEW_PROVIDER_ID` | The provider profile for the independent reviewer. Use an Anthropic profile. |
| `NEBULA_BUTLER_REVIEW_MODEL` | The reviewer model. Default: `claude-opus-5-5`. It must differ from the reviewed goal's model. |
| `NEBULA_BUTLER_REVIEW_CEILING_POLICY` | A file appended to the reviewer instructions, such as an impact-ceiling policy. |
| `NEBULA_BUTLER_REVIEW_EVIDENCE_ROOTS` | Directories the reviewer may read evidence bytes from. |
| `NEBULA_BUTLER_HYGIENE_PROJECT_IDS` | Projects the hygiene tick covers. |
| `NEBULA_BUTLER_HYGIENE_UNLIMITED_BUDGETS` | `1` nulls the token, time and step budgets of live goals in those projects. |
| `NEBULA_BUTLER_HYGIENE_DEFAULT_STAGE` | The stage given to a newly created Work item. Default: `triage`. |

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
   The reviewer profile must be enabled, allowlisted, and permit sensitive data.
3. Core sends one tool-free request. It contains:
   - the completion criteria;
   - the proposal;
   - the evidence bytes, read only from the evidence roots, bounded and redacted;
   - the reviewer instructions, including the ceiling policy and the MUST-DO prompt.

   Butler's conversation and notes are not included.
4. The verdict (ACCEPT, ACCEPT_WITH_RESIDUALS or REJECT) is recorded in goal
   metadata, together with the packet digest and the reviewer identity.
5. The verdict is applied:
   - ACCEPT completes the goal.
   - REJECT adds the must-dos as a supervisor note and resumes the goal.
   - ACCEPT_WITH_RESIDUALS completes the goal and starts a follow-up goal with
     null budgets and `completion_requires_review`, or, with `on_residuals=feed`,
     feeds the residuals back to the goal instead.

## Enabling (operator steps)

1. Create an Anthropic provider profile allowing `claude-opus-5-5`.
2. Attach its API key through the browser credential handoff.
3. Verify the profile's tools.
4. Optionally, select it as Butler's model with `POST /api/v1/chat/control-assistant/session`.
5. Set the environment variables above, starting with the host allowlist,
   private strings, review provider and evidence roots, and leaving
   `…_ALLOW_SUDO` off.
6. Set `completion_requires_review=true` on the goals that need independent review.
7. Deploy through the normal deployer.
8. Turn on `NEBULA_BUTLER_SUPERVISOR_ENABLED=1`, first with Butler at Watch or
   Steer authority (shadow), then Act.
