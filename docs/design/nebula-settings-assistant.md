# Nebula assistant contract

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
| Change setting | Exact target and value are validated; saved state is read back | Browser preference or Core entity | Browser and real Core |
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
| Configure Butler Voice | The catalog opens Butler's compact Voice timeout control; the assistant can discover the authenticated Core-wide Voice settings route and report a change only after Core read-back | Core Voice settings | Tool unit, component and real Core |
| Manage Nebula-wide Butler instructions | The catalog opens the editable BUTLER.md Markdown code editor above the Settings popup after Core loads the file. The full Settings page retains its file row and preview; upload opens a draft. The API checks the loaded digest before saving and the UI reads Core back before confirming | Core data directory and durable Butler global clock | Tool unit, component and real Core browser |
| Set Butler model and effort | The catalog opens Butler settings. Full Assistant settings opens Butler's saved conversation with its Assistant settings panel visible. Core saves the selected effort on Butler's chat; the next direct turn and each new scheduled harness check use that saved choice. An unset effort uses the model's advertised default | Core Assistant settings and harness session | Core unit, component and real Core |
| Explain a Nebula problem | A question receives an answer grounded in the selected project's effective mode and linked workspace when those facts matter; it does not infer operator intent from an automatically stored Docker default and distinguishes future policy from existing frozen command sessions | Core project and automation policy; model owns explanation | Tool unit, provider and harness boundary, real Core model turn |
| Help resolve a problem | A suggested Settings destination opens the same visible lens as search; a mode change still requires an explicit request and Core's Host access acknowledgment field | Shared setting catalog, shell lens, Core policy | Component, browser, real Core and LAN |
| Check project work | The selected project's assistant chats and missions are listed with bounded current status and exact targets. In Workbench conversations, chats Butler creates appear below Butler; older recovery chats use their linked Core goal as ownership evidence. Search reveals matching children, and a selected child remains open after reload | Core sessions, goal metadata, turns, queues and runs; URL for selection | Tool unit, component, browser and real Core |
| Organize conversations | In the Workbench conversation list, the More actions menu can pin a chat above the normal activity groups or set a small red, orange, yellow, green, blue, or purple flag. Butler can page the project chat roster, then use the target's current revision to pin, unpin, set, or clear a flag and read Core's saved result. These choices survive refresh because Core owns them. An archived Butler-created chat moves into Butler's own collapsed Archived section and remains searchable and restorable | Core chat-session metadata; UI owns only expansion state | Core API, Butler tool, component, browser and real Core |
| Brief recent agent work | Butler resolves a named Assistant, pages saved activity for the requested number of hours, includes delegated agents, and cites the source chat and record for claims about progress or repeated attempts | Core transcript, turn ledgers, harness activity, subagent lineage and goal reports | Tool unit, real Core and browser |
| Start or guide work | A new chat or mission uses an enabled listed runtime; an existing chat receives a durable queued follow-up or live harness steer; a mission uses the existing start or steer path | Core chat queue, harness and mission services | Tool unit and real Core |
| Interrupt work | An exact selected chat turn or mission is stopped through its existing Core lifecycle and read back | Core turn or run | Tool unit and real Core |
| Ask for a task plan | The chat's Assistant settings asks any provider or harness agent to share a plan on future turns, or Butler records one exact one-task request with the target revision and reason. Normal tools remain available, and the activity view shows whether a plan was shared | Core assistant config, directive, and task plan | Tool unit, component, browser and real Core |
| Hand off a previously planning-blocked goal | Butler reads a paused harness goal and uses a revision-checked action after Core verifies that its latest failed turn was never admitted and ran zero tools. Core links one provider draft to the paused source; the new planning preference is advisory | Core goal, turn, queue, approval, provider, and handoff records | Core unit, component, real Core and browser |
| Edit a goal's resume instruction | Chat → Goal → Edit goal shows the saved text; the current Core wording is the default. Saving it updates the goal by revision, and the next explicit Resume sends that text in the same conversation. Automatic continuation turns retain their separate Core instruction | Core goal record and resumed turn; goal editor owns the draft | Core unit, component, real Core and browser |
| Complete a provider goal | A tool-enabled provider agent calls the offered `goal_complete` tool (`goal.complete` in Core) with a summary and concrete evidence after meeting the goal criteria. Core saves the completed goal with the final answer, labels the evidence agent-reported, and starts no further continuation; the goal panel shows the saved state after refresh | Core goal, turn ledger, final message and goal panel | Core unit and real Core browser |
| Switch a saved harness chat to a provider | Chat → Assistant settings → Runtime → Provider checks the chosen provider and model, then Core changes the same chat's durable binding at an idle boundary. The chat ID, transcript, and paused goal stay in place; the next provider turn is saved there. An active response, running or blocked goal, working subagent, or queued follow-up gives an actionable refusal | Core chat binding and goal; React owns only the unsaved picker choice | Core unit, desktop/mobile browser, real Core and LAN |

