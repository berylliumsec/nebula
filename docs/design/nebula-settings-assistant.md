# Nebula assistant contract

The paired terminal Assistant exposes `/board` in slash completion and F4 Commands
after a project is selected, even before a conversation exists. It reads the same
Core Work records as the web project's Work board, including when agent Work
posting is disabled. Tasks are grouped by all six statuses. Up/Down selects,
Enter or Tab reads the description and latest 200 check-ins, `/` searches titles
and descriptions, R refreshes (Ctrl-R in Search), and Escape returns to the
unchanged conversation and unsent draft. Failed reads explain retry in place;
refresh retains selection when the item still exists and matches. Reopening or
reconnecting reads Core again. There is no polling. The board also performs the
web Work page's operator actions with Core readback: N creates a Ready item, U
saves a check-in (summary, next step, blocker, status), T tracks the current
conversation as a linked In Progress item (a new one when its earlier item is
Done, as in the web page), L links the selected item to the
current conversation, and A toggles agent Work tools; Escape cancels a prompt
without a write. Title edits and deletion are not offered (Core has no delete
route). Butler should not claim that viewing or saving a check-in verifies its
results. Plain terminal `/board` prints status-grouped tasks, and `/board new`,
`update`, `track`, `link` and `tools` run the same actions.
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
pre-existing paused goals stay paused. A harness goal interrupted by a dead owner
comes back paused with its claim released, and resuming it may repeat its last
step; requests that owner was running fail without retry. Butler must not suggest raising budgets,
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

