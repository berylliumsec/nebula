# Goal runtime routing

A research goal can run its main agent on different runtimes at different
stages of its Work. For example, the main agent runs on a Claude harness login
while the goal validates, and its subagents stay on a provider model. Bulk
stages (recon, lane promotion, bulk simulation, triage queues, coverage) stay
fully on the provider. The goal drops back when it returns to bulk work.

Routing is off by default. Codex, provider and harness goals that routing does
not own keep working exactly as before, and nothing switches silently.

Code: `src/nebula/v3/goal_runtime_routing.py`. Tests:
`tests/v3/test_goal_runtime_routing.py`.

## Configuration (operator)

Both variables are set in Core's service environment. The environment, not a
stored setting, holds the policy: it names Core-wide runtime profiles, only the
host operator sets it, and an agent cannot widen it with the tools it governs.

| Variable | Meaning |
|---|---|
| `NEBULA_GOAL_RUNTIME_ROUTING_ENABLED` | Master switch, default off. Off, routing reads and changes nothing, and the operator route and Butler tool refuse. |
| `NEBULA_GOAL_RUNTIME_ROUTING` | The policy as JSON (shape below). An invalid policy routes nothing and records a diagnostic. |

Policy shape:

```json
{
  "routes": [
    {
      "name": "validation",
      "stages": ["Validation", "Runtime", "Reporting"],
      "main": {"backend": "harness", "harness_profile_id": "<Claude harness id>",
               "model": "claude-opus-5-5"},
      "subagents": {"provider_id": "<provider id>", "model": "<provider model>",
                    "reasoning_effort": "high"},
      "fallback": "bulk",
      "fallback_cooldown_seconds": 900
    },
    {
      "name": "bulk",
      "stages": ["Recon", "Lane promotion", "Simulation", "Triage", "Coverage"],
      "main": {"backend": "provider", "provider_id": "<provider id>",
               "model": "<provider model>", "reasoning_effort": "high"},
      "subagents": {"provider_id": "<provider id>", "model": "<provider model>",
                    "reasoning_effort": "high"}
    }
  ],
  "promotion": {"review_rejections": 2, "route": "validation"},
  "projects": ["<project id or name>"],
  "goals": []
}
```

- `stages` are matched case-insensitively against the goal chat's Work stage
  key or label. The stage is the one the Butler board shows: the stage of the
  newest open Work item assigned to the goal chat, labelled by its project or
  nearest ancestor (`chat_work_stage`). A stage belongs to at most one route.
  A stage no route lists leaves the runtime as it is.
- `main` names `provider_id` for `backend: provider` or `harness_profile_id`
  for `backend: harness`. Its `reasoning_effort` is the provider effort, or the
  harness effort the model advertises.
- `subagents` names one of `provider_id` or `harness_profile_id`, a `model`
  and an optional `reasoning_effort`. Without it, a harness route turns
  subagents off and a provider route keeps the goal's subagent choice.
- `fallback` names the route a rate-limited harness turn runs on.
  `fallback_cooldown_seconds` (default 900) is how long the goal stays there
  before the harness is tried again.
