# Nebula assistant contract

The paired terminal Assistant exposes `/board` in slash completion and F4 Commands
after a project is selected, even before a conversation exists. It reads the same
Core Work records as the web project's Work board, including when agent Work
posting is disabled. Tasks are grouped by all six statuses. Up/Down selects,
Enter or Tab reads the description and latest 200 check-ins, `/` searches titles
and descriptions, R refreshes (Ctrl-R in Search), and Escape returns to the
unchanged conversation and unsent draft. Failed reads explain retry in place;
refresh retains selection when the item still exists and matches. Reopening or
reconnecting reads Core again. There is no polling or terminal board mutation;
Butler should direct creation, editing and status changes to the web Work
destination or existing authorized Work tools, not claim that viewing a check-in
verifies its results. Plain terminal `/board` prints status-grouped tasks.
Settings > Paired devices remains the destination for pairing/revoking this
client; no new Settings action or browser handoff is needed for board reads.

Terminal transcript scrolling is a local viewer action, not a Core setting.
Butler can explain wheel/trackpad scrolling and Page Up/Page Down directly from
the composer. Ctrl-Home selects the first loaded line; Ctrl-End returns to the
latest output and resumes automatic following. Incoming output preserves an
earlier reading position. Tab returns to the unsent draft. F12 Copy disables
mouse reporting for native drag selection; F12 Scroll restores it. F10 Source
exposes original Markdown. These controls do not mutate Core's saved messages.

Project runtime recovery is a Core lifecycle operation, not a memory-budget
setting. An expired project process can leave chat delivery unavailable while
the host API is healthy. Core automatically reconciles positively dead owners
and uses durable recovery receipts; unknown tool effects require review, and
pre-existing paused goals stay paused. Butler must not suggest raising budgets,
clearing ownership, replaying an uncertain operation or restarting all of Core
as a substitute for reading deployment status and the recorded recovery outcome.
There is no new Settings destination or browser-side recovery mutation.

Memory budgets include the observed resident-runtime reserve and minimum Core
reserve. Butler reads these through the same API as Settings. It must not lower
Core reserve below resident requirements or describe declared service capacity as
measured job usage or kernel enforcement; retire unused runtimes or increase the
reserve when new admissions are held.

In supervisor mode, the stable host serves memory policy, usage and runner
checks independently of replaceable API workers. Project Butler uses that same
validated API through its authenticated, lease-bound host channel; direct
project execution services cannot edit global policy. Running-job changes go
to the project's current execution owner and require its observed runner
application. An unavailable owning runner leaves the job charged and the
application unresolved.

Memory budgets are a host-wide Settings destination (`memory-budgets`, target
`memory-budget-settings`). Butler discovers typed `nebula.settings.memory_budget`,
`memory_usage`, `set_memory_budget`, `verify_memory_job` and `apply_memory_limit`
actions. Read before changing, retain unrelated policy fields, use fresh
revisions, and verify the authenticated Core read-back. State whether a policy
is saved for new jobs, a worker's limits are verified, or application is pending,
failed or unsupported. Lowering running limits requires an explicit impact
acknowledgement; a memory failure never authorizes automatic retry, a larger
budget or goal resume. See [Compute memory budgets](../MEMORY_BUDGETS.md) for
precedence, units, scope and runner dependencies.

Local search has one persistent `web_search_service` entry in the aggregate job
budget, with no per-query or additional runtime-reserve charge. Butler reports
its frozen effective policy and observed kernel evidence, including failed or
uncertain cleanup; it does not equate a saved limit with a running worker limit.
The service request caps RAM at 512 MiB and swap at zero while preserving stricter
administrator/environment/profile settings. Cached official images can start;
image download/build needs a daemon-worker memory adapter and is otherwise held.

Large memory inventories retain structured job summaries with explicit
`at_limit` and `summary_only` flags. They are partial observations, not a complete
job roster; use `verify_memory_job` for full saved evidence. Profile metadata
may be previewed, while the policy retains unrelated overrides within its
bounded structured response. Never construct a policy update from a truncated
excerpt or treat missing measurements as zero usage.

Entry: the persistent assistant icon in the shared shell opens one movable,
hideable control conversation. Closing the window preserves its Core transcript.
The operator may open Settings from a catalog result without losing the draft.

The installed iPhone app presents project agent conversations directly. It does
not mount this Settings assistant or its destination and mutation tools. The
app can choose among already configured agent runtimes and routes its App
connection action to native settings. Full Nebula in a browser retains this
Settings assistant and the complete destination catalog.