Host commands are a host-wide Settings section under Memory budgets (catalog
entry `settings.host-commands`, target `host-command-settings`). Commands from
every project share one Core queue, and each running command holds its memory
limit until it and anything it started exit. The operator sets "Running at once"
(1–64); commands beyond it wait in line, and projects with fewer running commands
go first. The section lists running, detached and queued commands with project,
chat, elapsed or waited time and memory; each row cancels a queued command or
stops a running or detached one. A run_command tool card shows the same Core
state: "Waiting for Host capacity · 3rd in line" with a cancel control, running
time and memory with a stop control, or an exited command whose detached process
still holds capacity. Saving the limit is revision-checked and is reported saved
only after Core's read-back matches; a stale revision is a conflict. A command
cancelled while queued never started, and its result says so. The settings
assistant finds this section through the catalog; typed Butler read, limit and
cancel actions for the queue do not exist yet. See
[Host command queue](host-command-queue.md).

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
| Configure host pressure | Settings > Setup > Host pressure saves CPU, memory-use, runnable-tasks/capacity and Core-cgroup I/O PSI some avg10 thresholds, each 80% by default. Read nebula.work.host_pressure or nebula.global.host_pressure, then authenticated GET/PUT /api/v1/host-pressure with current host_id, expected_revision and thresholds {cpu,memory,load,io} ratios; 1 (100%) turns that check off, since CPU use reaches and runnable tasks/capacity exceeds 1 on a saturated host. Any available reading >= a threshold below 1 queues new starts without provider/background slots; below all thresholds resumes without hysteresis. Unknown is not pressure; running jobs continue. Read saved/effective thresholds, current readings, limiting metric and effective concurrency (defaults 60/58). Host load and host I/O PSI are telemetry only; Core I/O PSI is task-stall time, not disk utilization | Core host-local revisioned policy, sampler and durable queues | authenticated settings, live updates, restart, cancellation and production LAN browser |
| Change setting | Exact target and value are validated; saved state is read back | Browser preference or Core entity | Browser and real Core |
| Configure and activate host CPU workers | Setup saves a host-local CPU budget and enabled state shared by all projects/browsers. Enable background PDF workers starts real Linux processes for PDF generation; Automatic uses roughly half capacity with a minimum of one and maximum of four whole worker CPUs. Butler reads `nebula.work.host_cpu_budget` or `nebula.global.host_cpu_budget`, then uses authenticated GET/PUT `/api/v1/host-cpu-budget` with fresh `host_id`, `expected_revision`, `cpu_limit` and explicit `enabled` only when activation/disable was requested. Omitted enabled preserves state. Saved pending/failed is distinct from running: verify `executor_state=running`, `apply_state=applied` and worker count. Disable/resize drains accepted work; disabled requires zero processes. Reload/restart restores policy. External chat/tool/LLM/GPU execution is outside this pool | Host-local atomic policy, Core-owned worker lifecycle and kernel affinity | authenticated API, real PDF/restart/drain/conflict/failure, component, production LAN/browser and frozen Linux Core |
| Turn on Butler supervisor powers | Off by default. The operator sets `NEBULA_BUTLER_SUPERVISOR_ENABLED=1` and the related `NEBULA_BUTLER_SUPERVISOR_*` and `NEBULA_BUTLER_REVIEW_*` variables in Core's service environment (see `docs/design/butler-supervisor-powers.md`); there is no browser control. Butler then finds `nebula.supervisor.*` operations (shell, signal, runner_control, repo_publish, run_gate, repo_audit, repo_restore, independent_review, hygiene_tick, must_do_resolve, recover_stuck_turn, post_goal_note, probe, tmp_cleanup, log_writer_status, request_operator_action, list_operator_actions, close_operator_action, route_goal_runtime). Clearing the variable is the kill switch and takes effect on the next call. Butler must not claim it can enable, widen or bypass these powers, the host allowlist, the pull-only production mirror, operator-only actions or the independent-review completion gate; general sudo needs the separate `NEBULA_BUTLER_SUPERVISOR_ALLOW_SUDO` flag, which is unrestricted root on a host whose Core user has passwordless sudo | Core service environment, code-enforced guardrails, durable audit log | Butler gateway, guardrail and gate unit tests |
| Route goal runtimes by Work stage | Off by default. The operator sets `NEBULA_GOAL_RUNTIME_ROUTING_ENABLED=1` and the `NEBULA_GOAL_RUNTIME_ROUTING` JSON policy (routes by Work stage, a review-rejection promotion rule, a rate-limit fallback, project and goal scope) in Core's service environment; there is no browser control (see `docs/design/goal-runtime-routing.md`). Routing switches only goals it owns: a goal whose current or pending Assistant settings already match its route is recorded in policy, one that differs is recorded out of policy and left alone until the operator route `POST /api/v1/chat/sessions/{id}/goal/runtime-route` or Butler's `nebula.supervisor.route_goal_runtime` names it. Switches keep the goal's MCP servers, happen only at a turn boundary, and refuse a harness without a verified Check/Test turn. Butler must not claim it can enable or widen routing | Core service environment, Assistant settings writer, goal chat routing record and project events | Goal runtime routing unit tests |
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
| Pair or revoke a device | Interactive Butler lists nonsecret paired-device identities and opens the requesting browser's Paired devices control. The Nebula host browser or host-only `nebula-core chat invite` creates and displays a single-use invitation; the new device redeems it. `nebula-chat pair` gives a terminal its own revocable session, `nebula-chat` or `shell` opens a full-screen terminal Assistant with project/chat navigation, live progress, direct agent conversations, skills, goals, settings, attachments and pending actions. Typing `/` in its composer opens commands filtered by the current conversation and turn; Up/Down and Enter select one, while Tab inserts it. Outside that menu, Up in the composer recalls the selected conversation's latest sent message from loaded Core history, repeat Up steps older, and Down restores an unsent draft; multiline cursor movement remains available and recall makes no new Core request. The terminal header and activity strip animate through local Core reads and confirmed active turns, with elapsed time for local operations; `NEBULA_CHAT_REDUCED_MOTION=1` keeps the same feedback stationary. The transcript, activity strip and composer fill the available terminal pane beside a separated conversation rail; compact shortcuts remain visible. The rail shows Core-backed Running, Waiting for action, Idle or State unknown; F3 shows compact RUN/WAIT/IDLE labels. A queued turn or pending action stays visible without a progress animation; running work can continue after the viewer detaches, and `/status` gives the selected chat's next action. `/snapshot` or F11 opens the selected project's on-demand Core-owned summary of Work progress, assets, mission, findings, cost, and pending approvals; R refreshes the panel without changing the chat. Answer headings show Core's elapsed time and token usage; thinking stays collapsed until `/thinking`, which reads saved thinking on demand. `/chat edit` restores a sent message to the composer and rewinds only when the edit is sent; `/chat bulk` archives, restores, pins, flags or deletes several conversations with Core readback; `/chat finish` completes a provider answer that stopped early. `/search` finds saved messages in the conversation or project (optionally bookmarked only) and opens the match; `/bookmark` toggles a message bookmark; `/side` starts, opens, leaves or discards a side chat without tools. `/queue` numbers Core's follow-ups and adds, edits, moves, removes, pauses, resumes, clears or sends one now; `/now` stops the running turn and sends new text next. `/model` switches a saved chat (including a harness chat) to a provider model through Core's checked runtime switch, verifying an unverified model first; chat choices become the project's defaults for new chats. `/mcp` sets whether selected tool schemas are always loaded and `/mcp exclude` excludes on-demand servers next turn; tool-result sharing can be allowed always for a runtime; a new harness chat can resume a saved or external harness session from `/settings`. `/goal complete` records a summary and operator evidence items, which Core requires; `/goal child` starts and opens a child goal. `/approvals` includes subagents' requests (Allow once or Deny) and offers editing a provider request's exact arguments or always allowing commands on an SSH host before approving. `/agents` opens a subagent's conversation to read or message it, stops one subagent or all of them. F2 lists active projects only; its Archived projects entry restores or permanently deletes an archived project, and Create project takes a name, client, Core-host folder and Host or Docker mode. `/project` renames, relinks, edits execution mode, command policy and network scope with confirmations for Host mode and wide access, or archives the selected project, reading each change back from Core. Its execution policy menu sets a Docker-mode VPN route (direct egress or an available OpenVPN profile; choosing one turns network access on), and `/project followup` edits tool follow-up: suggest next steps, take notes, and an allowed analysis runtime and model, with cloud confirmation for remote runtimes. Enter while a command finishes keeps the entry as the next input (footer Next:, Ctrl-C edits it), and multi-select menus accept multi-word searches because Space toggles only before a search is typed. Opening a chat summarizes what changed since this terminal last read it; `/catchup` reviews those updates and `/catchup read` marks them read; answers watched live in an open chat count as read unless a summary is waiting. `/quote` quotes a saved message into the draft, `/evidence` shows an answer's supporting evidence, and `/turn` shows a response's status, model, timing and error. `/results` opens a tool result's output to read, search or save raw, and `/activity` stops a harness's native subagent task or rewinds files to a checkpoint when the harness advertises those controls. `/model` offers More provider models, adding a reported model to the provider's allowlist before switching to it. `/notes` opens the selected project's saved Notes in a separate full-screen panel. Up/Down selects, Ctrl-N creates, Ctrl-S saves with a Core revision readback, Ctrl-R refreshes, and deletion checks report dependencies; chat work continues while Notes is open. `NEBULA_CHAT_PLAIN=1` uses a scrolling prompt. Legacy `chat` opens the project's saved Butler conversation, and `unpair` revokes that terminal. Install the separate `nebula-client` Python wheel on Linux or macOS without Core dependencies; the new shell requires Core terminal protocol 1. `nebula-core headless` runs Core without a GUI or frontend build. See `docs/CLI.md`. For another browser device, the browser confirms an exact ID and creation time, revokes it, and verifies Core list absence. The current browser uses its direct Unpair control because access ends immediately. The generic Settings API does not create offers or revoke devices | Core paired-device sessions; host and companion devices own one-time secrets and local access | Butler tool, Core API, terminal and production LAN browser |
| Create or delete a credential | Butler opens write-only credential entry in the requesting browser or prepares an exact managed reference for browser confirmation and deletion. Core status proves creation or absence; only the reference enters the saved Butler outcome. The generic Settings API exposes credential status reads, not secret mutations | Core credential store; browser owns the secret and persistence choice | Butler tool, Core API, component and production LAN browser |
| Attach a credential to a model provider | Butler lists provider identities and reads the exact revision, then prepares a managed-reference handoff. The requesting browser checks credential availability, confirms the provider and inference or OpenRouter management slot, patches only that slot at the current revision, and reads the saved provider and credential status back. The raw secret never enters Butler. Integration-specific attachment remains a separate workflow | Core global provider profile and credential store; requesting browser owns confirmation | Butler tool, Core API, component and production LAN browser |
| Configure an OpenAI API provider | Settings → Models → Add provider → OpenAI accepts an API key in the private credential flow and an optional Daybreak Blue choice. Core saves `openai_cyber_access_program=daybreak_blue` in that profile's options; every OpenAI Responses request sends `access_programs: {"cyber":"daybreak_blue"}`. Automatic omits the field. The choice survives refresh and appears on the provider card. OpenAI decides access for the selected project and model; an unauthorized explicit request can return 403. Butler discovers the Model providers destination for OpenAI API key or Daybreak Blue requests and can inspect nonsecret profile options through the provider Settings API | Core provider profile and credential store; OpenAI Responses adapter | Component, provider wire, assistant discovery and production LAN real Core |
| Attach a credential to an Assistant harness | Butler pages harness identities and exact revisions, then prepares an available managed-reference handoff. The requesting browser confirms the exact harness and patches only the reference and coherent authentication mode at the current revision. Grok cached login and Claude native shell are excluded. The browser reads back Core's saved harness and credential availability; running sessions keep their own state and the operator checks harness health before new work | Core global harness profile and credential store; requesting browser owns confirmation | Butler tool, Core API, component and production LAN browser |
| Attach a credential to an MCP server | Butler pages nonsecret MCP server identities, transports, revisions and occupied key names. The requesting browser confirms an available managed reference for one exact HTTP bearer, HTTP header or stdio environment slot, patches the current revision, preserves other active key references, and reads Core's saved slot, mode and credential status back. Built-in servers cannot be changed; the operator checks MCP health before use | Core MCP profile and credential store; requesting browser owns confirmation | Butler tool, Core API, component and production LAN browser |
| Manage nonsecret integration profiles | Butler discovers the Settings route and schema, lists provider, Assistant harness or MCP profiles, and reads the exact current identity before a change. A create reads back Core's new profile ID; patches use its current revision; deletion requires the observed revision and Core's dependency guards. Verified writes refresh the requesting browser's Settings catalogs. Credentials use their private browser flow, and a saved profile does not prove runtime health | Core profile validator, revision and exact readback; browser owns catalog presentation | Parameterized Core gateway, receipt parser and production LAN browser |
| Manage project Notes | Butler pages editable notes and same-project asset/evidence link targets, reads bounded body chunks, and creates or edits exact note fields in Core from the web app, Workbench chat or terminal. This includes operator-accepted execution notes (`ai_execution_note`), with original AI provenance and operator acceptance retained. Core derives a created note's ID from Butler's request key, so a retry replays the saved note instead of adding another; edits use the current revision. Core reads saved fields and links back before Butler says the note is saved, and the web app refreshes Project Notes. Deletion needs the operator's confirmation of the exact note in the web app Butler panel or Project Notes; in a terminal or Workbench chat Butler reports that the note was not deleted. Deletion remains blocked while reports use it. In an archived project Butler refuses note changes until the project is restored. Saved notes appear by title in Project Notes and can be selected as report sources. The paired full-screen `nebula-chat` `/notes` view browses and edits those same project records on demand, retains an unsaved draft on failure or revision conflict, and confirms saves from Core; recording a claim does not verify it | Core notes, relation projection and dependency/revision guards; browser owns deletion confirmation and temporary drafts | Core capability/API, component and production LAN browser |
| Review manual execution notes | From execution history, generate an explanatory draft with the selected provider or harness and per-request cloud consent. A manual draft remains ready until explicit operator acceptance; choosing a harness does not enable automatic notes. Accepted notes appear in Project Notes and Butler's note catalog with AI-generated and operator-accepted provenance. Automatic post-tool notes require the separate enabled follow-up configuration and an explicit automatic request | Core draft/observation transaction and revision; browser owns manual review and consent | API client/component, Core service and production browser |
| Export a diagnostics support ZIP | Butler prepares an export intent for the requesting browser. The browser describes the sensitive logs, asks for confirmation, verifies Core returned ZIP bytes, and starts the local download. Butler receives no bundle bytes and does not claim the operating system saved the file | Core diagnostics exporter and requesting-browser download | Butler tool, component and production LAN browser |
| Configure Butler Voice | The catalog opens Butler's compact Voice timeout control; the assistant can discover the authenticated Core-wide Voice settings route and report a change only after Core read-back | Core Voice settings | Tool unit, component and real Core |
| Manage Nebula-wide Butler instructions | The catalog opens the editable BUTLER.md Markdown code editor above the Settings popup after Core loads the file. The full Settings page retains its file row and preview; upload opens a draft. The API checks the loaded digest before saving and the UI reads Core back before confirming | Core data directory and durable Butler global clock | Tool unit, component and real Core browser |
| Set Butler model and effort | The catalog opens Butler settings. Full Assistant settings opens Butler's saved conversation with its Assistant settings panel visible. Core saves the selected effort on Butler's chat; the next direct turn and each new scheduled harness check use that saved choice. An unset effort uses the model's advertised default | Core Assistant settings and harness session | Core unit, component and real Core |
| Queue messages while Butler works | While a response runs, the composer queues the next message in Core's follow-up queue for Butler's chat. The Butler queue list edits, reorders, removes, pauses, resumes, clears and retries items; the queue survives reload and tab close. Core sends queued items in order; the device that queued one follows its response and applies its browser actions, and device-only commands run immediately | Core chat follow-up queue, Butler turn ledger and the queuing device's local memory of its queued items | Component, browser and real Core |
| Let Butler delegate to subagents | The catalog opens Butler settings. **Delegate longer work to subagents** is on by default for a provider runtime until the operator saves a choice; **Running at once** is optional (empty means no limit). On a harness runtime the operator turns on Subagents and picks a provider model. Butler answers quick requests itself, starts a subagent for each multi-step or independent item, replies without waiting, and posts each report into its conversation. The panel shows a compact strip of working and finished subagents, links to Butler's Subagents drawer, and adds reports as Core posts them without reopening. Children run in Butler mode on a provider model with Butler's Core tools and approvals, appear under Butler in the sidebar, and report browser actions instead of requesting them | Core Assistant settings, `ChatSubagent` records and Butler's transcript | Core unit, component, browser and real Core |
| Explain a Nebula problem | A question receives an answer grounded in the selected project's effective mode and linked workspace when those facts matter; it does not infer operator intent from an automatically stored Docker default and distinguishes future policy from existing frozen command sessions | Core project and automation policy; model owns explanation | Tool unit, provider and harness boundary, real Core model turn |
| Help resolve a problem | A suggested Settings destination opens the same visible lens as search; a mode change still requires an explicit request and Core's Host access acknowledgment field | Shared setting catalog, shell lens, Core policy | Component, browser, real Core and LAN |
| Check project work | The selected project's assistant chats and missions are listed with bounded current status and exact targets. In Workbench conversations, chats Butler creates appear below Butler; older recovery chats use their linked Core goal as ownership evidence. Search reveals matching children, and a selected child remains open after reload | Core sessions, goal metadata, turns, queues and runs; URL for selection | Tool unit, component, browser and real Core |
| Define Work stages | Settings assistant **Work stages** opens the selected project's Work board stage editor. The operator names, orders, flags and removes stages; Save sends the full list with the project revision and reloads projects from Core; a conflict asks to reopen. A subproject without its own stages uses its nearest parent's; its board says whose, and saving there gives it its own list. A parent's Work page counts its subprojects per stage under Subprojects by stage, and selecting a stage lists the subprojects that did or did not reach it. Group: Stage shows one column per stage (plus No stage and Other stage when used) and each item's Stage field saves at the item revision. All projects shows each project's stage cells, gaps and Missing a stage / Blocked filters. Butler reads `nebula.work.work_stages` and changes them with `nebula.work.set_work_stages`; agent Work tools and imports accept a stage key | Core project `work_stages` and item `stage`; URL owns grouping, focused stage and the open editor | Core unit, Butler capability, component and real Core |
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
| Start a scoped browser mission | Interactive Butler pages active security browser sessions with `nebula.work.browser_sessions`, then starts a selected-project mission with an exact session, target URLs, the passive risk class and lease limits. Network actions and credential grants remain operator-controlled. Core checks the same scope and session policy as the Browser form. Butler reads the saved mission ID and status; completion and findings require later mission inspection | Core browser session, scope policy, automation lease and mission; URL selects Browser or Missions | Butler tool, Core API and production LAN browser |
| Interrupt work | An exact selected chat turn or mission is stopped through its existing Core lifecycle and read back | Core turn or run | Tool unit and real Core |
| Ask for a task plan | The chat's Assistant settings asks any provider or harness agent to share a plan on future turns, or Butler records one exact one-task request with the target revision and reason. Normal tools remain available, and the activity view shows whether a plan was shared | Core assistant config, directive, and task plan | Tool unit, component, browser and real Core |
| Hand off a previously planning-blocked goal | Butler reads a paused harness goal and uses a revision-checked action after Core verifies that its latest failed turn was never admitted and ran zero tools. Core links one provider draft to the paused source; the new planning preference is advisory | Core goal, turn, queue, approval, provider, and handoff records | Core unit, component, real Core and browser |
| Add a supervisor note to a goal | Chat → Goal → Add note saves up to 4,000 characters of guidance on a nonterminal goal without rewriting its objective; a goal keeps at most 50 notes and refuses another until one is removed. Notes are listed under Supervisor notes with a remove control. When the goal is running, a provider turn receives the note, or any objective, criteria or plan edit, as a Core-added `goal.read` result before its next tool step, exactly once; a running harness turn is steered with it; a harness that cannot be steered (Grok) and an idle goal read it at the next turn. The panel says which applies. Butler adds a note with `nebula.work.add_goal_note` | Core goal record (notes, guidance revision, bounded change log) and the turn's ledger step; the note form owns the unsent draft | Core unit, component, production LAN Core |
| Remind a quiet goal to check in | A running goal whose agent posted no Work check-in for 30 minutes (since its last check-in, Butler progress report, reminder or start), or made 40 tool calls in its turn since its last check-in or reminder, is reminded once per window inside the running turn to post `work_check_in` with progress and next step. A provider turn receives it as a Core-added `goal.read` result labelled Check-in reminder delivered; a running harness turn is steered with it (Grok is not reminded). Only turns that have the Work tools are reminded; tools are never refused and Butler is never asked to recover the goal. The thresholds come from Core's environment (`NEBULA_GOAL_CHECKIN_REMINDER_MINUTES`, `NEBULA_GOAL_CHECKIN_REMINDER_TOOL_CALLS`; 0 turns a rule off) and have no Settings control | Core goal metadata (last reminder) and the turn's ledger step | Core unit and production LAN Core |
| Edit a goal's resume instruction | Chat → Goal → Edit goal shows the saved text; the current Core wording is the default. Saving it updates the goal by revision, and the next explicit Resume sends that text in the same conversation. Automatic continuation turns retain their separate Core instruction | Core goal record and resumed turn; goal editor owns the draft | Core unit, component, real Core and browser |
| Recover a harness goal from model capacity | When a Core-owned harness goal turn stops because the selected model is overloaded, Core waits briefly and starts one new continuation in the same chat. It uses the saved goal and completed read-only receipts, never replays the failed prompt, and counts the new turn against goal budgets. A second overload, a write, an uncertain effect, or a policy refusal leaves the goal paused with its saved reason for review. The transcript and goal state survive refresh. This automatic behavior has no Settings control or alternate-provider action | Core turn ledger, goal revision and chat transcript | Focused Core tests and production LAN browser |
| Resolve a decision whose delivery is unknown | When a project worker stops while a harness adapter handoff of an approval decision is in flight, recovery records that delivery as unknown and never sends the decision again. Until the operator settles it, the project's worker cannot retire when idle, hand off to a new release or stop with Core. Butler lists it in `nebula.work.attention` as `unconfirmed_delivery`; `nebula.work.approval` shows its delivery status. Only on the operator's explicit instruction, after they checked the harness, `nebula.work.resolve_delivery` records `delivered` (the harness received it) or `discarded` (treat it as not delivered) with the approval revision. Core saves the outcome with an audit event (`POST /api/v1/approvals/{id}/adapter-delivery`), sends nothing, and returns the saved approval for a repeated identical request; a stale revision or an already settled delivery is refused. A discarded decision needs a new response to redo the work. Scheduled Butler cannot resolve deliveries. There is no Settings or chat control yet | Core approval continuation and its run audit event | Core unit, Butler tool and real Core |
| Complete a provider goal | A tool-enabled provider agent calls the offered `goal_complete` tool (`goal.complete` in Core) with a summary and concrete evidence after meeting the goal criteria. Core saves the completed goal with the final answer, labels the evidence agent-reported, and starts no further continuation; the goal panel shows the saved state after refresh | Core goal, turn ledger, final message and goal panel | Core unit and real Core browser |
| Review a goal's completion | Chat → Goal → Add goal or Edit goal → Hold completion for review. When the agent of such a goal calls `goal.complete`, Core does not complete it: the goal stops blocked as Awaiting closure review with the agent-reported summary and evidence, and no further turn runs. The goal panel shows the proposal with Approve completion, which completes the goal with that evidence, and Send back, which resumes the same chat with the reviewer's feedback ahead of the resume instruction. Butler reviews the `goal.completion_review_requested` and `goal.completed` events and can approve or send back through `nebula.work.goal_action`; the choice is off by default and is saved per goal | Core goal record, metadata proposal, project event stream and goal panel | Core unit, component, real Core and production LAN browser |
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

