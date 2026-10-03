# Work: operator and agent contract

Work is a project-management surface. Agent tools are on for new projects and
can be turned off per project. A saved off choice stays off. The operator can
keep and read Work records after turning agent access off. Work records stay in
the local Core database.

| Journey step | Observable invariant | State authority | Test layer |
| --- | --- | --- | --- |
| Find Work | Work appears in navigation and lists projects | UI route and Core projects | component and browser |
| Agent access | A new project exposes Work MCP tools; a project turned off does not | Core `Engagement.work_enabled` | API and gateway |
| Create item | A saved item appears in its project board | Core `WorkItem` | API and real Core browser |
| Check in | An update appears in history and changes item status | Core `WorkUpdate` and `WorkItem` | service, API, real Core browser |
| Track agent | A live assigned conversation appears as working; a blocked task stays distinct from activity | Core chat activity and Work records | browser |
| Agent initiative | Eligible turns name the Work tools without a timed check-in prompt | Core tool catalog and turn instructions | service and gateway |
| Live updates and navigate | Core signals saved Work changes and chat activity transitions; the board and timeline refresh without polling and survive reload and deep links | Core change feed, URL, and Core database | real Core browser |
| Retry | A repeated request ID returns the same saved item or update | Core entity IDs | service and API |
| Import | An authenticated batch creates projects, tasks, and one source update per task; a retry returns the same records | Core project, Work item, and update IDs | API and real Core |
| Group projects | Existing projects can be linked to a parent without overwriting tasks; the Work page nests children, searches names, and shows parent rollups | Core project parent ID | API, component, and real Core browser |
| Find imported work | The overview pages Work items, uses one active-agent query, and lets the operator search a large project list | Core list routes and UI query | component and browser |
| Disable agents | The gateway refuses writes and no longer advertises Work after a session reconnects; saved records remain | Core project setting | gateway and API |

The built-in session MCP gateway supplies `work.list`, `work.create`, and
`work.check_in` to managed harness agents. Provider-chat agents receive equivalent
Core-owned tools through their normal tool broker. Both paths use the same Work
service and project boundary. The gateway derives its project and actor from the
active turn. An agent cannot choose a different project in tool arguments.
Nebula names the available Work tools in eligible agent turns. It does not
steer agents for timed Work updates. An authenticated Core event stream signals
saved changes to the Work page. The page refetches authoritative records after
each signal and after reconnecting. It has no timed data polling; Refresh is a
manual fallback.

`WorkItem` describes a task and current status. `WorkUpdate` is append-only and
retains who posted it, when Core received it, and the linked conversation or
mission. A chat goal, mission, or transcript remains its own record. An update
is an activity report, not evidence that a result was verified.

## Operator import

`POST /api/v1/work/import` accepts a bounded batch of generic projects and Work
items. Each project and item has a source-specific external ID. Core derives
stable IDs from the source and those IDs, so a retry creates no duplicate. A
project may instead name an existing Nebula project ID. An optional item update
records a source summary, next step, and blocker. The API stores only the
submitted data in the operator's Core. It does not read local files. Imported
new projects use the normal agent-tools default; importing into an existing
project keeps its current setting. A new project's optional workspace path must name an existing
folder on the Core host and is resolved before any batch write. Existing
imported records remain unchanged on retry, so later
agent progress is preserved. A batch can be retried after a partial failure.

A new imported project may specify `parent_engagement_id` for an existing parent.
For projects already in Core, `PATCH /api/v1/work/projects` accepts up to 100
`project_ids`, an optional `parent_engagement_id`, and an optional
`work_enabled` value. It changes only these fields, checks for missing projects
and hierarchy cycles, and commits each batch atomically. A repeated batch is a
no-op. The Work page groups child projects beneath their parent, shows parent
task counts across descendants, and searches child names with parent context.

The import caller maps its own source status and priority values to Nebula's
generic enums. Source-specific conversion and private data stay outside this
repository. After import, the standard Work routes and UI display the saved
projects, items, and updates. `GET /api/v1/work/items` and
`GET /api/v1/work/updates` accept `offset` and `limit` for verification.
The overview pages items in 500-item blocks, searches project names locally, and
limits rendered project rows to 80 at a time. One Core query returns active
agents across projects; the page does not request each project's activity.