| Journey | Observable invariant | Authority | Test layer |
| --- | --- | --- | --- |
| Discover and open | Icon is reachable on every route and at phone widths | Shell React state | Component and browser |
| Launcher placement | The assistant icon lives in the shared top bar, the phone Workbench navigation, or the phone focus toolbar; it never overlays the Zero dock or page content | Shell chrome and one persistent assistant conversation | Desktop and mobile browser geometry |
| Select runtime | Healthy configured provider or harness is chosen and named | Core profiles, shell selection | Component and real Core |
| Ask and stream | One durable control conversation receives ordered output | Core transcript and provider or harness turn | Real Core |
| Configure host pressure | Settings > Setup > Host pressure saves CPU, memory-use, runnable-tasks/capacity and Core-cgroup I/O PSI some avg10 thresholds, each 80% by default. Read nebula.work.host_pressure or nebula.global.host_pressure, then authenticated GET/PUT /api/v1/host-pressure with current host_id, expected_revision and thresholds {cpu,memory,load,io} ratios. Any available reading >= its threshold queues new starts without provider/background slots; below all thresholds resumes without hysteresis. Unknown is not pressure; running jobs continue. Read saved/effective thresholds, current readings, limiting metric and effective concurrency (defaults 60/58). Host load and host I/O PSI are telemetry only; Core I/O PSI is task-stall time, not disk utilization | Core host-local revisioned policy, sampler and durable queues | authenticated settings, live updates, restart, cancellation and production LAN browser |
| Change setting | Exact target and value are validated; saved state is read back | Browser preference or Core entity | Browser and real Core |
| Configure and activate host CPU workers | Setup saves a host-local CPU budget and enabled state shared by all projects/browsers. Enable background PDF workers starts real Linux processes for PDF generation; Automatic uses roughly half capacity with a minimum of one and maximum of four whole worker CPUs. Butler reads `nebula.work.host_cpu_budget` or `nebula.global.host_cpu_budget`, then uses authenticated GET/PUT `/api/v1/host-cpu-budget` with fresh `host_id`, `expected_revision`, `cpu_limit` and explicit `enabled` only when activation/disable was requested. Omitted enabled preserves state. Saved pending/failed is distinct from running: verify `executor_state=running`, `apply_state=applied` and worker count. Disable/resize drains accepted work; disabled requires zero processes. Reload/restart restores policy. External chat/tool/LLM/GPU execution is outside this pool | Host-local atomic policy, Core-owned worker lifecycle and kernel affinity | authenticated API, real PDF/restart/drain/conflict/failure, component, production LAN/browser and frozen Linux Core |
| Authorize a setting change | An explicit request in the dedicated assistant acts on the selected project; Core validates any required acknowledgment and reads saved state back | Assistant tool boundary and Core policy | Real Core |
| Hide and restore | Draft, turn, and transcript remain available | React presentation, Core turn | Browser and real Core |
| Refresh and reconnect | Transcript and pending action return without duplicates | Core and saved conversation identity | Real Core |
| Stop and retry | Only this assistant turn stops; the question remains editable | Core turn, React draft | Real Core |
| Close | Window closes without deleting history | React presentation, Core transcript | Browser and real Core |
| Compose | Enter submits one message; Shift+Enter inserts a newline; composition does not submit | React draft and Core turn | Component, browser and real Core |
| Open a named setting | An unambiguous open/show request opens the existing Settings lens immediately | Shared settings catalog and shell lens | Component and browser |
| Open storage integration | A request to connect a storage source opens Settings → Integrations → Storage; a request to view storage devices opens the global Storage dashboard | Shared settings and app destination catalogs, URL, Core storage state | Tool unit, component and real Core browser |
| Open an app destination | A request such as “show me my projects” navigates to the All projects page immediately; every static app route is available through the assistant catalog and exact model tool | Shared packaged destination list, URL and selected Core project | Tool unit, component, desktop/mobile browser, real Core |
| Change this device's theme | An explicit theme command opens Appearance, applies and confirms the browser preference, then closes the lens; “Zero theme” means Zero Dark | Browser theme context and storage; shell lens owns presentation | Component and desktop/mobile browser |
| Model-selected browser action | The model calls an exact open-setting or device-theme tool; Core returns a typed pending action, and only the requesting browser applies and confirms it | Scoped tool receipt and browser theme/lens state | Tool unit, component, real-Core browser and live model |
| Fast answer | Provider turns request low effort; harness turns select advertised low effort and fast tier or matching fast model | Runtime model options and Core provider adapters | Component and real Core |
| Change project execution mode | An explicit request opens Project Policy, names the selected project, saves through the existing policy API with Host acknowledgment when needed, reads back the mode, refreshes the visible control, and closes the lens after showing the result | Core automation policy; shell lens owns presentation | Component, browser, real Core and LAN |
| Change target and URL scope | A request for all destinations saves the selected project's URL, network and port scope while retaining unrelated policy fields; Allow all is a separate permission bypass | Core scope revision and assistant browser receipt | Component, browser, real Core and LAN |
| Start the headless security browser | An explicit start request asks Core to establish the selected project's session and reports the observed status or startup error | Core agent-security session | API, browser, real Core and LAN |
| Change other Core settings | The assistant discovers this build's existing Settings routes and body schemas, calls the authenticated route, and reports its result only after a successful response and read-back where offered | Core Settings API and current project | Tool unit, real Core and LAN |
| Pair or revoke a device | Interactive Butler lists nonsecret paired-device identities and opens the requesting browser's Paired devices control. The Nebula host browser or host-only `nebula-core chat invite` creates and displays a single-use invitation; the new device redeems it. `nebula-chat pair` gives a terminal its own revocable session, `nebula-chat` or `shell` opens a full-screen terminal Assistant with project/chat navigation, live progress, direct agent conversations, skills, goals, settings, attachments and pending actions. Typing `/` in its composer opens commands filtered by the current conversation and turn; Up/Down and Enter select one, while Tab inserts it. Outside that menu, Up in the composer recalls the selected conversation's latest sent message from loaded Core history, repeat Up steps older, and Down restores an unsent draft; multiline cursor movement remains available and recall makes no new Core request. The terminal header and activity strip animate through local Core reads and confirmed active turns, with elapsed time for local operations; `NEBULA_CHAT_REDUCED_MOTION=1` keeps the same feedback stationary. The transcript, activity strip and composer fill the available terminal pane beside a separated conversation rail; compact shortcuts remain visible. The rail shows Core-backed Running, Waiting for action, Idle or State unknown; F3 shows compact RUN/WAIT/IDLE labels. A queued turn or pending action stays visible without a progress animation; running work can continue after the viewer detaches, and `/status` gives the selected chat's next action. `/snapshot` or F11 opens the selected project's on-demand Core-owned summary of Work progress, assets, mission, findings, cost, and pending approvals; R refreshes the panel without changing the chat. `/notes` opens the selected project's saved Notes in a separate full-screen panel. Up/Down selects, Ctrl-N creates, Ctrl-S saves with a Core revision readback, Ctrl-R refreshes, and deletion checks report dependencies; chat work continues while Notes is open. `NEBULA_CHAT_PLAIN=1` uses a scrolling prompt. Legacy `chat` opens the project's saved Butler conversation, and `unpair` revokes that terminal. Install the separate `nebula-client` Python wheel on Linux or macOS without Core dependencies; the new shell requires Core terminal protocol 1. `nebula-core headless` runs Core without a GUI or frontend build. See `docs/CLI.md`. For another browser device, the browser confirms an exact ID and creation time, revokes it, and verifies Core list absence. The current browser uses its direct Unpair control because access ends immediately. The generic Settings API does not create offers or revoke devices | Core paired-device sessions; host and companion devices own one-time secrets and local access | Butler tool, Core API, terminal and production LAN browser |
| Create or delete a credential | Butler opens write-only credential entry in the requesting browser or prepares an exact managed reference for browser confirmation and deletion. Core status proves creation or absence; only the reference enters the saved Butler outcome. The generic Settings API exposes credential status reads, not secret mutations | Core credential store; browser owns the secret and persistence choice | Butler tool, Core API, component and production LAN browser |
| Attach a credential to a model provider | Butler lists provider identities and reads the exact revision, then prepares a managed-reference handoff. The requesting browser checks credential availability, confirms the provider and inference or OpenRouter management slot, patches only that slot at the current revision, and reads the saved provider and credential status back. The raw secret never enters Butler. Integration-specific attachment remains a separate workflow | Core global provider profile and credential store; requesting browser owns confirmation | Butler tool, Core API, component and production LAN browser |
| Configure an OpenAI API provider | Settings → Models → Add provider → OpenAI accepts an API key in the private credential flow and an optional Daybreak Blue choice. Core saves `openai_cyber_access_program=daybreak_blue` in that profile's options; every OpenAI Responses request sends `access_programs: {"cyber":"daybreak_blue"}`. Automatic omits the field. The choice survives refresh and appears on the provider card. OpenAI decides access for the selected project and model; an unauthorized explicit request can return 403. Butler discovers the Model providers destination for OpenAI API key or Daybreak Blue requests and can inspect nonsecret profile options through the provider Settings API | Core provider profile and credential store; OpenAI Responses adapter | Component, provider wire, assistant discovery and production LAN real Core |
| Attach a credential to an Assistant harness | Butler pages harness identities and exact revisions, then prepares an available managed-reference handoff. The requesting browser confirms the exact harness and patches only the reference and coherent authentication mode at the current revision. Grok cached login and Claude native shell are excluded. The browser reads back Core's saved harness and credential availability; running sessions keep their own state and the operator checks harness health before new work | Core global harness profile and credential store; requesting browser owns confirmation | Butler tool, Core API, component and production LAN browser |
| Attach a credential to an MCP server | Butler pages nonsecret MCP server identities, transports, revisions and occupied key names. The requesting browser confirms an available managed reference for one exact HTTP bearer, HTTP header or stdio environment slot, patches the current revision, preserves other active key references, and reads Core's saved slot, mode and credential status back. Built-in servers cannot be changed; the operator checks MCP health before use | Core MCP profile and credential store; requesting browser owns confirmation | Butler tool, Core API, component and production LAN browser |
| Manage nonsecret integration profiles | Butler discovers the Settings route and schema, lists provider, Assistant harness or MCP profiles, and reads the exact current identity before a change. A create reads back Core's new profile ID; patches use its current revision; deletion requires the observed revision and Core's dependency guards. Verified writes refresh the requesting browser's Settings catalogs. Credentials use their private browser flow, and a saved profile does not prove runtime health | Core profile validator, revision and exact readback; browser owns catalog presentation | Parameterized Core gateway, receipt parser and production LAN browser |
| Manage project Notes | Butler pages editable notes and same-project asset/evidence link targets, reads bounded body chunks, and asks the requesting browser to create or edit exact note fields. This includes operator-accepted execution notes (`ai_execution_note`), with original AI provenance and operator acceptance retained. The browser reconciles an uncertain create by its fixed note ID, uses the current revision for edits, and reads saved fields and links back. Deletion confirms the exact note and remains blocked while reports use it. Saved notes appear by title in Project Notes and can be selected as report sources. The paired full-screen `nebula-chat` `/notes` view browses and edits those same project records on demand, retains an unsaved draft on failure or revision conflict, and confirms saves from Core; recording a claim does not verify it | Core notes, relation projection and dependency/revision guards; browser owns the handoff and temporary drafts | Core capability/API, component and production LAN browser |
| Review manual execution notes | From execution history, generate an explanatory draft with the selected provider or harness and per-request cloud consent. A manual draft remains ready until explicit operator acceptance; choosing a harness does not enable automatic notes. Accepted notes appear in Project Notes and Butler's note catalog with AI-generated and operator-accepted provenance. Automatic post-tool notes require the separate enabled follow-up configuration and an explicit automatic request | Core draft/observation transaction and revision; browser owns manual review and consent | API client/component, Core service and production browser |
| Export a diagnostics support ZIP | Butler prepares an export intent for the requesting browser. The browser describes the sensitive logs, asks for confirmation, verifies Core returned ZIP bytes, and starts the local download. Butler receives no bundle bytes and does not claim the operating system saved the file | Core diagnostics exporter and requesting-browser download | Butler tool, component and production LAN browser |
| Configure Butler Voice | The catalog opens Butler's compact Voice timeout control; the assistant can discover the authenticated Core-wide Voice settings route and report a change only after Core read-back | Core Voice settings | Tool unit, component and real Core |
| Manage Nebula-wide Butler instructions | The catalog opens the editable BUTLER.md Markdown code editor above the Settings popup after Core loads the file. The full Settings page retains its file row and preview; upload opens a draft. The API checks the loaded digest before saving and the UI reads Core back before confirming | Core data directory and durable Butler global clock | Tool unit, component and real Core browser |
| Set Butler model and effort | The catalog opens Butler settings. Full Assistant settings opens Butler's saved conversation with its Assistant settings panel visible. Core saves the selected effort on Butler's chat; the next direct turn and each new scheduled harness check use that saved choice. An unset effort uses the model's advertised default | Core Assistant settings and harness session | Core unit, component and real Core |
| Explain a Nebula problem | A question receives an answer grounded in the selected project's effective mode and linked workspace when those facts matter; it does not infer operator intent from an automatically stored Docker default and distinguishes future policy from existing frozen command sessions | Core project and automation policy; model owns explanation | Tool unit, provider and harness boundary, real Core model turn |
| Help resolve a problem | A suggested Settings destination opens the same visible lens as search; a mode change still requires an explicit request and Core's Host access acknowledgment field | Shared setting catalog, shell lens, Core policy | Component, browser, real Core and LAN |
| Check project work | The selected project's assistant chats and missions are listed with bounded current status and exact targets. In Workbench conversations, chats Butler creates appear below Butler; older recovery chats use their linked Core goal as ownership evidence. Search reveals matching children, and a selected child remains open after reload | Core sessions, goal metadata, turns, queues and runs; URL for selection | Tool unit, component, browser and real Core |
| Organize conversations | In the Workbench conversation list, More actions can pin or flag one chat. Select conversations loads the full project chat list, then lets the operator select individual chats or all search matches and apply Archive, Restore, Pin, Unpin, a flag color, Clear flag, or Delete permanently. Deletion shows the exact selected count for confirmation. Core readback confirms each outcome, and failed chats remain selected for retry. These choices survive refresh because Core owns them. An archived Butler-created chat moves into Butler's own collapsed Archived section and remains searchable and restorable. The dedicated settings assistant can open this list and describe the selection workflow when asked for bulk chat actions, pinned chats, flagged chats, or archived Butler chats; it does not apply destructive actions for the operator | Core chat-session metadata and deletion; UI owns only temporary selection and expansion state | Core API, component, browser and real Core |
| Edit a saved conversation message | Interactive Butler can page active message previews with `nebula.work.chat_messages`, then use `nebula.work.rewind_chat` with the selected user message ID and current chat revision after an explicit request. Core retracts that message and later replies in the same chat and reports the saved revision. A replacement is a separate `nebula.work.message_chat` action; Butler must verify that turn separately. Scheduled Butler cannot rewind. The Workbench edit control uses the same Core retraction path | Core transcript, chat revision and harness replay; URL selects the chat | Core API, Butler tool and real Core browser |
| Export a saved conversation | Interactive Butler discovers `nebula.chats.export` for one listed main chat and passes its current revision. The requesting browser confirms sensitive transcript content, rereads the chat, fetches the complete active transcript through authenticated Core, verifies that the revision stayed stable, and starts a Markdown download using the chat menu's formatter. Butler receives only a short outcome, never the transcript bytes. Cancellation starts no download; scheduled Butler has no device export action | Core chat and messages; requesting browser owns confirmation and local download | Butler tool, component and production LAN browser |
| Build, transform, delete and export a report | Interactive Butler pages selected-project reports and source findings or notes, reads an exact report revision, and requests a draft create or edit through the paired browser. For a linked note, it can read bounded source text and prepare an exact report/source-revision AI section transform. The requesting browser chooses a permitted runtime, obtains one-request cloud consent where needed, lets the operator edit the draft, and saves it only after current-revision checks and Core read-back; the source note remains unchanged. Core validates all report references and reads back the saved record. The requesting browser confirms deletion of an exact draft or review report with `If-Match`; Core 404 and refreshed list absence confirm the result. A saved PDF render may retain its report; signed final reports cannot be deleted. A sign-off request opens the exact review report's existing form; the operator chooses identity and attests before Core makes it immutable. PDF and sensitive engagement bundle exports require browser confirmation, reread the selected revision, and start a download on that device. Butler receives no file bytes. An uncertain draft create is reconciled by its fixed report ID before retry; scheduled Butler has no report mutation or device export | Core report, project and render records; URL selects the report; requesting browser owns deletion confirmation, sign-off and downloads | Butler tool, Core API, component and production LAN browser |
| Manage project workspace files | Interactive Butler can page a selected project's workspace entries and read bounded UTF-8 text through `nebula.workspace.list` and `nebula.workspace.read`. It can create text without overwriting, replace or rename a regular file with its full current SHA-256, and request deletion with Files' confirmation. `nebula.workspace.preserve_evidence` copies one exact current hash into immutable Evidence and reads the saved Evidence identity back. `nebula.data.system kind=workspace_reset_status` reports reset eligibility without changing files; bulk reset remains a destructive Files handoff. `nebula.workspace.upload_from_device` opens a browser-owned picker for any file; Core checks the selected directory identity, expected content SHA-256, and a current file SHA-256 before a confirmed overwrite. `nebula.workspace.create_directory`, `rename_directory`, and `delete_directory` use opaque directory identities; deletion requires an empty folder and browser confirmation. Core checks path containment and reads mutations back. `nebula.workspace.download_file` sends only a path and digest to the requesting browser, which verifies downloaded bytes. Scheduled Butler has no workspace file actions. Relinking an existing project requires a separate control | Core WorkspaceService and selected project; requesting browser owns reset confirmation, file picker and download | Butler tool, Core API, component and production LAN browser |
| Relink an existing project folder | Interactive Butler reads the target project and prepares `nebula.projects.relink` at its current revision. The requesting browser chooses a Nebula-host folder, sees both paths, and confirms the change. Core rejects stale revisions and projects with sessions, queued follow-ups, native hooks, or other work that can still use the old folder; generic project updates cannot bypass this route. After a successful save, the browser refreshes the selected project context and opens the new Files view. Existing files stay in the old folder, a stale Notebook file index is disconnected, and Host access keeps its one-time project policy | Core project record and workload state; requesting browser owns host-folder selection and confirmation | Butler tool, Core API, component and production LAN browser |
| Inspect and stop a project terminal | Interactive Butler can discover `nebula.terminal.list` for ticket-free live session identities in the selected project, and request `nebula.terminal.stop` for one exact session ID and creation time. The requesting browser rereads Core, confirms the destructive stop, sends a conditional project-scoped close, and reads back absence before reporting success. Cancellation leaves the terminal active; uncertain results require fresh inspection. Opening the Terminal destination on the requesting browser starts or reconnects the shell through its existing UI. Scheduled Butler cannot use the terminal actions | ContainerTerminalService, selected project, requesting browser and live process; WebSocket tickets and shell output stay out of Butler | Butler tool, Core API, component and production LAN browser |
| Edit and run a project Notebook | Interactive Butler can open the Notebook destination and discover typed `nebula.notebook.*` actions. It pages project paths and cell previews, reads exact source, then creates or edits with a current document revision or cell source hash. Core checks the live Notebook grant and project path before each call. A saved edit is reported after Core read-back; a stale hash preserves the existing cell. After an operator request, Butler executes the exact current code cell hash, pages the run and its ordered events, and can interrupt one exact run ID. An uncertain execution is checked against recent runs before another attempt. For resources, Butler reads `kernel_status` and `installed_package`, installs only a pinned `name==version` after an explicit request, and reports Core's observed version. A Docker project without a current container reports the package state as unknown; an authorized install mounts the persisted environment and reads its version before changing it. Butler restarts only the selected idle kernel instance and generation after an explicit request; Core reports a new generation after worker acknowledgement. A missing kernel is reported as absent, and uncertain installs or restarts require fresh reads before retry | Core Notebook grant, project policy, selected environment, kernel instance and generation, workspace file and run store; URL selects the Workbench view | Butler tool, Core API and production LAN browser for edit, execution, interruption, package installation and restart |
| Add or download a shared Library file | Interactive Butler discovers `nebula.library.add` or selects an exact item and revision before `nebula.library.download`. The requesting browser owns its file picker or sensitive-download confirmation. It sends file bytes directly to authenticated Core, reads the new item back from Core, or checks that the original item stayed at the selected revision before starting a local download. The control tool returns no original file bytes. A cancelled choice has no effect, and an uncertain upload requires Library inspection before retrying | Core global Library and artifact store; requesting browser owns local file selection and download | Butler tool, component and production LAN browser |
| Connect or remove a Notebook file index | Interactive Butler reads `nebula.notebook.file_index_status`, which exposes configured state and project revision without the host path. `connect_file_index` asks the requesting browser to choose a SQLite index; Core validates that it indexes the selected project folder, checks the revision, and returns its saved state. `remove_file_index` asks the browser to confirm removal at that revision, then Core read-back shows no selection. The host index file remains. Scheduled Butler has no file-index mutation action | Core Notebook service, project revision and requesting-browser host picker | Butler tool, Core API, component and production LAN browser |
| Store or download project Evidence | Interactive Butler pages selected-project evidence metadata, reads an exact record and revision, and prepares an upload or original-file download. The requesting browser chooses the file, title, type and project links, checks the file hash, and reports a stored result only after Core detail and list agree. Original download requires confirmation and byte-for-byte SHA-256 verification. Cancellation transfers nothing; an uncertain upload requires inspecting Evidence before another attempt. Butler receives no original bytes, and scheduled Butler has no device-file action | Core evidence and artifact store; requesting browser owns local file selection, confirmation and download | Butler tool, Core API, component and production LAN browser |
| Brief recent agent work | Butler resolves a named Assistant, pages saved activity for the requested number of hours, includes delegated agents, and cites the source chat and record for claims about progress or repeated attempts | Core transcript, turn ledgers, harness activity, subagent lineage and goal reports | Tool unit, real Core and browser |
| Start or guide work | A new chat or mission uses an enabled listed runtime; an existing chat receives a durable queued follow-up or live harness steer; a mission uses the existing start or steer path | Core chat queue, harness and mission services | Tool unit and real Core |
| Start a scoped browser mission | Interactive Butler pages active security browser sessions with `nebula.work.browser_sessions`, then starts a selected-project mission with an exact session, target URLs, the passive risk class and lease limits. Active testing and credential grants remain operator-controlled. Core checks the same scope and session policy as the Browser form. Butler reads the saved mission ID and status; completion and findings require later mission inspection | Core browser session, scope policy, automation lease and mission; URL selects Browser or Missions | Butler tool, Core API and production LAN browser |
| Interrupt work | An exact selected chat turn or mission is stopped through its existing Core lifecycle and read back | Core turn or run | Tool unit and real Core |
| Ask for a task plan | The chat's Assistant settings asks any provider or harness agent to share a plan on future turns, or Butler records one exact one-task request with the target revision and reason. Normal tools remain available, and the activity view shows whether a plan was shared | Core assistant config, directive, and task plan | Tool unit, component, browser and real Core |
| Hand off a previously planning-blocked goal | Butler reads a paused harness goal and uses a revision-checked action after Core verifies that its latest failed turn was never admitted and ran zero tools. Core links one provider draft to the paused source; the new planning preference is advisory | Core goal, turn, queue, approval, provider, and handoff records | Core unit, component, real Core and browser |
| Edit a goal's resume instruction | Chat → Goal → Edit goal shows the saved text; the current Core wording is the default. Saving it updates the goal by revision, and the next explicit Resume sends that text in the same conversation. Automatic continuation turns retain their separate Core instruction | Core goal record and resumed turn; goal editor owns the draft | Core unit, component, real Core and browser |
| Recover a harness goal from model capacity | When a Core-owned harness goal turn stops because the selected model is overloaded, Core waits briefly and starts one new continuation in the same chat. It uses the saved goal and completed read-only receipts, never replays the failed prompt, and counts the new turn against goal budgets. A second overload, a write, an uncertain effect, or a policy refusal leaves the goal paused with its saved reason for review. The transcript and goal state survive refresh. This automatic behavior has no Settings control or alternate-provider action | Core turn ledger, goal revision and chat transcript | Focused Core tests and production LAN browser |
| Complete a provider goal | A tool-enabled provider agent calls the offered `goal_complete` tool (`goal.complete` in Core) with a summary and concrete evidence after meeting the goal criteria. Core saves the completed goal with the final answer, labels the evidence agent-reported, and starts no further continuation; the goal panel shows the saved state after refresh | Core goal, turn ledger, final message and goal panel | Core unit and real Core browser |
| Switch a saved harness chat to a provider | Chat → Assistant settings → Runtime → Provider checks the chosen provider and model, then Core changes the same chat's durable binding at an idle boundary. The chat ID, transcript, goal ID, budgets and goal status stay in place. A running goal follows the selected runtime at the next safe turn boundary. Core retains a pending selection while a response, unresolved action, child or authorized follow-up still owns work; paused and blocked goals remain so until explicitly resumed | Core chat binding and goal; React owns only the unsaved picker choice | Core unit, desktop/mobile browser, real Core and LAN |