Assistant harnesses offer Codex, Grok and Claude. **Add Claude** creates a
managed Claude Agent SDK process; the CLI path is optional because the SDK
bundles one. It signs in with the host's existing `claude` login (`/login`) or
an API key credential. An optional account folder runs that profile's Claude
CLI with `CLAUDE_CONFIG_DIR` set to the folder; sign it in on the host with
`CLAUDE_CONFIG_DIR=<folder> claude login`. Claude profiles use
managed approvals only and do not offer browser, computer-use or image
generation capabilities.

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
`nebula.work.stop_mission`. With `nebula.work.browser_sessions` and
`nebula.work.start_mission` Butler can start a scoped browser automation
mission for the project. These typed tools are bound to the selected project
by Core. They list targets and enabled runtimes before an action; they do not
accept arbitrary API paths, workspace commands or cross-project target IDs.
For the selected project's Work board, Butler discovers
`nebula.work.work_items`, reads `nebula.work.work_item`, and uses
`nebula.work.create_work_item`, `nebula.work.update_work_item`, or
`nebula.work.check_in_work_item` after an operator request. Creation and
check-ins use stable request IDs; edits and check-ins use the current item
revision. Butler reports the saved Core result and board link. A Work check-in
does not run or complete an assigned assistant chat.

### Waiting for memory

