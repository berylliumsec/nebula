# Work: operator and agent contract

Work is an optional project-management surface. It is off for agents until an
operator enables it for a project. The operator can keep and read Work records
after turning agent access off. Work records stay in the local Core database.

| Journey step | Observable invariant | State authority | Test layer |
| --- | --- | --- | --- |
| Find Work | Work appears in navigation and lists projects | UI route and Core projects | component and browser |
| Enable agents | One project exposes Work MCP tools; other projects do not | Core `Engagement.work_enabled` | API and gateway |
| Create item | A saved item appears in its project board | Core `WorkItem` | API and real Core browser |
| Check in | An update appears in history and changes item status | Core `WorkUpdate` and `WorkItem` | service, API, real Core browser |
| Track agent | A live assigned conversation appears as working; a late update is distinct from a blocker | Core chat activity and Work records | browser |
| Prompt | A working agent without an update for 20 minutes receives a prompt; agents are instructed to update about every 30 minutes | Core turn start and latest agent update | service and harness/provider |
| Refresh and navigate | The item and its timeline survive reload and deep links | URL and Core database | real Core browser |
| Retry | A repeated request ID returns the same saved item or update | Core entity IDs | service and API |
| Import | An authenticated batch creates projects, tasks, and one source update per task; a retry returns the same records | Core project, Work item, and update IDs | API and real Core |
| Find imported work | The overview pages Work items and lets the operator search a large project list | Core list routes and UI query | component and browser |
| Disable agents | The gateway refuses writes and no longer advertises Work after a session reconnects; saved records remain | Core project setting | gateway and API |

The built-in session MCP gateway supplies `work.list`, `work.create`, and
`work.check_in` to managed harness agents. Provider-chat agents receive equivalent
Core-owned tools through their normal tool broker. Both paths use the same Work
service and project boundary. The gateway derives its project and actor from the
active turn. An agent cannot choose a different project in tool arguments.

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
submitted data in the operator's Core. It does not read local files or enable
agent Work tools. A new project's optional workspace path must name an existing
folder on the Core host and is resolved before any batch write. Existing
imported records remain unchanged on retry, so later
agent progress is preserved. A batch can be retried after a partial failure.

The import caller maps its own source status and priority values to Nebula's
generic enums. Source-specific conversion and private data stay outside this
repository. After import, the standard Work routes and UI display the saved
projects, items, and updates. `GET /api/v1/work/items` and
`GET /api/v1/work/updates` accept `offset` and `limit` for verification.
The overview pages items in 500-item blocks, searches project names locally, and limits rendered project
rows to 80 at a time. Agent activity queries cover Work-enabled projects,
projects with assigned Work, and the current project.