PostgreSQL storage migration is a host administration workflow. Butler can explain
`nebula-core migrate-to-postgresql` and the procedure in
`docs/postgresql-migration.md`, including source inspection, stopping Core, an
empty target, verification, and restoring the previous configuration after a
failed cutover. The command accepts its target through a protected URL file or
`NEBULA_V3_DATABASE_URL`; Butler does not request database passwords in chat or
perform the offline copy through a browser Settings action. A successful copy
preserves source data and configuration until the administrator switches Core.
The same data directory retains artifacts, workspaces, credentials and separate
memory, checkpoint and vector stores.

The setting catalog lists every Settings section, Butler's Voice control, and
the Nebula-wide Butler instruction editor. Dedicated actions handle
device appearance, provider and harness default models, post-tool preferences,
project execution mode, target scope and headless browser sessions. For other
Core-backed settings, `nebula.settings.api_schema` lists only the current build's
Settings routes and request schemas. `nebula.settings.api_request` calls one of
those existing routes through Core authentication. Pairing and device mutation
routes are excluded because their one-time link and destructive confirmation
belong to the browser. The selected project ID is
supplied by Core, and the assistant cannot select a different project in a
route. Existing API validation, revision checks and required acknowledgments
still apply. Browser appearance remains device-owned. Uploads and local desktop
operations remain subject to the capabilities of the active device. An explicit
operator setting request authorizes the assistant action without a second
confirmation dialog. A pending browser action is never reported as saved.