A never-saved Core reserve is sized from host memory (services floor plus a
quarter of capacity for 3 to 16 resident workers); a saved one is never changed.
A project that shows **Waiting for memory** gets the memory of an idle resident
project's worker automatically; when every resident worker is busy its
requests fail after the bounded wait, naming the busy projects. To run more
projects at once, Butler reads Memory budgets and proposes a higher Core reserve
through `set_memory_budget` with the observed revision; it must not archive
chats, stop work or retire workers to make room, and it reports the saved
reserve only after reading it back from Core.

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
For Work stages, Butler reads `nebula.work.work_stages` (ordered key, label
and flag-when-empty, plus the project revision; `effective_stages` and
`inherited_from` report the nearest parent's stages that a subproject without
its own uses) and replaces the project's own list with
`nebula.work.set_work_stages` at that revision only after an explicit request;
Core reads the saved list back. Saved stage keys never change, so items keep
their stage; an item whose key is no longer listed shows under Other stage.
Butler moves an item with the `stage` argument of `nebula.work.check_in_work_item`
or `nebula.work.update_work_item` (null clears it). The settings assistant's
**Work stages** entry and destination open the selected project's Work board with
its stage editor (`/projects/{project_id}/work?stages=edit`); All projects
shows each project's stages, flags a stage marked flag-when-empty that nothing
has reached, and filters to Missing a stage or Blocked.
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
The same tools save a goal's `completion_requires_review` choice (Hold
completion for review in the goal editor). With it on, an agent's own
`goal.complete` leaves the goal blocked as Awaiting closure review with its
proposal in `metadata.completion_proposal`; `nebula.work.goal_action` with
`complete` approves that proposal, and `resume` with a reason sends it back.
The Chat destination also accepts "goal closure review".