The setting catalog lists every Settings section, Butler's Voice control, and
the Nebula-wide Butler instruction editor. Dedicated actions handle
device appearance, provider and harness default models, post-tool preferences,
project execution mode, target scope and headless browser sessions. For other
Core-backed settings, `nebula.settings.api_schema` lists only the current build's
Settings routes and request schemas. `nebula.settings.api_request` calls one of
those existing routes through Core authentication. The selected project ID is
supplied by Core, and the assistant cannot select a different project in a
route. Existing API validation, revision checks and required acknowledgments
still apply. Browser appearance remains device-owned. Uploads and local desktop
operations remain subject to the capabilities of the active device. An explicit
operator setting request authorizes the assistant action without a second
confirmation dialog. A pending browser action is never reported as saved.
Storage source enrollment stays in the visible Storage settings form because its
webhook token is shown only once. The settings assistant can discover and open
that form or navigate to the global Storage dashboard; it does not create or
rotate source tokens through generic Settings API calls. Remote storage actions
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
`nebula.work.chat`, `nebula.work.mission`, `nebula.work.start_chat`,
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
Butler can save a different main harness and provider subagent on the same chat.
The saved choice becomes the bound runtime when a new message starts; Butler
checks the resulting turn before reporting a goal resumed. Immediate messages
carry the same saved MCP servers and Plan choice as queued next messages.
For a harness-to-provider change, opening the saved chat's Assistant settings
and choosing Provider uses Core's runtime-switch preflight and apply actions.
The switch is saved only after Core reads it back as a provider chat. It waits
for the harness response and subagents to finish, and for the goal to be paused
or settled with its follow-up queue clear. Provider turns then append to the
same transcript and retain the original goal ID. Saving only a next-turn
Assistant configuration is not proof that the backend binding changed.
`nebula.work.require_plan` records a one-task request against an exact
agent, goal, child, or turn, using a fresh target revision and concrete reason.
An active harness turn reports that the directive takes effect next turn. The
saved plan and Butler reason remain visible in the chat activity after refresh.
Butler's automatic one-task force requires the project's delegated action grant,
current Core evidence, and a recorded reason.
`nebula.work.handoff_goal` creates one provider draft only for a paused harness
goal whose latest planning attempt stopped before admission with no tool calls.
The source retains its queue and remains paused. Core checks pending approvals,
selected provider model/tool verification, MCP selection, privacy scope, and
remaining budgets before linking the goals atomically. A repeated request reads
back the same continuation. Butler starts it through a separate goal action;
the source and target link and reason appear in both goal panels after refresh.
The handoff carries neither approval nor queue authority. For a scheduled check,
`nebula.global.handoff_goal` performs the same checked create and can start the
new draft under an Act grant. When handoff is unavailable, Butler reports the
hold and can select another eligible project.
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
For MCP cost questions, the assistant explains that unticked, on-demand servers
contribute a compact name-and-purpose directory to each provider routing request,
while checked servers and tools pinned under Project policy contribute full
function schemas each time. It points to Assistant settings for checked servers
and Project policy for Always loaded tools; unchecking a global server keeps it
available through the on-demand catalog.
Provider turns request low effort; Core's provider adapter maps or omits that
control for the selected model. Harness model options determine low effort and
fast selection. An existing harness conversation may rebind to its advertised
fast sibling while retaining the Core transcript.

The focused test selection must cover the capability catalog, provider and
harness tool paths, Core authorization and read-back, shell visibility, settings
handoff, desktop and mobile browser geometry, reconnect, and production LAN
origin. Physical-device behavior is unverified until exercised on a device.