For runner profiles, Butler can rename or enable an existing trusted profile at
its observed body revision. New profiles and changes to the runtime, executable,
context, socket, platform, isolation or seccomp selection require the requesting
browser's Sandbox runners control. Omitted runner fields have Core's replacement
defaults and must preserve existing trust configuration. For SSH environments,
Butler reads configured aliases without running SSH resolution, edits nonsecret
metadata, preserves an existing approval or tightens it to Ask each time, and can
forget saved settings at the observed revision. The first standing Allow grant
belongs in the browser's Environments control. Initial metadata creation asserts
absence; an existing revision cannot recreate deleted settings. Core readback
refreshes the relevant catalog while open forms retain their original revisions
and unsaved values. Saving metadata does not establish runtime or SSH health.

Spawned Assistant harness creation and changes to its program, account home,
kind or connection configuration require `setting_id=harnesses`, even while the
profile is disabled. Health and quota reads can start that saved host program.
Butler can preserve an existing program while renaming the profile; constrained
Codex endpoint profiles can still use nonsecret Settings CRUD. Unrestricted
permission mode remains operator-owned.

Follow-up preferences use `body.expected_revision` from the current project
settings. Butler may preserve an existing cloud confirmation for the same backend
identity or revoke it. First consent, or consent after changing backend identity,
requires `setting_id=follow-up` in the requesting browser. Core atomically checks
the project revision, including direct preference changes, so a stale write cannot
restore revoked consent. Workbench rereads verified changes; an open Settings
form retains its draft and offers Reload before replacement. Runtime selection
alone does not run analysis or validate backend health.

The Settings API also discovers the Web search lifecycle, local attribution
profiles, public provider directory, fixed host-loopback provider discovery,
provider and harness health, model verification and account observations.
The OpenAI provider is distinct from a Codex account harness. Its optional
Daybreak Blue choice uses OpenAI's Responses API `access_programs.cyber` value
`daybreak_blue`; Codex app-server uses `cyberAccessProgram=daybreakBlue`.
Web search actions read current runtime status after the operation; their
verified receipt refreshes the mounted runtime and project-policy controls.
Failed stop/remove commands preserve the last saved state and require a fresh
status read before an explicit retry. The status refresh icon only reads Core.
Local profile creation and activation read their exact saved identity; edits,
activation and deletion use observed revisions and Core's one-active-profile and
attribution dependency guards. Profile changes refresh the local catalog.

Skills, native hook metadata and project instruction status use the selected
project supplied by Core, including a linked workspace and the existing managed
skill root. The model supplies no query-string project override. Account refresh
POSTs return an observed balance/quota or explicit unavailable reason without
exposing keys or claiming a profile write. Runtime actions retain their original
API response and add bounded `observed_settings` from the related status route.
An accepted setup operation remains pending until its observed phase is ready.
Metadata validation uses inert container, SSH and harness I/O; it does not attest
to an actual host runtime or vendor account.

The Notebook destination opens the selected project's Workbench Notebook view.
Project Policy contains execution mode, Host consent acknowledgement, and package
network access. The MCP server catalog owns the Notebook grant. Butler's
capability search and Settings catalog expose the typed Notebook action family;
the `read` and `change` gateway routes to Core's project-scoped Notebook service.
Butler must read a file revision or cell hash before a write and read Core again
before reporting the saved cell. After an operator asks it to execute a cell,
Butler calls `nebula.notebook.execute` with the current source hash, reads the
returned run ID from Core, then waits or pages ordered events before reporting
the result. If the execution response is uncertain, it calls
`nebula.notebook.recent_runs` for that path and hash before any retry.
`nebula.notebook.interrupt` targets one exact run ID and is read back from Core.
Package lookup requires the project's network permission; pinned installation
and exact idle-kernel restart are in the interactive catalog with Core read-back.
For file-index changes, Butler reads the project revision without a host path.
The requesting browser chooses the host SQLite file or confirms removal, and
Core checks that revision before saving. The host index must describe the
selected project folder; removal does not delete the index file. Host consent
stays on the operator's settings control. Scheduled Butler does not receive
these Notebook actions.
Butler lists selected-project storage sources without token digests or callback
credential references. Enrollment opens the visible Storage settings form; the
requesting device chooses a runtime and callbacks, saves the source, and holds
the one-time webhook token. For an existing source, Butler can prepare rotation
or revocation at its current revision. The requesting browser confirms the exact
source, Core fences the revision, and the browser reads the saved status back.
A rotated token appears only in that browser's temporary panel and never enters
the model transcript. If the response is uncertain, inspect the source before
another rotation. Butler can prepare enabled-state and reporting interval
changes for browser confirmation and Core readback. Callback editing opens the
exact source form on the requesting device, where its credential reference
stays out of the model transcript. Opening that form does not claim a saved change. Remote storage actions
still require a source preview and an authenticated operator approval in the
Storage interface.
The headless security session is independent of the visible-browser desktop
cutover. It needs the verified Chromium runtime and the mitmdump security proxy.
The Mac desktop shell supplies the bundled addon and discovers mitmdump in its
trusted system paths or accepts an explicit absolute executable path. A missing
proxy is reported as a startup failure instead of a saved session.

State owners: the URL owns the active project, Core owns the conversation and
Core settings, browser storage owns device preferences and the last conversation
pointer, and React owns window position, visibility, and unsent text. A
project-scoped action uses the currently selected project and names it in the
confirmation. With no project selected, the assistant asks for a selection.
An explicit navigation command is a local UI action rather than a model turn:
it opens the same Settings lens as a catalog result, even without a configured
model. Ambiguous or conversational requests continue to the selected runtime.
An explicit device-theme command follows that same local path, including when
no project or model is selected. The assistant shows the Appearance lens, applies
an advertised theme, checks the active theme, and reports whether
browser storage retained it. A question about how themes work remains a model
question. The legacy “Zero theme” name resolves to Zero Dark on this device.
For requests that reach a model, `nebula.settings.open`,
`nebula.settings.set_device_theme`, and
`nebula.settings.set_project_mode`, `nebula.settings.set_project_scope`, and
`nebula.settings.headless_session` expose exact bounded actions. The tool receipt
is an intent, not a completed browser change. The requesting assistant window
validates the action, applies it, and reports the observed result. The
`nebula.settings.catalog` tool lists exact action names and argument values when
the model needs to discover a destination or capability. Replayed tool events
are deduplicated by tool-call id. Browser action outcomes remain attached to
their Core message in browser storage so the requesting device shows the
confirmation again after refresh. The transcript follows new results into view.
The general assistant never receives these tools.
The dedicated assistant also has `nebula.work.catalog`, `nebula.work.overview`,
`nebula.work.chat`, `nebula.work.mission`, `nebula.work.browser_sessions`, `nebula.work.start_chat`,
`nebula.work.message_chat`, `nebula.work.stop_chat`,
`nebula.work.start_mission`, `nebula.work.steer_mission`, and
`nebula.work.stop_mission`. These typed tools are bound to the selected project
by Core. They list targets and enabled runtimes before an action; they do not
accept arbitrary API paths, workspace commands or cross-project target IDs.
For the selected project's Work board, Butler discovers
`nebula.work.work_items`, reads `nebula.work.work_item`, and uses
`nebula.work.create_work_item`, `nebula.work.update_work_item`, or
`nebula.work.check_in_work_item` after an operator request. Creation and
check-ins use stable request IDs; edits and check-ins use the current item
revision. Butler reports the saved Core result and board link. A Work check-in
does not run or complete an assigned assistant chat.

### Project workspace files

Interactive Butler uses `nebula.workspace.list` to find a selected-project file or folder. It returns an opaque directory identity for the exact listed directory. File replacement or rename uses the current full SHA-256; folder creation, rename and deletion use directory identities and Core read-back. `nebula.workspace.upload_from_device` asks the requesting browser to choose a file for a listed directory. The browser hashes selected bytes on HTTP LAN origins, checks the current destination, confirms any overwrite, and sends bytes directly to authenticated Core with content and directory preconditions. It reports success only after exact entry-state read-back. Cancellation or an uncertain response requires inspecting Files before another attempt. `nebula.workspace.download_file` asks the requesting browser to verify bytes and start a local download. Butler never receives a device file path or binary payload.

`nebula.workspace.preserve_evidence` also requires that full hash, promotes the exact regular-file bytes into immutable artifact storage, verifies the saved Evidence project, path and SHA-256, and refreshes Evidence in the requesting project. A stale hash creates no Evidence. `nebula.data.system kind=workspace_reset_status` reads current managed-workspace reset eligibility, including active terminal and reviewed-execution counts. A linked host folder is always ineligible. Bulk reset is deliberately not model-callable: Butler opens Files, where the operator reviews the fresh status and types the exact project name before Core removes anything.

### Project terminal lifecycle

Interactive Butler uses `nebula.terminal.list` to read live session IDs, creation
times, states, and attachment status in the selected project. This read never
issues a WebSocket ticket or returns shell output. `nebula.terminal.stop` returns
an exact pending action to the requesting browser. The browser checks the same
session against fresh Core state, asks the operator to confirm loss of running
commands and container-only packages, and sends a conditional project-scoped
close. It reads Core again before saying the terminal stopped. Cancellation has
no effect; an uncertain response or failed read-back asks the operator to inspect
Terminal before retrying. Opening Terminal through `nebula.navigate.open` uses
the existing browser UI to start or reconnect a shell. Scheduled Butler cannot
list or stop terminals.

### Butler report control

Interactive Butler uses `nebula.reports.list`, `nebula.reports.sources`, and
`nebula.reports.detail` to find a report, source findings and notes, and its
current revision. `nebula.reports.create` asks the requesting browser to save a
draft with a stable report ID; Core validates the references and reads back the
draft. `nebula.reports.update` edits a draft or review report against its current
revision. `nebula.reports.source_note` pages a selected-project note with its
current revision. `nebula.reports.transform_note` prepares an exact report and
source-revision browser handoff. The browser uses the existing writing service
and permitted runtime catalog, requires one-request cloud consent where needed,
and shows an editable AI draft. The operator saves only after review; the
browser rereads the report and note before writing and confirms the exact
transform in Core. The original note remains unchanged.
`nebula.reports.signoff` opens the existing operator sign-off form; the
operator must personally attest before Core finalizes it.
`nebula.reports.delete` asks this browser to confirm a named non-final report at
its current revision, then checks exact Core absence and refreshes the list.
Core refuses signed final reports and reports still referenced by saved renders.
For a PDF download,
`nebula.reports.export_pdf` asks the browser to render and confirm the exact
report revision. `nebula.reports.export_bundle` asks for separate confirmation
before downloading the sensitive project bundle. Butler receives no file bytes.