Butler adds a supervisor note with `nebula.work.add_goal_note` (session and
text only; notes need no revision because they only append). Its result reports
`delivery`: `next_step` when a running provider turn will read it before its
next tool call, `running_turn` when a running harness turn was steered, and
`next_turn` otherwise. The Chat destination accepts "add supervisor note" and
"goal supervisor notes".

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
Each Butler check also runs in its own child chat ("Butler check · …"). When a
check finishes, Core posts its final summary once to one main Butler chat, as
a Butler message that names the check (time, Nebula-wide or project scope, the
project it ran in) and links to the child chat. The message starts no Butler
turn and is not operator input. A Nebula-wide check always posts; a project or
event-triggered check posts when it has a summary or a decision. A check that
never started Butler posts nothing, and neither does a check whose result is
"No change": Core saw no change, guidance, recovery goal, or main-agent
dialogue, the check resolved, and the first line of Butler's reply begins with
"No change". By default the summary goes to the Butler chat of the project that
hosts the Nebula-wide check, then the most recently used Butler chat, then the
check's own project Butler chat. The operator can choose another project Butler
chat in Settings → Automation → Butler check interval ("Post check summaries
to"), and Butler can read and change the same choice with
`summary_target: {project_id, session_id}` (or `null` for the default) in the
`butler-check-interval` PUT body with the current revision. A chosen chat that
is deleted or archived falls back to the next default, and Settings shows the
last fallback and its reason.
Core persists the interval with a revision guard, reads it for each due check,
and reports the next due time from the last saved check. Reducing the interval
can make the next check due immediately. It can run with no running goals. An Act
project where Butler has a model hosts the run: the project's own Butler chat,
or a Butler home session focused on the project, which shares its model with
that project so the project gets its own Butler chat for background checks.
Another Act project without a Butler chat runs its recovery checks on the
model of the newest Butler chat the host or any project reserved, unless that
is a cloud model and the project keeps its data on local models.
Without a host no scheduled check runs, and Butler Oversight shows "Needs Act"
with the exact fix for the selected project: set its Butler authority to Act,
turn its work on, or open Butler in it once. A project that is not the host
shows "Ready" once the host has run a recent check. Butler reads the live
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
request. Native harnesses use a managed opener when available and otherwise use
the vendor adapter's bounded lifecycle without a Nebula memory-runner limit. An
unverified installed vendor channel remains a concrete capability limitation.
Do not claim an installed vendor passed merely from an
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
Host-mode commands need the protected local memory runner. When the Memory
budgets `local` environment reports unsupported, tell the operator to run
`sudo systemctl enable --now nebula-memory-runner@<user>` on the Core host;
Butler cannot install or start it, and must not suggest the unmanaged override.

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

### Butler from a terminal client

`nebula-chat` and the terminal Assistant send `client_surface: "terminal"` with
each turn; web clients omit it and stay `browser`. Butler's instructions say it
is in the terminal, so it does not offer actions that only a web browser can
finish. Core refuses any Butler action whose result would be a
requesting-browser handoff (`pending_browser_apply`). It decides from that
result rather than from a list of action names, so a new handoff action is
covered and an action that saves in Core keeps working. The refusal tells the
model the action needs the Nebula web app, and nothing changes. The same check
covers the legacy direct `nebula.settings.*` and `nebula.navigate.open` names
that a harness gateway still dispatches. A Core older than `client_surface`
rejects the field as unknown with a validation 422 before its chat handler runs,
so the terminal resends that turn once without it and remembers that Core; it
then labels a handoff receipt as needing the web app instead of reporting the
tool as finished.

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
are refused before mission creation. Network actions stay in the visible
operator-controlled Browser; a passive mission receipt does not establish an
independently verified finding.


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
limit also apply to scheduled background checks once the operator has saved a
choice. Butler's default delegation for a provider runtime applies only to direct
conversations; an unattended check does not delegate until the operator turns the
switch on and saves. A disabled switch remains off. Children run in Butler mode
through Nebula's subagent tools, on a provider model or a harness profile. A
harness child is a control-assistant harness session with Butler's Core tools
and no native shell. A harness child that is disabled or no longer offers its
model makes a background check run without subagents (diagnostic
`butler.recovery.harness_subagent_unavailable`). Vendor-native delegation is not
enabled by this setting for background checks.

## Butler subagents in direct conversations

Butler handles one response at a time. To stay responsive, Butler on a provider
runtime delegates multi-step or independent items to subagents, tells the
operator what it started, and ends its reply, so the next request can be sent
while children work. Turn this off in Butler settings with **Delegate longer
work to subagents**; set **Running at once** to cap concurrent children. Each
child runs in Butler mode on a provider model or a harness profile, with
Butler's Core tools and the project's approvals, and posts its report into Butler's conversation when it
finishes. The Butler panel lists working and finished children above the
composer and adds each report as Core posts it. Finished rows clear on the next
request. **Subagents** opens Butler's saved conversation with its Subagents
drawer, where a child can be read, messaged or stopped. Children cannot request
browser actions; they report what needs the operator instead.

## Queueing messages for Butler

While Butler answers, its composer stays open and its button reads **Queue**;
**Stop** moves to the Butler activity row. Each queued message goes into Core's
follow-up queue for Butler's conversation, the same queue the terminal's
`/queue` edits, so it is kept when the tab closes and appears on other devices.
Core sends queued messages in order when Butler is free. The **Butler queue**
list above the composer edits, reorders or removes a queued message, and
**Pause**, **Resume** and **Clear** act on the whole queue. A message Core could
not send pauses the queue and shows **Needs review** with **Retry**; nothing is
resent silently. The device that queued a message follows its response as if it
had just sent it, so settings, pickers and other browser actions still apply
there; other devices only see the saved reply. Device-only commands such as
changing the theme, opening a setting or switching project mode run at once
instead of queueing.

Open **Butler scheduled activity** in Oversight, expand **Check transcript**, then
choose **Open investigation and subagents** to inspect the case's saved chat,
provider or harness transcript, and child results. Each case has a separate
conversation. Reloading preserves the same case and investigation, and restarting
Core reattaches to existing investigation turns rather than duplicating work.


## Projects paused for a Core update

A Core update can hold a project (`ProjectHold`; see
[Project holds](project-holds.md)). While a project is held, the workspace
shows "Pausing work in this project" while running responses use their grace
period, then "Work in this project is paused". No chat work starts in a held
project: Send is disabled with the draft kept, goals the hold paused read
"Goal paused by update · resumes automatically" and have no Resume control,
and queued messages, subagent reports and schedules wait. Nothing is lost or
failed. Releasing the hold resumes exactly the goals it paused; goals that were
paused before stay paused.

When an operator asks why work stopped, or why Send or Resume is unavailable,
explain the hold and that it ends on its own when the update finishes. The
banner's **Release** control (an icon button at phone width) confirms first and
should be suggested only when the update that placed the hold is no longer
running. This project-wide hold has no Settings destination; the deployer and
the hold API place it. Butler can place or release one only when the operator
asks (`nebula.work.hold_project`, `nebula.work.release_project`). To set
current research aside while the project keeps working, hold chats instead
(see "Holding chats when priorities shift").

## Butler mode

**Butler mode** (route `/butler`) is the one place for Butler: talking to it,
seeing every running chat across projects and what needs you, and Butler's
oversight, memory and settings. The
**Butler** launcher in the top bar opens Butler mode; the small panel's **Open
Butler mode** button and asking Butler to "open Butler mode" do too. **Esc** or
**Exit Butler mode** returns to the page the operator came from. The iPhone app's
Butler tab is the same Butler mode page; it has no **Exit** or **Open full
Nebula**, and a chat's **Open full chat** opens it as a popup there. There is no separate floating Butler panel on the web or
desktop.

Butler's threads stay on the left. The top bar's sections switch the right
side: **Board** (the status board), **Code & files**, **Oversight** (incidents,
authority, watched goals, decisions and resources; `?section=oversight`),
**Memory** and **Settings** (Butler's model, effort, cyber access, subagents,
Voice timeout, appearance, and "find a setting or place"; `?section=settings`).
Oversight, Memory and Settings follow the project chosen in their **Project**
picker. Links to Butler settings or oversight from elsewhere open these
sections; while the small panel is open they open inside it instead. On a phone
the sections are **Board**, **Threads**, **Oversight** and **More** (Code &
files, Memory, Settings, All projects, Open full Nebula). **Start Butler Voice**
is in the top bar, next to **All projects**, which opens the Projects page
(`/projects`): every project's Work status and, where projects define them,
their stage cells and Missing a stage filter.

The rail's **Projects** picker holds each project's one ongoing Butler
conversation (the same conversation the small panel shows), with its request
cards for uploads, credentials, Library adds and linked folders. The picker is
searchable: typing filters projects by every word of their name, projects
whose chats need the operator or are working stay under **Active** with their
counts above **Quiet**, ↑/↓ move, Enter opens the project's thread and Esc
closes the list. It renders the first 80 matches and says to keep typing for
more. Parallel
**Threads** follow below. Text added to Butler from the selection menu while in
Butler mode joins the composer's **selected context** and is sent with the next
message; remove an item with its ×.

The **status board** reads one Core-owned list of goal chats across all
projects that are not archived; chats without a goal are not listed. Each
project has three sections: **Active** (running goals, including chats
working or needing the operator, which come first), **Idle** (paused, blocked
or proposed by Butler) and **Completed** (completed or cancelled, collapsed at
first). Its header shows "N goals" and each section's count. Butler's own
conversations, side chats, subagent chats and archived chats are never listed.
**Group** switches between **Project** and **Status** (the three sections
across projects). The board's search sits beside
**Group**: it filters loaded cards as the operator types, says how many chats in
how many projects match, then Core's search (chat titles, models and project
names) fills in chats the board had not loaded. Esc or × clears it. The grouping
and open sections are remembered on this device only.