- `promotion` moves a goal to `route` once its independent reviews have sent it
  back (`REJECT` or `NOT_READY` in the goal's `independent_reviews` history)
  `review_rejections` times. The default is 2.
- `projects` is required. Use `"*"` for every project. `goals`, when set,
  narrows routing to those goal chat or goal ids. Use it for a staged rollout.

## Ownership: never silent

Routing changes only a goal it owns. A goal becomes owned in one of two ways:

1. **In policy.** The goal's current Assistant settings already match its
   route, or so does a switch already waiting for idle. This covers a goal
   switched by hand through `PATCH /chat-sessions/{id}/assistant-config`.
   Routing records `in_policy` and changes nothing: no new switch, no
   overwrite, no duplicate pending switch. Matching compares the main backend,
   profile and model, and the subagent runtime and model when the route names
   subagents. Reasoning efforts are not compared.
2. **Explicit route.** An operator or Butler route action names the goal.

A goal whose runtime differs from its route and that routing does not own is
recorded as `out_of_policy` and left alone. The record is made once per route
and runtime. If an owned goal's runtime is changed by hand, routing releases it
(`out_of_policy`, `owned: false`) and does not change it back until an
explicit route action.

State lives on the goal chat in `metadata.goal_runtime_routing`: `owned`,
`applied` (the runtime routing chose), `route`, `status`, `override`,
`cooldown`, `deferred`, `main_models` and the last 50 `events`. Each event is
also added to the project's events as `goal.runtime_routing`, with the old and
new runtime, the reason and the actor.

## Applying a route

- **Code path.** A switch is `update_chat_assistant_config`, the writer behind
  `PATCH /chat-sessions/{id}/assistant-config`. It validates the selection the
  same way, and `RuntimeSwitchService` binds a new harness session or provider.
  The goal's `mcp_server_ids` are carried into every routed selection.
  Moving from a harness back to a provider keeps the MCP, hook and subagent
  choices, where a manual switch resets them.
- **Turn boundary.** Automatic routing runs in `RuntimeSwitchService.settle`.
  Every goal dispatch and continuation calls it before it starts the goal's
  next turn: provider continuation, provider dispatch, harness dispatch, the
  idle-goal reconcilers and resume. The stage read there is the one the next
  turn runs under. Core does not hook Work stage writes: a switch can only
  take effect at a boundary anyway, there are several writers, and the
  boundary read is never stale.
- **Never during a turn.** If the chat is busy (a turn, approval, claim,
  running subagent or queued follow-up), automatic routing records
  `deferred` and the next boundary applies it. An explicit route on a busy goal
  saves the existing waiting-for-idle pending switch, which Core applies when
  the goal goes idle or at its next boundary.
- **Verified harness only.** Core refuses a route to a harness profile that is
  disabled, does not advertise the model, or has no verified Check/Test turn
  (`capabilities.turn_state` is `verified` and sign-in has not failed). A
  harness route this goal left on a rate limit may be retried even though that
  failure marked the profile's last turn failed.
- **Mixed runtimes.** A harness main agent with provider subagents uses the
  existing harness subagent gateway (`subagent.start` and the related tools).
  Children run on the routed provider and model and inherit the goal's MCP
  selection.
- **MCP on the harness.** The goal's selected MCP servers and Core's gateway
  tools (`work.*`, `goal.*`, `subagent.*`) reach a Claude turn through the one
  Nebula gateway MCP server that the adapter passes to the CLI.

## Explicit route

Operator:

```
POST /api/v1/chat/sessions/{session_id}/goal/runtime-route
{"route": "validation", "reason": "validate the candidate", "dry_run": false}

GET  /api/v1/chat/sessions/{session_id}/goal/runtime-route
```

Butler (supervisor kill switch on, project or hygiene projects only, audited):

```
nebula.supervisor.route_goal_runtime
{"session_id": "...", "route": "validation", "reason": "...", "dry_run": true}
```

`route` is one of these:

- A route name. It pins the goal to that route until another action changes it.
- `auto`. Routing owns the goal and follows its stage and promotion rule.
- `release`. The goal keeps its current runtime, and routing stops changing it.

`dry_run` returns the plan without changing anything: the route, the reason,
the from and to runtimes, whether the goal would switch, and any busy reason.

## Rate-limit fallback

A harness goal turn can stop at a usage or rate limit. Core classifies that as
any of these:

- diagnostic `rate_limited` or `quota_exhausted`
- Claude's `api_error_status` 429
- a vendor `rateLimitExceeded` or `usageLimitExceeded`
- error text such as "usage limit", "rate limit" or "hit your limit"

When the goal is owned and on a route that names a `fallback`, Core does these
things in order:

1. Records `cooldown` (`route`, `until`).
2. Switches the goal to the fallback route and records a `rate_limit_fallback`
   event.
3. Resumes the goal if that turn paused it.
4. Runs the next turn on the fallback runtime instead of leaving the goal
   paused.

While the cooldown lasts, each boundary keeps the fallback. After it ends, the
next boundary moves the goal back to the harness. A transient 429 that the
existing one-time retry covers is first retried on the harness. The fallback
is not used when the step, time or token budget is spent, or the operator has
paused the goal.

## Reviewer independence

The independent reviewer must not share the goal's main runtime. The reviewer
and the recorded approval (`independent_review_approves`, Butler's completion
gate) are checked against these:

- the goal's **current** main model
- the model of a pending runtime switch
- every main model routing put the goal on, compared by **model family**

After a goal has run on Claude, a Claude reviewer is refused with a reason. A
reviewer on the goal's own main harness profile is refused too.

## Rollout plan

1. Run Check and Test turn on the Claude harness profile. Confirm the provider
   model is enabled, allowlisted and tool-verified.
2. Set the policy with `"goals": [<two goal chat ids>]` and enable the switch.
   Read each goal's `GET .../goal/runtime-route` and check `in_policy` or
   `out_of_policy`. Use `dry_run`, then route each goal with `auto`.
3. Compare the two goals against unrouted peers for a few stage cycles. Check
   switch events, deferred switches, fallbacks and cooldowns, review verdicts,
   cost and stage throughput.
4. Widen `goals`, then clear it and list whole `projects`. Goals already
   switched by hand are recorded as in policy and are not switched again.
5. To roll back, clear `NEBULA_GOAL_RUNTIME_ROUTING_ENABLED`. Goals keep their
   current runtime, and nothing switches back.