### Project lifecycle

Interactive Butler also discovers `nebula.projects.list` and
`nebula.projects.detail` to page the operator's projects, including archived
ones, and read an exact target revision. Archive and permanent delete require
the same project-specific confirmations as the project switcher. Its typed
create, rename, archive, restore, and delete actions issue a scoped pending
browser action. The requesting browser uses Core's project routes, checks the
target revision and
reads the saved result before reporting success; a cancelled dialog saves
nothing. `nebula.projects.create` uses a managed workspace.
`nebula.projects.create_linked` opens the host folder picker in the requesting
browser, then Core resolves and saves that folder with the new project. The
browser reads back the exact project path and pending Host consent before
navigating; the model never supplies a host path. This creates a new project
and does not relink an existing workspace. Host execution consent remains in
the visible project policy control. Scheduled Butler cannot discover
or invoke these interactive project actions. An uncertain create response
requires a fresh project search before retrying. The requesting browser checks
all project names before creation and refuses a sequential retry if Core saved
the first request, including when that project is archived. Each Butler create
receipt carries a UUID retry ID to Core. Core saves the ID, operator identity,
request fingerprint, and project ID in the same transaction as the project,
default scope, and Host policy. Reapplying that receipt returns the same project;
changing its details or reusing it after project deletion fails with a conflict.
Core serializes Butler project names at the write boundary, so concurrent Butler
receipts cannot create the same normalized name. Other project creation paths
keep their existing semantics.

### Project Work and goals

For agent access, Butler reads `nebula.work.work_setting` before calling
`nebula.work.set_work_setting` with the current project revision. The Core
read-back confirms whether the selected project's built-in agent Work tools
are enabled. Disabling them leaves existing items and check-ins visible.
For older check-ins, Butler pages `nebula.work.work_updates` by item and reads
`nebula.work.work_update` for exact summary, next step, or blocker text in
bounded chunks. The item and update must belong to the selected project.
The dedicated assistant reads and edits the saved goal resume instruction
through `nebula.work.goal` and `nebula.work.update_goal`. It discovers those actions in its work
catalog, reads the exact goal and revision, and updates only the named goal.
The Chat destination accepts "goal settings" and "goal resume instruction";
the operator then selects a conversation and opens its goal editor. The instruction applies to an explicit
Resume action, including one invoked by Butler; it does not change automatic
continuation turns or resume a paused goal merely by saving the edit.

`nebula.work.chat_settings` and `nebula.work.set_chat_settings` expose the saved
`planning_enabled` switch with revision checks and read-back. The switch lives
in each conversation's Assistant settings. Provider, Codex, and Grok agents
receive an advisory planning request and can share structured steps with
`plan.submit` while their normal tools remain available. A missing plan never
blocks the turn. When a plan is shared, Core includes it in a delegated
subagent's task context; a harness-reported native plan is a fallback. Child
agents inherit the planning preference and may share their own plan.
Butler can save the main runtime and a provider or harness child runtime independently on the same chat.
Core applies an idle runtime selection to the same chat immediately. Active work
keeps its recorded runtime; a separate pending selection activates at the next
safe boundary. Butler reads the durable binding and pending state before reporting
a switch or resumed goal. Immediate messages
carry the same saved MCP servers and Plan choice as queued next messages.
For a harness-to-provider change, opening the saved chat's Assistant settings
and choosing Provider uses Core's runtime-switch preflight and apply actions.
Core reports the active runtime separately from any saved pending selection.
Activation waits for the response, unresolved approvals/recovery, subagents and
previously authorized follow-ups to finish. A blocked goal without in-flight
work does not prevent selection; paused and blocked goals keep their state.
The running Core goal's next turn uses the activated runtime in the same chat.
Refresh and reconnect retain the pending choice. If privacy, model availability
or compaction consent changes, Review selection remains visible and the old
backend is not used as a fallback. Explicit resume reconciles an older split
between saved Assistant configuration and chat binding without requiring a new
operator message. Native vendor goals remain separate.
`nebula.work.require_plan` records a one-task request against an exact
agent, goal, child, or turn, using a fresh target revision and concrete reason.
An active harness turn reports that the directive takes effect next turn. The
saved plan and Butler reason remain visible in the chat activity after refresh.
Butler's automatic one-task force requires the project's delegated action grant,
current Core evidence, and a recorded reason.
### Hand off a paused harness planning goal

To hand off a paused harness planning goal to an eligible provider such as
DeepSeek, interactive Butler uses `nebula.work.handoff_goal`. Core creates one
provider draft only when the latest planning attempt stopped before admission
with no tool calls.
The source retains its queue and remains paused. Core checks pending approvals,
selected provider model/tool verification, MCP selection, privacy scope, and
remaining budgets before linking the goals atomically. A repeated request reads
back the same continuation. Butler starts it through a separate goal action;
the source and target link and reason appear in both goal panels after refresh.
The handoff carries neither approval nor queue authority. For a scheduled check,
`nebula.global.handoff_goal` performs the same checked create and can start the
new draft under an Act grant. When handoff is unavailable, Butler reports the
hold and can select another eligible project.

### Inspect conversation activity

`nebula.work.timeline` accepts a listed conversation ID or an Assistant title and
a bounded number of hours. Ambiguous titles return selectable conversations. The
read-only result pages saved messages, provider tool steps, selected harness
activity, delegated-agent lifecycle and schema-checked goal reports. Each row
has a source ID, time, and conversation link. Butler follows the cursor before
calling a brief thorough, distinguishes agent-reported progress from observed
execution, and treats unrecorded intervals as unknown rather than idle time.
A requested chat follow-up is saved to Core's durable queue. Live steering is
available only for an active harness chat or harness mission. A provider chat
can receive a queued next message; a provider mission cannot be live-steered.
Starting work reports its saved ID and current state, never a completed result.
For an older saved message, Butler pages `nebula.work.chat_messages` using
`next_before_message_id` and selects the exact active user message. A stale
revision, active response, other project's chat, or replaced message prevents a
rewind. On success, the same chat ID remains and Core records which messages
were retracted. A subsequent replacement turn must be observed independently;
rewind alone does not mean an agent answered the edited prompt.
For a transcript export, Butler selects an exact chat from `nebula.work.chats`
or `nebula.work.chat`, then invokes `nebula.chats.export` with its current
revision. The requesting browser presents the same sensitive-data confirmation
as the conversation menu. It reads all active saved messages from Core and
checks the chat revision again before starting the local Markdown download.
The model receives no complete transcript. A cancelled or stale handoff is not
a saved file, and a browser download start does not establish the device's
final save location.
For shared Library items, interactive Butler can page bounded metadata and
read one item's current revision. Add uses a picker and global-impact
confirmation on the requesting browser, followed by Core detail and list
read-back. An uncertain upload response is not a success receipt: inspect the
shared Library before selecting the file again. Download uses exact item and
revision checks before and after Core artifact retrieval, then starts a local
browser download. The model receives no original file bytes through these
control actions. Reindex and remove are exact browser
handoffs: the requesting browser rereads the item, names the global impact in
a confirmation, checks the revision again, sends a revision-guarded request,
and reads Core's saved result before reporting success. Cancel leaves the item
unchanged. The control action returns metadata only; the browser owns original
file transfer. If Butler's Knowledge setting is enabled, Core may separately
retrieve bounded Library excerpts into the model context as reference data.
Scheduled Butler has no shared Library mutation action. The operating system
owns the final download save location.
For project Evidence, interactive Butler uses `nebula.evidence.list` and
`nebula.evidence.detail` to page bounded records and select one exact revision.
`nebula.evidence.upload` opens a requesting-browser file picker and editable
metadata and link form. Core validates same-project finding and asset links and
stores an immutable original; the browser compares its SHA-256, filename, size,
links and saved list before reporting success. A failed or uncertain upload
does not automatically resend the file. `nebula.evidence.download` rereads the exact
record, requires device confirmation, and checks the returned bytes against
Core's digest. Butler receives no file bytes through either action. The final
download location is controlled by the operating system.
The existing Settings controls remain available. Browser and notes mutation
tools are separate future work; this slice does not grant them.
The packaged app destination list covers the All projects page, the selected
project overview and its Work, Assets, Evidence, Sources, Results, Findings and
Reports pages, all eight Workbench views, Library and Settings. Its entries
hold route templates, labels and aliases. The browser supplies the selected
project ID and uses React Router navigation; the model receives only destination
IDs and a bounded `nebula.navigate.open` action. A destination that needs a
project opens All projects for selection when none is active. Dynamic resource
links such as an individual finding or conversation require a selected record
and are not guessed from a name. URL state remains authoritative after reload,
back and forward. Settings mutations remain exclusive to this assistant.
An explicit project execution-mode command is also handled by the UI so the
operator sees the existing control before Core saves with Host acknowledgment.
The requested source mode, when supplied, must match Core's current policy;
the assistant preserves unrelated policy fields and uses revision checks. A
failure leaves the policy unchanged and keeps a retryable draft. The saved
mode must be read back, and the visible control must reflect it before the lens
closes. Existing sessions
retain their frozen policy revision; new sessions use the new mode.
The assistant also answers Nebula usage and troubleshooting questions. Its
read-only project-context tool reports the selected project's current policy
and linked workspace without creating or changing a policy. In Docker mode,
commands can work inside the project workspace; an arbitrary host path is not
available merely because the host has it. Linking the intended folder as the
project workspace or, with explicit Host access consent, switching future
sessions to Host mode are the two relevant remedies. The assistant must explain
that a previously opened command session keeps its frozen mode. Its product
help scope does not grant filesystem commands, arbitrary MCP servers, web
search, or permission to use settings tools from general chat. General chat
still does not receive Nebula settings tools. When its answer cites a known
Settings destination, the UI opens that destination as a lens on click.

## Core goal troubleshooting

For a tool-enabled provider goal, a final answer alone leaves the Core goal
running. The agent calls the offered `goal_complete` tool (`goal.complete` in Core)
with a completion summary and concrete
evidence after meeting every criterion. Core checks the active turn and saves
the completed goal with the final answer in one transaction, then stops
automatic continuations. The goal panel shows this evidence as agent-reported;
the call does not independently verify the research claim.

