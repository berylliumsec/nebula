# Nebula assistant contract

Entry: the persistent assistant icon in the shared shell opens one movable,
hideable control conversation. Closing the window preserves its Core transcript.
The operator may open Settings from a catalog result without losing the draft.

| Journey | Observable invariant | Authority | Test layer |
| --- | --- | --- | --- |
| Discover and open | Icon is reachable on every route and at phone widths | Shell React state | Component and browser |
| Launcher placement | The assistant icon lives in the shared top bar, the phone Workbench navigation, or the phone focus toolbar; it never overlays the Zero dock or page content | Shell chrome and one persistent assistant conversation | Desktop and mobile browser geometry |
| Select runtime | Healthy configured provider or harness is chosen and named | Core profiles, shell selection | Component and real Core |
| Ask and stream | One durable control conversation receives ordered output | Core transcript and provider or harness turn | Real Core |
| Change setting | Exact target and value are validated; saved state is read back | Browser preference or Core entity | Browser and real Core |
| Confirm sensitive change | The operator sees scope and consequence before a write | Core approval and UI | Real Core |
| Hide and restore | Draft, turn, and transcript remain available | React presentation, Core turn | Browser and real Core |
| Refresh and reconnect | Transcript and pending action return without duplicates | Core and saved conversation identity | Real Core |
| Stop and retry | Only this assistant turn stops; the question remains editable | Core turn, React draft | Real Core |
| Close | Window closes without deleting history | React presentation, Core transcript | Browser and real Core |
| Compose | Enter submits one message; Shift+Enter inserts a newline; composition does not submit | React draft and Core turn | Component, browser and real Core |
| Open a named setting | An unambiguous open/show request opens the existing Settings lens immediately | Shared settings catalog and shell lens | Component and browser |
| Open an app destination | A request such as “show me my projects” navigates to the All projects page immediately; every static app route is available through the assistant catalog and exact model tool | Shared packaged destination list, URL and selected Core project | Tool unit, component, desktop/mobile browser, real Core |
| Change this device's theme | An explicit theme command opens Appearance, applies and confirms the browser preference, then closes the lens; “Zero theme” means Zero Dark | Browser theme context and storage; shell lens owns presentation | Component and desktop/mobile browser |
| Model-selected browser action | The model calls an exact open-setting or device-theme tool; Core returns a typed pending action, and only the requesting browser applies and confirms it | Scoped tool receipt and browser theme/lens state | Tool unit, component, real-Core browser and live model |
| Fast answer | Provider turns request low effort; harness turns select advertised low effort and fast tier or matching fast model | Runtime model options and Core provider adapters | Component and real Core |
| Change project execution mode | An explicit request opens Project Policy, names the selected project, confirms Host access, saves through the existing policy API, reads back the mode, refreshes the visible control, and closes the lens after showing the result | Core automation policy; shell lens owns presentation | Component, browser, real Core and LAN |
| Explain a Nebula problem | A question receives an answer grounded in the selected project's effective mode and linked workspace when those facts matter; it does not infer operator intent from an automatically stored Docker default and distinguishes future policy from existing frozen command sessions | Core project and automation policy; model owns explanation | Tool unit, provider and harness boundary, real Core model turn |
| Help resolve a problem | A suggested Settings destination opens the same visible lens as search; a mode change still requires an explicit request and Host access confirmation | Shared setting catalog, shell lens, Core policy | Component, browser, real Core and LAN |

The setting catalog lists every Settings section. Current direct actions cover
device appearance, provider and harness default models from their advertised
lists, project post-tool note and suggestion preferences, and project execution
mode through the assistant UI with explicit consent. Other sections
open their existing Settings controls. A direct action is allowed only
for bounded values handled by an existing validation and save path. Browser
appearance is applied in the active browser. Core-owned settings are applied by
Core, with read-back before reporting success. Secrets, uploads, record creation,
deletion, and multi-step setup open their existing Settings controls. Security,
connectivity, runtime, and disruptive changes require explicit confirmation.
Neither model text nor an MCP annotation substitutes for that confirmation.

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
`nebula.settings.set_project_mode` expose exact bounded actions. The tool receipt
is an intent, not a completed browser change. The requesting assistant window
validates the action, applies it, and reports the observed result. The
`nebula.settings.catalog` tool lists exact action names and argument values when
the model needs to discover a destination or capability. Replayed tool events
are deduplicated by tool-call id. Browser action outcomes remain attached to
their Core message in browser storage so the requesting device shows the
confirmation again after refresh. The transcript follows new results into view.
The general assistant never receives these tools.
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
operator sees the existing control and approves Host access before Core saves.
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
search, or a way to bypass the existing Settings approval path. General chat
still does not receive Nebula settings tools. When its answer cites a known
Settings destination, the UI opens that destination as a lens on click.
The product knowledge index is generated from current top-level product
documentation, the Settings catalog, and this contract, chunked by headings with source paths and
content hashes, embedded locally, and packaged as a compressed text resource
that the package build generates and Core loads as a read-only SQLite vector database in memory. A full-text index answers immediately if the local embedding
model has not been prepared; dense retrieval is added when that model is
ready. Build verification rejects an index whose source digest no longer
matches the documentation. The index describes product behavior; Core-owned
project policy and workspace state always come from the live context tool.
Provider turns request low effort; Core's provider adapter maps or omits that
control for the selected model. Harness model options determine low effort and
fast selection. An existing harness conversation may rebind to its advertised
fast sibling while retaining the Core transcript.

The focused test selection must cover the capability catalog, provider and
harness tool paths, Core authorization and read-back, shell visibility, settings
handoff, desktop and mobile browser geometry, reconnect, and production LAN
origin. Physical-device behavior is unverified until exercised on a device.