Each card shows the chat's status, project, model, how long its current
response has run, one line of what it is doing or waiting for, and "Started by
Butler" or "Proposed by Butler" when Butler created it. Section dots: Active
solid blue, Idle a hollow amber ring, Completed green (grey when cancelled). Clicking a card opens
its brief (below); ↑ and ↓ move between chats while a brief is open and Esc
returns to the board. In the brief, **Approve** and **Deny** answer a pending
approval with the exact request shown; **Stop** stops a working chat's response
after confirmation; **Pause goal** and **Resume goal** change its goal; **Ask
Butler about this** quotes that chat into the open idle thread of its project,
or starts a new thread there (if that thread is still answering, it switches
to **Reply in this thread** when Butler finishes, unless you chose); and
**Open full chat** shows the transcript in a pop-up over Butler mode (scroll
it, then click outside or press Esc to return; **Open in workbench** leaves for
the full chat). The pop-up reads the newest messages and older ones on request,
and re-reads only while the chat runs. A decision or stop is reported only
after the next board read from Core shows the change.

Click a board card in Butler mode to open its **brief**, which Core serves as
`GET /api/v1/butler/sessions/{session_id}/brief` (Butler's own conversations,
side chats and archived chats or projects are not found). The brief shows the
chat's state, model, turn count, billed cost when the route reported it,
subagent count, its goal with the plan step the agent is on, what needs the
operator (for an approval, the exact command), the chat's turns as one line
each and its 50 most recently changed files. Each turn's line is derived
without a model: the first sentence of its answer, else a phrase from its
tool calls (for example "Ran 3 commands · edited fuzz.c"), else its failure.
Each turn also shows command, edit, approval and tool counts and the files it
changed. Turns come 30 at a time (at most 100), newest last; `before` and
`older_before` page back. Files come from Core's workspace observations and
from the paths of file-editing tool calls, so a runtime that reports neither
shows none. The active plan step is reported by the agent: `report_progress`
takes an optional 0-based `plan_index`, and the goal's
`PATCH /api/v1/chat/sessions/{session_id}/goal` accepts `active_plan_index`
(null clears it). It is an agent claim, separate from `current_step`, which
counts turns. A turn's plan step is the last one reported in it, else the one
in effect when it started.