For a Core-owned goal in a harness chat, the active agent can call `goal.block`
with a concrete reason. Core saves the blocked goal and stops automatic
continuations; the goal panel shows the reason and lets the operator resume.
Blocking a Project Work item or a native harness goal does not block the Core
goal. Core pauses a goal for review after three automatic responses with no
recorded tool activity, so a status-only loop cannot continue indefinitely.
The agent's block action is scoped to the goal on its active turn and cannot
choose another conversation's goal.

## Nebula-wide Butler instructions

An operator may create one `.butler/BUTLER.md` under Core's data directory
(`--data-dir` or `NEBULA_V3_DATA_DIR`) through Settings → Automation → Butler
instructions, or by placing a file on the Core host. Nebula does not bundle a
default file. The authenticated `GET` and `PUT /api/v1/butler/instructions`
endpoints expose the saved text and support digest-checked updates. Settings
opens the editable Markdown code editor directly from its popup catalog entry;
the full Settings page also accepts a UTF-8 Markdown upload or direct edits and confirms the saved version
through a fresh Core read. Its path and read state also appear in Butler
Oversight. Core reads a regular,
UTF-8 file without following symlinks, limits it to 32 KiB, and reads it again
for every scheduled run. An edit or removal during a run stops later writes
from that run; a new file version is considered at the next check.

When the file exists, Core records one Nebula-wide periodic check after the
saved check interval, five minutes by default. The operator changes it in
Settings → Automation → Butler check interval from one minute to one day.
Interactive Butler can also discover `butler-check-interval`, read its current
value and revision through the Settings gateway, and save a requested interval
with that revision in the PUT body. Core readback must confirm the requested
minutes; a conflict requires a fresh read before retrying.
Core persists the interval with a revision guard, reads it for each due check,
and reports the next due time from the last saved check. Reducing the interval
can make the next check due immediately. It can run with no running goals. A
configured Butler chat in an Act project hosts the run. Butler reads the live
Act-project roster and chooses where to work. Core still enforces each target
project's Act revision and privacy boundary for reads and writes. The
scheduled harness run uses the model and reasoning effort saved in that Butler
chat's settings. Changing the effort applies to new runs and the next direct
message; an already running turn keeps its selected effort. Global actions
can resume a paused project or main goal, create and start a
main goal, create one linked recovery goal for a paused or blocked main goal,
hand off a planning-blocked harness goal to one linked provider goal,
or create a managed child project under an Act parent. A child may
receive an initial goal as part of its creation; it otherwise starts with the
normal Watch authority. Goal and child-project creation check existing work
before writing. The model cannot grant Act to another project, expand scope,
or treat a pending approval or uncertain write as permission to retry.
Butler can also read and edit every saved next-turn Assistant setting on a main
chat, every saved project Assistant default for future chats, and every editable
field of a nonterminal main goal, including token, active-time, step, and child
budgets. The direct and scheduled catalogs use the same Core field schemas as
the Assistant settings and goal services. A scheduled cross-project edit requires
the target project's current Act revision plus the target chat or goal revision.
Increasing an exhausted budget does not itself resume a goal: Butler reads the
saved result, checks other holds, then invokes resume on the new revision.
There is no fixed Butler investigation time, model-step, or tool-step budget;
the project lease is renewed while it works. Losing Act authority or the lease
still stops the investigation, and Core's target policy and approval checks remain
in force. A check with no running main goals and remaining paused or blocked
main goals stays marked as needing attention when Butler only reads or changes
unrelated state.
A Core-verified Butler save refreshes the mounted interval control immediately.
An unsaved operator draft stays intact and requires Reload before saving.

When Butler sends immediate guidance to a blocked main agent, Core keeps that
scheduled check open and follows the accepted chat turn. When the turn finishes,
Butler reads its exact saved reply and can answer or take another scoped action
in the same check. Further replies continue through that case without waiting
for the next scheduled check. A held message, missing saved reply, failed turn,
or unresolved blocked goal remains visible as needing attention. Core bounds
the exchange and preserves the turn and action receipts across worker restarts.

Butler Oversight displays the durable scheduled-check transcript as a compact
section. Expanding a check reads every saved harness attempt and its paged event stream
from Core; it survives refresh and reconnect. A Codex `serverOverloaded` failure
can retry the same case once after Core confirms no change was attempted; the failed
attempt remains visible beside the retry. Quota exhaustion does not trigger this retry.
The check's linked recovery goals
show their current Core status and report in that same section. For recovery
work Butler records the main agent's plan, then creates a linked Codex goal
using an advertised `gpt-6-sol` route with `max` effort and Daybreak Blue.
The main agent passes its shared plan to subagents; subagents can also share
their own plan without a tool gate. Butler can query Codex or Grok's reported
included quota and credit balance through the profile-scoped `nebula.work.harness_quota`
and `nebula.global.harness_quota` read tools. The Usage view shows both balances
even when the included window is exhausted. Codex credits and Grok prepaid credits
are vendor units; a positive balance is separate from included usage and does not
guarantee admission if a vendor denial or spending limit applies. Grok's included-credit period may be
shared with other Grok products; missing vendor values remain unknown.

| Journey step | Observable invariant | State authority | Test layer |
| --- | --- | --- | --- |
| Discover | Butler Oversight shows the path, file state, and last scheduled check | Core data directory and global clock | Component, real Core and browser |
| Schedule | Exactly one global check is recorded after the saved interval, five minutes by default, even if all goals are idle | Core global state and event stream | Core unit and real Core |
| Set interval | An authenticated operator saves a one-minute to one-day interval with a fresh revision and reads back the value and next due time | Core global state | Core unit, component, and real Core |
| Choose | Butler can inspect all eligible Act projects before selecting work | Core project and authority records | Tool unit and real Core |
| Set effort | Saving an advertised effort in Butler settings is reflected in the next direct turn and scheduled harness session | Core chat settings and harness runtime options | Core unit, component and real Core |
| Edit Assistant options | Butler can discover and save every Core next-turn Assistant field and project default under Act, and read back the saved revision | Core chat and project records | Schema parity, Core unit and real Core |
| Use Assistant work controls | Scheduled Butler can list and invoke every project Assistant work action through `nebula.global.work_catalog`, `work_read`, and `work_change`; Core checks target Act authority, the action schema, project ownership, and saved revisions | Core Work catalog and action handlers | Catalog parity and Core integration |
| Renew goal limits | Butler can extend a paused main goal's active time and step limits, then resume it on the new revision after reading other holds | Core goal and recovery ledger | Core unit and real Core |
| Continue blocked check-in | Immediate guidance is followed through the main agent's saved reply, and Butler can respond again in the same case before it closes | Core case, chat turns and messages | Core unit and real Core |
| Review transcript | Opening a saved check shows every attempt, paged activity, and linked recovery-goal reports after refresh or reconnect | Core case, harness turns, event streams, and goal records | Core unit, component and real Core browser |
| Read quota | An agent can query a configured Codex or Grok profile for vendor-reported remaining percentage and separate credits; missing values stay unknown | Harness vendor read and Core quota observation | Core unit and real Core |
| Retry server load | One failed scheduled analysis retries in the same case only after the exact vendor load code and absence of write attempts are verified | Core case and harness event ledger | Core unit, component and real Core browser |
| Act | A stale Act revision, removed file, local-only boundary, or stale goal revision prevents a write | Core policy, instruction digest and goal records | Core unit and real Core |
| Reconnect | The clock and action receipts survive Core restart without duplicating a project or goal | Core database and recovery cases | Core unit and real Core |

## Bundled product guidance

The product knowledge index is generated from current top-level product
documentation, the Settings catalog, and this contract, chunked by headings with source paths and
content hashes, embedded locally, and packaged as a compressed text resource
that the package build generates and Core loads as a read-only SQLite vector database in memory. A full-text index answers immediately if the local embedding
model has not been prepared; dense retrieval is added when that model is
ready. Build verification rejects an index whose source digest no longer
matches the documentation. The index describes product behavior; Core-owned
project policy and workspace state always come from the live context tool. On
startup, Core mirrors the bundled passages and their precomputed vectors into
the local Chroma product-help collection. The source digest and passage count
determine whether the mirror needs replacement; product help remains searchable
from the bundled index if Chroma is unavailable.
For MCP cost questions, the assistant points to **Assistant settings → MCP tool
context** and Project policy for Always loaded tools. New UI provider chats
explicitly choose `mcp_context_mode: on_demand`: selected and eligible on-demand
sources send a brief directory; only `tool_catalog.load` introduces full schemas
for this turn. Ranking supplies hints only, never automatic preloads. A fresh
turn clears deferred schema presence, not historical messages or manifest
revisions. Routing, compaction, reconnect and resume within the current turn keep
the exact manifest. The accessible mode selector also offers **Always load
selected schemas** (`always_loaded`), the explicit opt-in to selected native
schemas and legacy carried/preloaded catalog schemas. Existing saved chats
missing this field retain `always_loaded`. A saved mode change applies next turn
and never mutates an active request snapshot. Project-pinned tools stay loaded;
mandatory Core tools and native harness behavior are unchanged. Conversation
on-demand mode retains its catalog gateway even when project deferral is off.
`set_chat_settings` discovers and saves `mcp_context_mode` beside MCP selections;
read current settings, preserve unrelated values, then read Core's observed
saved value before saying it is saved. Defaults, forks, retries, goals and
schedules carry the saved choice. Each assembled request has one canonical definition per loaded tool;
stateless providers still receive their required native declarations on every
request. A directory entry or ranking suggestion is not a loaded schema.
Unchecking a provider chat's MCP server excludes every tool from that source on
the next turn: no complete schemas, directory entries, discovery, ranking or load
reinsertion. The conversation Assistant settings show “Excluded · Not sent next
turn”; their server-level Exclude action also handles on-demand sources. Rechecking
restores the server. `set_chat_settings` discovers and saves
`excluded_mcp_server_ids` alongside `mcp_server_ids`; read current settings first,
preserve unrelated choices, and read Core's saved result before claiming success.
Core derives exclusions from deselected IDs, distinguishes them from untouched
on-demand sources, and preserves them across refresh, forks and automatic goals.
Current in-flight/resumed turns retain their frozen snapshot and historical
manifest revisions and transcripts remain unchanged. Missing exclusions in old
snapshots default empty; native harness behavior and permissions are unchanged.
Disabling or revoking the source removes its
active definitions and execution permission; historical calls remain. Jev is
optional, respects the existing project privacy opt-in, and ranks only eligible
definitions absent at their current complete digest. Native harness tool loading
keeps its existing behavior. Prompt-cache savings are not implied by persistence.
Provider turns request low effort; Core's provider adapter maps or omits that
control for the selected model. Harness model options determine low effort and
fast selection. An existing harness conversation may rebind to its advertised
fast sibling while retaining the Core transcript.

