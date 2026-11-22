# Project holds

A project hold is Core's own stop of all chat work in one project. Use it when
a project must have no running work: a whole-Core stop, a non-additive
migration, or retiring a pinned worker version. A rolling supervisor-mode
promotion does not need one.

## Why Core owns it

On 2026-11-20 a deployment wind-down was done by asking Butler to pause goals
and then pausing them one by one. It did not hold:

- Pause required the goal's exact revision, and busy goals change revision on
  every turn, so most pauses lost the race.
- A paused goal did not quiet its project. Subagent reports kept waking parent
  turns, and Butler's escalations started new subagents.
- Butler's periodic checks saw paused goals and resumed them.
- Nothing durable recorded what had been paused, so restoring it depended on
  an agent's summary.

## Contract

| Step | What Core does |
|---|---|
| Place | `POST /api/v1/engagements/{project}/hold` with `reason`, optional `deployment_id` (repeating it returns the same hold) and `grace_seconds` (default 180). Every running goal is paused with the hold's mark (`metadata.project_hold_id`). |
| Holding | No chat turn starts in the project. Operator and API sends, resumes, goal starts and resumes answer 409 with code `project_held`. Background producers defer without failing anything. Running turns may finish until `grace_until`. |
| Grace ends | Core's 15-second chat pass stops every turn that is still active, using the same stop an operator uses, and records each stop. |
| Held | Once no turn is active the hold is `held` and `held_at` is set. `GET .../hold` reports `held`, `active_turns` and `running_goals`. |
| Release | `POST .../hold/{hold}/release`. Core resumes exactly the goals carrying this hold's mark and starts their work. Goals paused before the hold stay paused. A goal that cannot resume (for example, its budget is spent) stays paused without the mark, so its operator can resume it normally. Deferred subagent reports and queued follow-ups are woken. |

## What defers while held

| Producer | Behaviour while held |
|---|---|
| Provider and harness turn creation, turn resume | Refused before anything is written (`ProjectHeld`, 409). |
| Goal start and resume (operator, Butler, restart recovery) | Refused (`ProjectHoldConflict`, 409). Only the hold's own release clears its mark. |
| Goal dispatchers and reconcilers | Goals are paused, so they skip them. |
| Subagent report waking a waiting parent | The parent keeps waiting; release resumes it. |
| Chat follow-up queue | Items stay queued, unclaimed and unreviewed. |
| Schedules | The occurrence stays due and fires after release. It is not skipped. |
| Process callbacks | The turn keeps waiting for the callback. |
| Core restart auto-resume, handoff restore | The turn stays interrupted or parked until a pass after release. |
| Butler oversight for the project | No deadlines, reviews, authority actions or recovery cases. |
| Global Butler actions on the project | Refused as a tool error. |
| Harness analysis turns, harness Missions starting now | Refused (`ProjectHeld`, 409). |
| A scheduled harness Mission that comes due | Waits, still queued, and starts after release. |
| Delegated Mission synthesis | Waits for release instead of ending the Mission. |
| Mission resume after an approval decision | Refused (409). The decision stays recorded; posting it again after release resumes the Mission. |

## Chat holds

When priorities shift inside a project, the operator holds the research in
progress and keeps starting new work in the same project. A chat hold is a
`ProjectHold` with `session_id` set.

| Step | What Core does |
|---|---|
| Place | `POST /api/v1/engagements/{project}/holds` with `session_ids` or `all_active: true`, optional `reason` and `grace_seconds`. One hold per chat, all sharing a returned `batch_id`. `all_active` holds the top chat of every chat with a running goal or active turn. Holding a held chat again returns its hold. No confirmation step. |
| Coverage | The chat and every subagent or goal-child chat beneath it (walking `parent_session_id` through those links only). Forks and chats started later are not covered, so new chats and goals start normally. |
| Holding | The same contract as a project hold, limited to covered chats: their goals pause with the hold's mark (children before parents, so release restores both), their turns get the grace period and are then stopped, and their subagent reports, follow-ups, schedules and callbacks wait. Sending to a held chat answers 409 `project_held`. |
| Read | `GET .../hold` lists active `chat_holds`; `GET /api/v1/chat-sessions/{id}/hold` says whether a chat is held and by which hold. |
| Release | `POST /api/v1/engagements/{project}/holds/release` with `batch_id` and/or `session_ids`. A goal another active hold still covers (a project hold placed meanwhile, say) moves to that hold instead of resuming or losing its mark. |

Interactive Butler has the same controls as `nebula.work.holds`,
`hold_chats`, `release_chats`, `hold_project` and `release_project`, and uses
them without confirmation when the operator asks. Background Butler never
holds or releases work.

Callers that do not know the chat (Butler oversight, harness analysis, a
Mission starting without a supervisor chat) see only project-wide holds.

## Limits

- Placing a hold and creating a turn are not one transaction. A turn that
  slips in at the moment of placement runs, and is stopped when the grace
  period ends.
- Stopped turns and subagents are not restarted on release. Their goals
  resume, and each goal decides its next step from its durable progress.
- A Mission's duration budget keeps counting while its project is held.