**Threads** run Butler conversations in parallel. Each message starts a new
thread by default, in the project chosen beside the composer, and Butler
answers each one at once, so the operator never waits for one answer to ask
the next. **Reply in this thread** continues the open thread once Butler has
answered it. Threads use the project's saved Butler settings (runtime, model,
tools and subagents); a project with no Butler conversation yet asks the
operator to open the Butler panel once to choose them. Threads are separate
Butler conversations, so the Butler panel keeps its own conversation. Butler in
a thread acts only inside that thread's project. A step that needs this
browser (for example a picker or opening a setting) shows a note to finish it
in the Butler panel. The open thread and the phone tab are in the URL
(`?thread=` and `?tab=`), so reload and back return to them. On a phone,
**Board** and **Threads** tabs share one composer.

To archive or delete a Butler thread, open it and use **Archive** or
**Delete** in its header. Archive hides the thread from Butler mode at once and
Core keeps it in the project's archived chats, where it can be restored. Delete
first asks "Delete this thread and its messages? This can't be undone." and
then removes it from Core. Both wait until Butler has finished answering.

While Butler works on a thread, the thread shows the same **Activity** card as
a normal chat: its tool steps as they run, or **Queued** with Core's reason and
queue position when the turn waits for capacity (for example a turn Core
resumed after a restart). The card comes back from Core after a reload or on
another device. A finished answer keeps a collapsed card of its steps. While
Butler answers the open thread, the send button becomes **Stop response**,
which stops that answer like Stop in a normal chat; **New thread** still sends.

### Butler-proposed goal chats