### Standing instructions and retained native sessions

Current standing instructions are authoritative independently of a lossy summary.
Core saves exact instruction and goal-skill snapshots. Supported stateful native
channels retain unchanged instructions once per native context; reconnect and
compaction preserve that channel. An instruction edit or removal starts a fresh
native context when the adapter cannot replace its current instruction head,
keeping the Core conversation and a bounded historical handoff. Core reports the
change in the same chat. Unknown or unverified native instruction delivery fails
visibly before a message is sent; it is not reported as delivered.

Stateless provider requests continue carrying the current instructions on every
request. Native harness processes still require their advertised protected memory
runner; an unavailable runner or unverified installed vendor channel is a concrete
capability limitation. Do not claim an installed vendor passed merely from an
inert protocol fixture. A code rollback leaves projects needing these context
capabilities on a compatible execution worker, while compatible API/UI updates
and other projects may proceed.

### Canonical host Settings and public web search

The independent Core host owns shared Settings observations, session credentials
and external setup effects. Butler's allowed Settings requests reach the same
canonical host state as the browser. A pending durable receipt is not a saved or
applied result; follow it and read back the authoritative value.

Runtime discovery and selection belong to the host. Image preparation, its
status, cancellation and retry belong to the selected Project and exact accepted
operation. A host runtime repair must become visible to that Project without
replacing an existing image operation. Terminal capacity is observed for that
Project; unknown or unavailable capacity offers a check or retry and never
implies an available terminal slot.

Account observations use the same protected reader from Settings, Butler and
automatic quota updates. An unsupported native reader reports unavailable and
retains its blocked memory job; it must not start an unrestricted account process.
A configured account endpoint remains an observation of that endpoint.

The host owns global Library uploads, derived-index publication and verified
artifact downloads. An indexed result is ready only for the matching saved
request and content digest. Removing a Library entry removes it from retrieval
while retaining its immutable artifact. These operations do not create a global
execution Project or grant an API worker host execution authority.

Project policy's Web Search opt-in uses one host-managed service and the host's
saved engines. Queries create no project-owned service or duplicate reservation.
A memory-admission or readiness refusal remains visible; an explicit retry uses
the same owner and never falls back to an unrestricted local launch. Image
download/build remains unavailable without a verified daemon-side memory runner;
inspect and use only a fully verified cached image in that environment.

The focused test selection must cover the capability catalog, provider and
harness tool paths, Core authorization and read-back, shell visibility, settings
handoff, desktop and mobile browser geometry, reconnect, and production LAN
origin. Physical-device behavior is unverified until exercised on a device.

### Device and operator boundaries

Interactive browser controls, private browser streams, debugger input, terminal
recording consent and Butler's own authority decisions remain with the requesting
operator. Butler can open their exact catalog destination. An opening receipt
means the control is open; it does not claim the operator submitted anything or
that Core saved a mutation. The [device-boundary audit](butler-device-boundaries.md)
records those limits and rejects handoffs mislabeled as saved changes.

### Private TypeSafe, VPN and MCP Settings

For TypeSafe key entry or removal, interactive Butler opens the exact TypeSafe
API key control with `nebula.settings.open` and `setting_id=typesafe`. For VPN
profile import or removal, it opens `setting_id=vpn-egress`. MCP configuration
import opens `setting_id=mcp`: its file or pasted text may contain literal
credentials and local-program trust choices. The requesting
browser owns private keys, passwords, configuration files, storage choice and
removal controls. The model-driven Settings gateway omits and refuses these
private operations, including wrapper routes that remove backing credentials.
Public TypeSafe status, VPN profile metadata, MCP schema and reference-only
export remain readable. Ordinary nonsecret MCP profile edits and exact managed
reference attachment retain their separate controls. An opening
receipt means the form opened; it does not claim a key or profile was saved.
Closing an unsaved form clears its private input. Continue in its visible
control and use Core's public status to verify any later saved result.

Opening Butler's model and effort or Voice timeout keeps Butler visible and
selects More. Reopening these controls from Chat or Work returns to the saved
settings, rather than hiding the panel or leaving a hidden section selected.
The catalog's regular Settings lenses, Butler's own controls, scheduled activity
and the nested BUTLER.md editor each retain their own visible entry and close
controls.

Delegated browser inspection accepts only `allowed_risk_classes=["passive"]`
and an empty credential grant list. Other risk classes and credential grants
are refused before mission creation. Active testing stays in the visible
operator-controlled Browser; a passive mission receipt does not claim an
assessment result.


Nonsecret provider, Assistant harness and MCP edits preserve authentication,
sharing permission and local-program trust. The Settings gateway compares the
effective profile against a fresh Core read: omitted PUT fields and replaced
privacy objects cannot silently clear credentials or consent. New profiles use
uncredentialed, untrusted defaults. Credential attachment uses its exact
requesting-browser handoff; sharing permission and trusted program changes open
the corresponding Settings control. Renaming or enabling an existing profile
retains these fields, including a full read/edit/PUT. PATCH requires the observed
body revision; PUT carries its observed revision in Core's If-Match guard, so a
concurrent browser change cannot be overwritten after the comparison. A trusted
MCP program's launch definition also remains under its operator trust choice.

Project policy edits use the observed `body.expected_revision`; the generic
Settings gateway checks it before dispatch and Core checks it again when saving.
The first Host-access acknowledgment belongs to the operator's requesting
browser in `setting_id=network-scope`. Generic policy requests cannot create
that consent, including coercible boolean values or requests that remain in
Docker mode. Existing consent is preserved for later nonsecret policy edits.

MCP discovery, exact reads, configuration exports, edits and probes include
shared profiles and profiles owned by the selected project. Another project's
profile is excluded before export serialization, including warning text.
Nonsecret edits preserve project ownership, and new project-owned profiles use
the selected project. Probes receive Core's selected project; Core checks
ownership at lookup and again before saving a capability snapshot. A refusal
does not probe or change the foreign profile. The browser's MCP catalog remains
the visible owner of profile selection and health results.


### Project inventory and candidate findings

| Journey | Invariant | Authority | Evidence |
| --- | --- | --- | --- |
| Discover/select | Bounded pages expose only the selected project's IDs and revisions | Core catalog and store | Core and paired browser |
| Create/retry | An identical request key retains one identity; changed content is refused | Core-derived identity and validators | Core and paired browser |
| Edit | Only supplied fields change at the observed revision; links remain in project | Core record and relations | Core and paired browser |
| Refresh/reconnect | Owner lists refresh after readback and exact details survive reload | Core plus project URL | Component and paired browser |
| Failure/recovery | Stale edits leave saved state intact; a fresh revision can recover | Core revision guard | Core and paired browser |
| Verification | Candidates never imply independent confirmation or launch testing | Operator evidence and attribution | Core refusal and visible review control |

### Project Sources

Butler discovers project Sources with `nebula.sources.list`, reads an exact
source's current metadata with `nebula.sources.detail`, and reads local index
preparation with `nebula.sources.index_status`. These controls expose neither
original bytes nor private source URLs or stored index chunks.

`nebula.sources.add` opens file selection or URL entry on the requesting browser.
Core records and indexes the source; the browser verifies the exact new identity,
file digest and list membership before refreshing Sources. Knowledge retrieval
can separately supply matching passages after ingestion. An uncertain result
closes private input and tells the operator to inspect Sources before retrying.

`nebula.sources.download`, `reindex` and `remove` prepare exact browser-owned
actions at the observed revision. The browser confirms, rereads Core, and uses
project-bound revision guards. Downloads verify the original size and digest.
Reindexing requires a newer ready revision; removal requires exact absence and
retains the immutable artifact. A concurrent edit refuses the stale action and
can recover after a fresh read. Saved lists survive reload and reconnect.

The acceptance journey uses the production bundle, paired Core, synthetic local
files and an inert URL-document transport. It covers private input cancellation,
capture failure and retry, creation, selection, download, concurrent-edit refusal,
reindex recovery, refresh/reconnect and removal. Public URL validation and
redirect pinning retain their separate focused Core regressions; the fixture
does not relax production network policy.
| Delete/stream | These record UI paths expose no deletion or streaming lifecycle | Existing UI contract | Not applicable to this slice |


Interactive Butler discovers assets with `nebula.assets.list` and
`nebula.assets.detail`, and creates an inventory record with
`nebula.assets.create`. It discovers findings with `nebula.findings.list` and
`nebula.findings.detail`, creates a candidate with `nebula.findings.create`, and
changes only supplied unconfirmed-record fields with `nebula.findings.update`.
Page titles and current revisions before choosing targets; description reads
are bounded. Use listed same-project assets and evidence for finding links.
These operations store supplied analyst information; they do not start a scan,
exploit, assessment, or independent verification.

Core derives a creation identity from the selected project, record kind and a
stable request key. Repeating identical creation arguments returns that saved
record while it exists; changing content under the same key is refused. An edit
requires its observed revision. Core applies the same entity and relation
validators as the public API and reads exact saved fields back before a success
receipt. That receipt refreshes Assets or Findings in the requesting browser; a
failed list refresh explains Retry without replaying the write. Saved records
remain visible after paired reload and reconnect.

Finding confirmation and edits to confirmed findings retain the operator's
evidence and verifier attribution. Open the Findings destination to review
those records in its visible controls. Butler cannot forge a verifier or turn
a candidate record into evidence that a test ran. Archived projects must be
restored before record changes. Scheduled Butler does not receive these record
controls. Asset editing/deletion and finding deletion are not operator actions
on these UI paths; this slice adds the create/edit controls already visible
there.


### Saved Results and message bookmarks

Butler can inspect selected-project saved Results with `nebula.data.results`
and `nebula.data.result`, then read the exact result or its separate presentation
hints with `nebula.data.read_result`. Content is ASCII JSON in 4,096-character
chunks with a current revision and `next_offset`; Butler never executes it.
`nebula.data.delete_result` prepares exact title/revision removal on the requesting
browser. Cancellation sends no write. The browser rechecks after confirmation,
uses Core's atomic revision guard, and requires exact absence before reporting
removal and refreshing Results. A lost response is reconciled through Core;
failed readback requires inspection before any retry.

