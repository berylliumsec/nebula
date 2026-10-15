# Nebula agent guidance

Read and follow these repository rules before working in Nebula:

- [Branch, validation, and PR workflow](.codex/rules/change-workflow.md) for every change.
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

## Coordinate Core deployments with Butler

Before restarting or replacing a running Core, enumerate the projects it serves
and contact Butler in each project with active assistant or mission work. Find or
create its project-scoped conversation through authenticated `GET` or `POST`
`/api/v1/chat/control-assistant/session`, then send the deployment request through
`POST /api/v1/chat/completions` with `control_assistant: true`. Ask Butler
to wind down active main agents, pause running Core-owned goals and queued
continuations it can safely pause, and report the exact projects, parent chats,
goals, missions, and queues it changed. Butler coordinates with parent agents;
it does not message their children directly. Keep that inventory in the
deployment record, including which items were already paused beforehand.

Wait for Butler's turn to finish, then read the authoritative Core state and
verify the pause results before restarting. Recheck for newly active work at the
restart boundary. If a native harness goal, mission, or in-flight turn has no
safe pause control, let it reach a safe stopping point or defer deployment and
report the blocker. Do not silently substitute stop, cancel, or approval for
pause, and do not treat Butler's acknowledgement alone as proof of state change.

After the new or restored Core passes deployment health checks, contact the same
project Butler sessions and ask it to resume only the exact work it paused for
this deployment. Have Butler read fresh state and revisions first, then verify the
result in Core. Preserve work that was paused before the deployment; leave work
blocked by an approval, limit, or uncertain effect paused and report it for
operator resolution. Never put Core authentication secrets in the deployment
record or PR.