Asked in plain words to spin up work ("triage the crash logs with Claude until
there is a deduplicated report"), Butler calls `nebula.work.propose_goal_chat`
with a title, a listed runtime, model and effort, objective, completion
criteria, MCP tools and limits (time, tokens, steps, subagents). Core saves a
durable draft goal conversation marked `proposed_by: butler`, visible after
reload and on every device. The receipt has `state: proposed`,
`operator_confirmation_required: true` and a `goal_chat_proposal` client
action: nothing runs until the operator chooses **Start**. **Edit** changes the
draft goal; **Cancel** deletes the empty draft. The status board lists each
proposal in Idle, waiting with attention kind `proposal`.

Under Delegate or Act authority Core begins the goal chat at once and returns
`state: started` with a `goal_chat_started` action, which Butler reports. If
Core refuses (for example a hold), the draft stays and the receipt says why.
Board cards carry `started_by: butler` for chats Butler began. Watch and Steer
always leave the draft for the operator.

### Resuming idle goals from the Butler board

**Resume all idle** (beside the search) resumes idle goals in bulk. It reads
every paused or blocked goal Core lists across projects, matching the search
when one is set, not only the cards on screen. It then asks to confirm with the
count ("Resume 47 idle goals?"). To resume a chosen set instead, tick the
checkbox on each paused or blocked Idle card, or use **Select all** in a
project's Idle section, which ticks every resumable idle goal Core lists in that
project. A bar then shows "N selected" with **Clear** and **Resume N**; Esc also
clears the selection, which is kept on this device only until reload. Proposed
goals (drafts Butler suggested) have no checkbox and are never started by
these controls; use **Start** on their card. Each goal resumes through Core's
own goal action, so a project hold, a spent budget, unavailable harness usage
or an interrupted response awaiting recovery still refuses it. A status line
counts progress ("Resuming 12 of 47…") and then reports the result ("Resumed
45 of 47. 2 stayed idle; their cards say why."). Each refused card shows
"Not resumed:" with Core's reason and stays ticked so it can be retried.

### Holding chats when priorities shift

When the operator's priorities change, they hold the chats doing the current
research and start new goals in the same project. A chat hold pauses that
chat's goal, its subagents, queued follow-ups and schedules; running steps get
a short grace period and then stop. Other chats, and new chats and goals, keep
running. Nothing is lost: releasing a hold resumes exactly the goals it paused,
each from its saved progress.

On the Butler board, tick Active or Idle cards and press **Hold N** in the
selection bar, or press **Hold all active** beside the search to hold every
chat with work running now. Hold acts at once with no confirmation; a bar
then says "Held N chats" with **Undo**. Held chats move to a **Held** section
under Active, showing the hold's reason and age, with **Release all** for the
section and a ▷ **Release** button on each card. In a chat, the conversation
menu has **Hold chat** and **Release chat**; the list shows a pause mark and a
muted title on a held chat. Opening a held chat shows "This chat is on hold"
with **Release**, and Send becomes **Release and send**, which releases only
that chat (and its subagents) and then sends the draft; other chats in the
same batch stay held.

Butler holds and releases chats when asked, without a confirmation step:
`nebula.work.hold_chats` (chosen chats, or `all_active`),
`nebula.work.release_chats` (by batch or chat) and `nebula.work.holds` to read
them. Background Butler checks never hold or release work.

### High-priority chats and projects

Mark a chat **High priority** in its conversation menu (or for several chats
from the list's bulk actions), or mark a whole project with the chevron
button on its row in the project switcher. High-priority work is taken first
wherever work waits for shared capacity: provider and tool slots, the host
pressure line, the Host command queue and project workers. Subagents and goal
children inherit their chat's priority; forks start at normal priority.
Nothing already running is interrupted and no capacity is set aside, so
normal work runs at full speed when nothing high-priority is waiting. Blue
chevrons mark high-priority chats in the list and on Butler board cards, which
list them first; a card's expanded actions toggle **High priority**. Butler
sets priority when asked (`nebula.work.set_priority`) and reviews
high-priority chats first; the operator does not need to tell Butler which
chats matter.

### Filtering the Butler board by stage

**Stage** (beside **Group** on the Butler board) narrows the board to goal
chats in chosen project Work stages, for example only the goals in Recon. The
list names each stage by its label across all projects (a project that spells
it "recon" counts with "Recon"), in the projects' stage order, followed by **No
stage** for goal chats with no open assigned Work item that has a stage. Each
choice shows how many goal chats it holds, counting Active, Idle and Completed
and matching the search. Tick one or more; the control then reads "Stage:
Recon" or "Stage: Recon + 1", and a line under the toolbar says "Showing stage
… · N goal chats. Hold and Resume act only on these." Subprojects use their
parent's stages, so their chats filter under the parent's labels. **Clear stage
filter** (the × beside the control) or **Show all stages** removes the filter.
This device remembers the filter across reloads, like the grouping; a
remembered stage that no goal chat has any more stays listed with 0 so it can
be removed.

The filter is applied by Core (`GET /api/v1/butler/board` with `stage=<label>`,
repeatable, and `no_stage=true`), so counts, Completed pages and Select all
follow it. The top bar's **need you** and **working** counts stay unfiltered,
so a filter never hides a chat waiting on the operator. While a filter is on,
the bulk buttons say what they cover: **Hold active in Recon** holds only the
filtered running chats, by name, and **Resume idle in Recon** resumes only the
filtered paused or blocked goals; goals in other stages are left alone.

## Conversation stage labels

A conversation's **stage** is its project's Work stage: the stage of the Work
item assigned to that chat, named exactly as the project defines it on the
Projects page (for example Recon, Simulation, Validation). A subproject without
its own stages uses its nearest parent's, as its Work board does. It shows in
the Workbench conversation list, in a parent project's Subprojects list, beside
the open chat's title, and on goal cards and briefs in Butler mode. Its dot uses the Projects page stage-tile colours for that
item: green not started, blue in progress or in review, red blocked; the
tooltip names the item. When several open items are assigned to one chat, the
most recently updated one counts. A chat with no open assigned item, an item
without a stage, or a stage the project no longer lists shows no stage. Core
reads it on every chat, list and card read (`metadata.work_stage` on chat
sessions, `work_stage` on card items); nothing extra is stored. To give a chat
a stage, assign it a Work item and set the item's stage on the Work page.

Butler's own status is a quieter **Butler · …** line, never the stage. A
**Butler check** (the chat Butler opens to investigate a blocked or stalled
goal, a failed subagent or a scheduled check) follows its recovery case:
**Investigating** (Butler is looking, or Core is checking its repair), **Needs
you** (Butler could not repair it and needs a decision), **Uncertain** (Butler
acted but Core could not confirm the repair), **Resolved** (Core verified
recovery) or **Closed** (superseded). Butler checks nest under the project's
Butler chat without the "Butler check ·" prefix, and a check that needs the
operator puts that chat under **Needs you**. The project's Butler chat and its
parallel conversations show **Butler · Active**, **Needs you** or **Idle**. A
goal card whose chat has an open Butler check, or one resolved in the last 24
hours, shows **Butler · <status>** after its stage, and its brief offers **Open
Butler check** (`butler_case` on card items, `metadata.butler_case_status` on
chat sessions).

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
restart before adoption. Hold the projects for the stop as AGENTS.md describes
(the Butler procedure for Cores older than project holds), stop every legacy
Core using that database, then run
`nebula-core runtime-adopt --confirm-core-stopped --data-dir <existing-directory>`.
Keep any configured administration credential in `NEBULA_V3_API_TOKEN` for this
command. Adoption preserves the database and files in place; it does not merge
different desktop data directories. New roots need no adoption step.

Local detached workers currently require Linux. Core pins a worker with a
pidfd where the Python build and kernel offer one; otherwise it signals a
worker only after re-reading its recorded kernel birth. Other desktop
platforms can connect to a remote Core; local continuity on them remains
unavailable. The retained runtime rejects unauthenticated remote mode explicitly.

Linked memory is a per-project setting with no Settings screen and no Settings
destination. It is not RAM or a memory budget. Butler discovers the typed
`nebula.settings.linked_memory` action, which reads the project's stage, its
revision, the Core-wide kill switch and curation status, and
`nebula.settings.set_linked_memory`, which changes the stage. The stages are
`off`, `shadow`, which curates memory cards from completed top-level goals and
records the cards a turn would receive without adding them, and `inject`, which
also adds up to three matching cards to each turn as untrusted reference data.
Change the stage only when the operator asks in the current conversation: read
first, pass the read revision as `expected_revision`, and report Core's
read-back rather than the request. Moving to `inject` requires
`confirm_inject` after the operator explicitly confirms it. Goals that
completed before the first enable are never curated. See
[Linked memory](../LINKED_MEMORY.md).

## File attribution in Butler mode

Butler mode's **Code & files** section shows which chat last changed each
workspace file: a dot in the file list (orange for a chat that needs the
operator, blue for one running), bars beside changed lines (green added, orange
changed) and a **Who changed this file** list with **Open brief** for each
chat. It has no setting. Core reads `GET /api/v1/projects/{project_id}/files/attribution`
(each path's most recent change in the last 7 days by default, at most 500
paths, newest first, with the chat's board state) and
`.../files/attribution/history?path=` (up to 50 changes to one path, newest
first). Paths are relative to the project workspace, as the file browser shows
them. Attribution comes from two records: a turn or tool observation written
when a project native lifecycle hook runs (path level only, so provider chats
in projects without hooks show nothing), and harness file-change activity
(Codex patches and Claude Write or Edit calls, with line counts and changed
line ranges where a patch was recorded). A change seen while other turns ran in
the same workspace is marked uncertain; Butler reports it as possible, never as
proof that a chat made the edit.

# Project tool approval policy

New projects start with approval policy `never`. Project approval policy `never` grants permission bypass for every enabled
Core-managed tool: provider, native harness gateway, MCP, SSH and browser tools.
It overrides approval requirements, scope restrictions and explicit policy denies.
Authentication, disabled capabilities, input validation, execution resource limits,
and operating-system controls still apply. Existing conversations refresh this
policy at the next turn; command sessions refresh before the next command.
Changing away from `never` removes its derived bypass, while a separately saved
Allow all authorization remains effective. Discover and change this setting through
the existing project execution-policy destination, not individual tool exceptions.