`nebula.data.search_chats` searches visible saved project messages, optionally
one chat or bookmarks only. `nebula.data.bookmarks` pages saved bookmark states.
`nebula.data.set_bookmark` sets or clears one exact message bookmark using its
current revision, or zero when no bookmark record exists. Core owns revisions
and confirms the saved value; the requesting chat rereads its controls. Private
Butler, side, temporary and other-project conversations are excluded. These
interactive controls do not run agents or enable command execution.


### Inspect conversation and execution history

Butler can discover saved turns with `nebula.inspect.turns`, then read Core
context, message reasoning or evidence, turn summaries, hook outcomes and saved
result projections with `nebula.inspect.chat`. Read projections as bounded
4,096-character ASCII JSON chunks. Pass the returned SHA-256 on later chunks
so a changing projection is refused instead of spliced into an older one.
Project results have a separate `page_next_offset`. Private Butler, side,
temporary and other-project chats are excluded.

`nebula.inspect.calls` enumerates one chat or mission's tool-call identities;
`nebula.inspect.artifacts` pages its original metadata without storage paths.
`nebula.inspect.output` and `nebula.inspect.search_output` use Core's existing
ownership-checked, redacted line projections, bounds and continuation cursors.
Original bytes stay outside model-facing control receipts.

`nebula.inspect.executions` and `nebula.inspect.execution` expose existing
operator execution history. `nebula.inspect.execution_output` reads verified
redacted stdout or stderr in 4 KiB byte pages, with UTF-8 continuation offsets.
`nebula.inspect.cancel_execution` requires the freshly observed execution
revision and calls Core's atomic revision check before cancellation, then reads
back its actual status. A cancelling status still needs a terminal readback.
The requesting Activity history refreshes; none of these actions starts or
reruns code. Code execution and source-copy controls retain their operator review
and device interaction in Activity.

For a listed original, first read its metadata (`nebula.inspect.artifacts` or
`nebula.inspect.execution_artifact`), then use `nebula.inspect.download_artifact`.
The requesting browser confirms sensitive original content, rereads its owner,
project and revision, verifies hash and size, and starts a device download. The
control supports originals up to 20 MiB; larger originals use their existing
browser controls. Cancellation sends no content request. A browser download
start does not prove the operating system's final save location. Selecting a
saved source's preview for a chat attachment remains an explicit browser input
step; closing it transfers no excerpt to Butler.

### Core observations, resource search and guides

Interactive Butler can use `nebula.data.system` for bounded Core health, public
redacted diagnostic errors/files/settings and incident resolution, recorded
runtime usage, execution capabilities, desktop browser mode, local issue overview,
workspace reset readiness and Git status/diff, saved harness session metadata and visible-parent
side-chat labels. The paths and queries are closed in Core. Sensitive diagnostic
reveal/copy, retrying an incident, private account history import and publication
consent remain operator controls. Side-chat transcripts and private harness
owners are excluded; none of these reads starts a program or vendor model turn.

### Operator-owned review handoffs

Butler opens the exact existing control for actions whose decisive input or
consent belongs to the requesting browser. Butler activity owns improvement-issue
destinations, private repository browsing and held-draft revision. Activity owns
execution draft generation, chat attachment, editing and accept/reject review;
Chat owns post-tool result review, goal skill snapshots, external harness-session
import, Notebook pinning and destructive checkpoint rewind. Network scope owns
private scope-document import and per-target authorization. Storage owns exact
source-plan approval, reconciliation and dispatch retry. Diagnostics owns
sensitive detail and remediation. Missions owns discussion creation and permanent
mission deletion. Harness Settings owns test turns. Each handoff reports only
that the destination opened; it does not report a saved draft, import, approval,
rewind, remediation, remote action, discussion or deletion.

Ordinary revision-bound settings remain callable when Core can read them back.
`nebula.settings.api_request` can GET and PUT `diagnostics/settings`, returning the
saved logging values. For performance bottlenecks, latency, SQL timing, worker
queue delays, or event-loop stalls, GET `diagnostics/performance` through the same
tool. Discovery and schema expose this read-only operation. It returns bounded
process-local counters, recent p50/p95 timings, SQL fingerprints, and correlated
slow-request phases; a live diagnostics export includes `performance.json`.
These inclusive timings can overlap and do not establish a root cause or measure
browser rendering, model generation, or network transit. Compare snapshots before
and after reproducing the slow operation. No log-level change or paid provider
call is needed. Compatible next-turn runtime switches reuse
`nebula.work.set_chat_settings`; switches requiring transcript compaction remain
in Chat for explicit review. Provider final-answer resume is a requesting-browser
conversation transport and grants no additional model operation.

`nebula.data.search_resources` searches the selected project and shared Library.
Core excludes private Butler, temporary and side conversations before ranking and
pagination. Results are metadata with canonical resource identities. Reads return
at most 4,096 JSON characters per digest-bound chunk; use `page_next_cursor` for
another search page, preserving the query and kinds. Search never executes an
advertised resource action.

Use `nebula.data.guides` to discover the actual named guide catalog and current
local operator's saved progress. `nebula.data.set_guide_progress` requires that
operator key, current revision (zero for a new guide) and a valid step. Core
readback precedes a browser refresh; open guide input is preserved. Completion is
progress metadata, not an operator security attestation. The fixed skill and
AGENTS.md templates can be created through `nebula.data.create_starter_files` and
are verified without overwriting existing entries. Registering a hook program
remains an explicit choice in the lifecycle-hooks guide.

### Fixed runtime setup and observed egress

Use `nebula.data.system` with `setup_status` or `automation_runtime` for bounded
Core setup and the selected project's runtime availability. For a live terminal,
`terminal_public_ip` reads its reported egress address and observation time. It
never creates a terminal or returns a WebSocket ticket. No active terminal means
availability is unknown; open Terminal only on the operator's request.

The existing `nebula.settings.api_request` supports fixed runtime detection and
selection plus image prepare, retry and exact-operation cancellation. Describe the
current schema first. Select only a detected `candidate_id`; Core re-verifies the
fixed executable. Image prepare/retry always bind to the selected project, even
when the model omits or supplies null `project_id`. A foreign target is refused.
Read `setup/status` before cancellation and pass the current own `operation_id`;
Core preserves stale-ID, cancellation and idempotency guards. Accepted preparation
is still pending. Report the actual error, cancelled or ready phase from a later
Core status read, and retry only the requested operation. A shared automation
image can separately be prepared through `automation/runtime/prepare`; its global
image metadata is distinct from the project's execution policy or Host consent.


## Shared Core tools and mixed subagents

Assistant settings chooses the main runtime and child runtime independently.
Provider and harness parents may each select a provider or harness child with
its own advertised model. A provider parent without an explicit child selection
keeps its own provider/model. Core applies the same depth, concurrency, privacy
and model-verification rules in every combination. The capability result's
`subagents.available` and `can_start` describe the parent; the separate
`child_delegation_available` describes whether the child can delegate further.
Discovery accepts category, cursor and limit on both provider and harness paths.

Project lifecycle hooks are selectable for both backends. Explicitly assigned
child hooks retain immutable manifests and executable hashes in every round.
Omitted child hook IDs still mean none, as documented in the tool schema.
Required start hooks block task work; required completion hooks block publication
of a successful answer. Actual hook execution IDs and statuses are durable
attestation, and appear in child reports and the conversation's hook outcomes.
Core command guards remain independent of vendor-native hooks. A narrowed child
or selected command guard uses Core's gateway rather than unrestricted native
tools that could bypass the Core selection.

Managed harnesses share Core working notes, conversation search, durable research
result queries, goal progress/attention/completion, selected skill resources,
project SSH wrappers and web search. Project policy, workspace identity, cloud
transfer consent, active-turn ownership and approvals still apply. Enabled usable
MCP sources can be discovered through the on-demand catalog; loading a schema
grants no permission. Butler's administrative capabilities remain Butler-only.

Narrowed children keep the Core control tools needed for their assigned skills,
catalog sources and delegation. Retrying preserves their original tools, SSH
hosts, skills and MCP catalog selection. Interrupted effectful hooks require
reconciliation before replay; a rejected final answer still records consumed
usage and pauses its Core goal. Notebook MCP remains explicitly selected for
both backends and is excluded from automatic on-demand discovery.

## Concurrent Butler checks and subagents

Background recovery starts one worker per eligible case, so a slow check-in does
not block other cases. Existing provider and harness capacity limits still apply.
Butler's saved **Subagents** switch and child runtime, effort, and active-child
limit also apply to scheduled background checks. A disabled switch remains off;
children may use either a provider or a harness through Nebula's subagent tools.
Vendor-native delegation is not enabled by this setting for background checks.

Open **Butler scheduled activity** in Oversight, expand **Check transcript**, then
choose **Open investigation and subagents** to inspect the case's saved chat,
provider or harness transcript, and child results. Each case has a separate
conversation. Reloading preserves the same case and investigation, and restarting
Core reattaches to existing investigation turns rather than duplicating work.


## Closing the desktop and shutting down Core

On Linux, local Core runs independently of its desktop viewer. Closing, updating,
or restarting the desktop leaves admitted work running. Reopening uses the same
private local connection and saved Core identity. **Settings → Terminal and Core
setup → Shut down Core** is the separate, explicit shutdown action. It confirms
the request, waits for Core's durable shutdown decision and observed process exit,
and reports blockers instead of treating an accepted request as completion.
The current window stays disconnected after shutdown; reopen it to start Core.

The settings assistant should direct an operator who wants to close the window
to the normal desktop close action. If the operator explicitly wants to shut down
Core, open Terminal and Core setup and explain the confirmation and possible
active-work blocker. Never infer a Core shutdown request from closing the viewer.
Remote Core is controlled by its own host; the local shutdown control is hidden
when the desktop is connected to a remote Core.

An existing installation that predates independent Core needs one coordinated
restart before adoption. Follow the project's Butler deployment coordination
procedure, stop every legacy Core using that database, then run
`nebula-core runtime-adopt --confirm-core-stopped --data-dir <existing-directory>`.
Keep any configured administration credential in `NEBULA_V3_API_TOKEN` for this
command. Adoption preserves the database and files in place; it does not merge
different desktop data directories. New roots need no adoption step.

Local detached workers currently require Linux pidfd support. Other desktop
platforms can connect to a remote Core; local continuity on them remains
unavailable. The retained runtime rejects unauthenticated remote mode explicitly.
