# Host command queue

Status: proposed (2026-11-19). Replaces the per-session reservation and idle release
from #1216/#1217.

## Problem

Every Host command session reserved its full memory limit (2 GiB on live) for as long
as the chat kept it open. Idle chats filled the budget, and new sessions waited behind
them. #1216 releases idle sessions after 120 s, but capacity still follows chats, not
running work, and nothing shows the operator what is waiting or lets them stop it.

## Operator journey

An agent (chat, mission, harness) runs a Host command. When Core has room, the command
starts at once. When it does not, the command waits in one Core-wide queue. The operator
sees "Waiting for Host capacity · 3rd in line" on the tool call and can cancel it there.
In Settings › Compute, they see every running and queued Host command across projects,
set how many may run at once, and cancel any of them. The agent receives a normal result:
completed, failed, timed out, or "cancelled by the operator".

## Contract

| Invariant | Owner |
| --- | --- |
| Capacity is held only by running work: a command's reservation starts when it is admitted and ends when its group is empty. | Core DB (`memory_jobs`) |
| At most `host_command_limit` Host commands run at once across all projects and workers, and their reservations fit the memory budget. | Core DB, admission transaction |
| Admission is fair: the next command comes from the project with the fewest running Host commands, oldest first within a project. | Core DB, admission order |
| A queued command never blocks another owner, a session, or a worker. | Execution worker |
| Every queued or running command can be cancelled by the operator or by stopping its turn, and its result says so. | Core API → worker |
| A queued command whose worker died is removed from the queue within 30 s and never admitted. | Reconciler (heartbeat) |
| Queue state survives reload: position, wait time and status come from Core, not the browser. | Core API |

## Design

**Admission unit.** Each Host command, foreground or background, is one memory job
(`resource_kind=host_command`, id `command-<execution id>`) with its own protected
group. A Host session holds no reservation and is not closed for being idle; the
idle release from #1216/#1217 is removed. The one exception is a project handoff:
the source worker closes the project's idle Host sessions (no command in flight,
running, or holding a detached group) so they do not pin the project, and the
owner's next command opens a fresh session on the target worker.

**Queue.** Queued jobs stay in `memory_jobs` (state `queued`), shared by every worker.
One admission transaction (under the existing budget lock) admits a job only when:
it is first in fair order, the running count is below the limit, and its reservation
fits. Waiting workers check with a read and write only to admit or to heartbeat
(every 5 s), replacing today's write every 0.2 s.

**Limit setting.** `host_command_limit` joins the memory budget policy: "Host commands
running at once", 1–64, default 8. It is saved through the existing memory-budget
revision flow and is discoverable in the settings assistant.

**Waiting.** A queued command has no fixed wait limit; it waits until admitted,
cancelled, or its turn stops. Its command timeout starts at admission. (Open question:
a 30-minute safety limit.)

**Command lifecycle.** `CommandExecution.status` gains `queued`:
`queued → running → completed | failed | timed_out | cancelled | interrupted`.
A foreground command returns when its main process exits. If it left detached
processes (`nohup`, `setsid`, `&`), its group stays charged and listed as "detached"
until those exit or the operator cancels it.

**Cancel.** `POST /host-commands/{execution_id}/cancel`. A queued command is removed
before it starts; a running one has its group killed (`cgroup.kill`). Stopping the
chat response cancels its queued and foreground commands as today.

**Visibility.**
- Tool card (ActivityLedger): queued state "Waiting for Host capacity · N in line" with
  a cancel icon; running shows a cancel icon too.
- Settings › Compute › Host commands: the limit, then Running and Queued lists with
  project, chat, command, elapsed or waited time, memory, and a cancel icon per row.
- `GET /host-commands` returns running, detached and queued commands with positions;
  the page refreshes like the memory settings (poll + focus + change event).

**Recovery.** A queued row whose heartbeat is older than 30 s is cancelled by the
reconciler and skipped by admission. A running command whose worker died is settled
by the existing dead-owner recovery, which kills its group first.

## Lifecycle coverage

| Step | Applies | Notes |
| --- | --- | --- |
| Create/start | Yes | Immediate when there is room. |
| Queue | Yes | Position and wait time on the card and in Settings. |
| Interrupt/cancel | Yes | Card, Settings, Stop response. |
| Reload/reconnect | Yes | All state is read from Core. |
| Core restart / deploy | Yes | Queued rows of an evicted worker are reaped; running groups are killed and settled as interrupted. |
| Delete project | Yes | Cancels its queued and running commands. |

## Out of scope

SSH and Docker commands (other runners), per-command memory sizes, and reordering
the queue by hand.
