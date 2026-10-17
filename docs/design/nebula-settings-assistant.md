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
| Explain a Nebula problem | A question receives an answer grounded in the selected project's effective mode and linked workspace when those facts matter; it does not infer operator intent from an automatically stored Docker default and distinguishes future policy from existing frozen command sessions | Core project and automation policy; model owns explanation | Tool unit, provider and harness boundary, real Core model turn |
| Help resolve a problem | A suggested Settings destination opens the same visible lens as search; a mode change still requires an explicit request and Core's Host access acknowledgment field | Shared setting catalog, shell lens, Core policy | Component, browser, real Core and LAN |
| Check project work | The selected project's assistant chats and missions are listed with bounded current status and exact targets | Core sessions, turns, queues and runs | Tool unit and real Core |
| Brief recent agent work | Butler resolves a named Assistant, pages saved activity for the requested number of hours, includes delegated agents, and cites the source chat and record for claims about progress or repeated attempts | Core transcript, turn ledgers, harness activity, subagent lineage and goal reports | Tool unit, real Core and browser |
| Start or guide work | A new chat or mission uses an enabled listed runtime; an existing chat receives a durable queued follow-up or live harness steer; a mission uses the existing start or steer path | Core chat queue, harness and mission services | Tool unit and real Core |
| Interrupt work | An exact selected chat turn or mission is stopped through its existing Core lifecycle and read back | Core turn or run | Tool unit and real Core |

The setting catalog lists every Settings section and Butler's Voice control. Dedicated actions handle
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

For a Core-owned goal in a harness chat, the active agent can call `goal.block`
with a concrete reason. Core saves the blocked goal and stops automatic
continuations; the goal panel shows the reason and lets the operator resume.
Blocking a Project Work item or a native harness goal does not block the Core
goal. Core pauses a goal for review after three automatic responses with no
recorded tool activity, so a status-only loop cannot continue indefinitely.
The agent's block action is scoped to the goal on its active turn and cannot
choose another conversation's goal.

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
Provider turns request low effort; Core's provider adapter maps or omits that
control for the selected model. Harness model options determine low effort and
fast selection. An existing harness conversation may rebind to its advertised
fast sibling while retaining the Core transcript.

The focused test selection must cover the capability catalog, provider and
harness tool paths, Core authorization and read-back, shell visibility, settings
handoff, desktop and mobile browser geometry, reconnect, and production LAN
origin. Physical-device behavior is unverified until exercised on a device.
