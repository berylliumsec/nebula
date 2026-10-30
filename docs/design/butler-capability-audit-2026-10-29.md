# Butler capability audit — October 29, 2026

## Meaning of full operator control

Butler should be able to discover and carry out every **operator workflow** that
Core can safely delegate, with the same target, policy, revision, confirmation,
and read-back rules as the visible UI. That is a product coverage goal, not a
grant to call arbitrary API routes or return credentials to a model. A device
action still needs the requesting browser; a Core action still needs Core's
authorization; a long-running action still needs an observed result.

This is a source audit of `origin/main` at `b5bb47c` and the capability registry,
not a claim that every action was exercised in a running Core. `api.py` contains
373 HTTP route decorators across 41 tags. Route count is an inventory aid: some
routes are internal, some are read-only, and several implement one workflow.
The branch's project Work catalog has 57 named operations, including discovery
and reads; these counts are not a coverage percentage.

## Current coverage

| Operator area | Butler can do now | Gap or boundary |
| --- | --- | --- |
| Chats and agents | Discover selected-project work; page saved main chats; start, message, steer, stop, fork, rename, archive, restore, delete and retry assistant chats; inspect goals, subagents, queues and schedules; rewind an exact saved user message at a current revision; request a confirmed transcript download in the current browser | Side chats, temporary assistants and Butler's own session are excluded from Work actions. The operating system controls the final save location for a transcript. |
| Chat priority | Page and read pinned and flagged chats; pin, unpin, set or clear a flag with the current revision and Core read-back | The browser sidebar shows Core's saved state after refresh; this branch's paired LAN journey covers that path. |
| Goals, missions and approvals | Read and change Core-owned goals, missions, follow-up queues, schedules, plans, approvals and nonsecret harness questions through typed Work actions | Native harness goals only expose the controls the harness actually supports. A saved queue or approval receipt does not prove the downstream task completed. |
| Project work board | Main agents can receive and update assigned work through the agent Work gateway | This branch adds Butler's project-scoped item list, read, create, edit, check-in, agent-access setting, and paged check-in history. Browser and real-Core proof is tracked separately below. |
| Butler memory | List and search visible preferences and lessons; save a quoted memory; edit or forget one on operator request with its current revision | Memory is context with provenance, not authority to change another project or certify a result. |
| Assistant runtimes | List available runtimes; change chat defaults and next-turn Assistant settings; inspect model and quota information | A next-turn setting does not rebind a running turn. Some runtime lifecycle controls remain in the visible settings screens. |
| Settings and navigation | Discover listed Settings routes, read schemas, use authenticated Core Settings API calls, open destinations, and request device theme or project mode/scope changes | The Settings gateway covers an allowlisted route family, not all Core APIs. Device actions are pending until that browser confirms them. One-time secrets and local file pickers stay on their protected UI paths. |
| Projects and global oversight | Navigate to projects; interactive Butler can page and read projects and request create, rename, archive, restore, and permanent delete through the requesting browser; scheduled Butler can inspect and act on eligible projects with current Act authority and privacy scope | The browser checks Core revisions, confirms removal, and reads back results. The branch records each Butler create request and serializes normalized names in Core. Host folder selection and Host consent remain in their visible controls. |
| Workspace, Code and terminals | Interactive Butler can page project workspace entries, read bounded UTF-8 text, create or replace text, rename a regular file with its current SHA-256, and ask the requesting browser to confirm deletion or download verified bytes | Browser file selection and binary upload, directory mutation, terminal lifecycle, and linked-host-folder selection remain gaps. Scheduled Butler has no file actions. |
| Browser, Notebook and research data | Configure Notebook mode and start or stop the Core-owned headless security browser through the requesting device; page Notebook files and cells and create or edit a cell with Core read-back; page shared Library metadata and request browser-owned upload, original-file download, reindex or removal | Direct browser page control, Notebook run lifecycle and package installation, evidence, reports and exports still lack accepted Butler journeys. Security testing remains under the project's scope and approval policy. |
| Integrations and operations | Discover some provider, harness, MCP, environment, credential, diagnostics, release and device Settings routes | Storage source enrollment, secret rotation, deployment, pairing redemption, system service control and one-time token display remain specialized operator workflows. |

The present gateway is deliberately small: Butler receives
`nebula.capabilities.search`, `describe`, `read`, and `change`; Core resolves each
named operation. Project Work actions are defined in `control_assistant_work.py`,
Settings routes in `settings_assistant_api.py`, device destinations in
`assistant_destinations.json`, and background/global authority in
`butler_capabilities.py` and `butler_global_capabilities.py`. A search result is
not itself permission to perform the action.

## Branch slice: organize any saved main chat

Entry point: the operator asks Butler to find and pin, unpin, flag, clear a flag,
archive or restore a saved project conversation. Butler pages the roster when
the bounded overview is insufficient, reads the exact chat, changes it through
the existing revision-checked Core route, and reports Core's saved state.

| Step | Observable invariant | State authority | Check |
| --- | --- | --- | --- |
| Discover | Butler's catalog describes `nebula.work.chats` and the update fields | Core capability registry | Tool test |
| Select | Paging reaches an older main chat; private and other-project chats are excluded | Core chat sessions | Tool test |
| Change | Current revision and listed color are required; stale or invalid choices do not save | Core API and database | Tool test, real Core |
| Read back | Butler sees pin and flag in detail and the page; the sidebar shows the same saved state after refresh | Core database; UI presentation | Tool test, real-Core browser |
| Recover | A stale revision tells Butler to reread; an active response prevents archive but permits priority changes | Core conflict contract | Tool test, real Core |

The URL owns the selected project and chat identity. Core owns chat metadata and
revision. React owns only the open menu and expansion state. These actions do
not create a new chat, stream output, change the model, or revoke access.

## Branch slice: manage project Work items

Entry point: the operator asks Butler to find, create, assign, reprioritize, or
post a status update on a Work board item in the selected project. Butler pages
the project roster, reads the exact item and revision, uses the owning Core Work
route, then reports the saved item or check-in and its board URL.

| Step | Observable invariant | State authority | Check |
| --- | --- | --- | --- |
| Discover | The catalog names exact Work item actions and their fields; paging reaches older tasks | Core catalog and Work store | Tool test |
| Select | Cross-project item IDs and assignees are rejected; the item URL opens the selected board card | Core Work service and URL | Tool test, real Core |
| Change | Create and check-in use stable request IDs; edit and check-in require the current item revision | Core Work service | Tool test, real Core |
| Read back | Butler receives the saved status and revision; the board shows the same item after refresh | Core database and UI | Tool test, real-Core browser |
| Recover | A stale revision fails without overwriting an operator edit; repeating an already saved request ID returns the same object | Core conflict and idempotency contract | Tool test |

Creating or checking in a Work item does not run an assigned chat. The Work board
and Core database own task state; Butler's prose is not a completion receipt.

## Branch slice: Work agent access and check-in history

Entry point: the operator asks Butler to enable or disable the selected
project's agent Work tools, or to review an older Work item check-in. Butler
reads the exact project setting or item, changes the setting with the current
project revision, and reports the saved Core value. Check-ins are paged by
item, with exact text available in bounded chunks.

| Step | Observable invariant | State authority | Check |
| --- | --- | --- | --- |
| Discover | Butler's catalog names the Work setting and paged history actions | Core capability catalog | Tool test |
| Select | An item and update must both belong to the selected project; paging reaches updates past the UI's recent window | Core Work store | Tool test, real Core |
| Change | A stale project revision cannot toggle agent tools; disabling does not erase Work records | Core project revision and Work setting | Tool test, real Core |
| Read back | Butler sees the new setting and full exact check-in text; the Work board shows the same saved setting and update after refresh | Core database and UI | Tool test, real-Core browser |
| Recover | Conflict directs Butler to reread; missing or cross-project updates are refused | Core conflict and project boundary | Tool test |

The setting controls agent access to the built-in Work MCP. It does not stop a
running agent, alter an assigned chat, or delete historical check-ins. Stream,
interrupt, and delete lifecycle steps do not apply to these two actions.

The focused candidate checks passed: 11 selected Python cases, 17 frontend
cases with CI's 15-second per-test limit, and the selected paired LAN journey
in desktop Chromium 1440 px, compact Chromium 1024 px, Android Chromium
320/390/430 px, and iPhone WebKit 320/390/430 px emulation. The browser journey
uses the production bundle, verifies the served asset hash, reads the saved
check-in through Butler, and checks the disabled agent-tools setting and Work
card after refresh. Physical Safari was unavailable. These checks validate this
branch slice; they do not prove the remaining workflows below.

## Branch slice: project lifecycle

Entry point: an operator asks the interactive Butler to find, create, rename,
archive, or restore a project. The project switcher remains the visible control
for selecting and confirming a project change. Core owns the project record,
revision, default scope, and Host policy. The URL and Workspace context own the
browser's current selection; Butler owns only the pending request and its
reported result. Scheduled Butler cannot use these cross-project actions.

| Step | Observable invariant | State authority | Check |
| --- | --- | --- | --- |
| Discover and select | Butler pages projects, including archived ones, and reads an exact ID, name, status, and revision before a change | Core project records | Tool test, real Core |
| Create | A new project has a default scope and an unacknowledged Host policy; the browser checks all project names before posting and lists matching projects after an uncertain response. Core atomically stores the request ID, actor, and fingerprint with the project and policies. The same receipt returns the same ID; a changed request or deleted target conflicts; concurrent Butler creates cannot reuse a normalized name | Core project creation route, retry receipt and policy | Core concurrency, API, tool, component, real Core browser |
| Rename and restore | A stale revision is refused; a saved project can be read again by exact ID and appears under the updated name or active list after refresh | Core project revision and switcher | Tool test, real Core browser |
| Archive | The requesting browser shows the same project-specific confirmation as the switcher before applying an exact revision; cancelling keeps the project active | Browser confirmation and Core project record | Component, real Core browser |
| Delete | Permanent deletion remains behind the switcher's explicit irreversible confirmation and Core's dependency and revision guards | Browser confirmation and Core deletion lock | Existing project-deletion journey plus new Butler handoff test |
| Reconnect | The selected URL and project list agree with Core after refresh or a switch away from the archived project | URL, Workspace context, Core | Real Core browser |

Streaming, interruption, and fork are not project-record operations. A project
create or archive does not stop running work; the switcher already explains this.
Workspace folder selection belongs to the Core host picker and is a separate
handoff. The preflight protects sequential retries, including archived-name
matches. Core's durable receipt and transaction-level name guard protect Butler
creates when two browsers pass the preflight together. Other project creation
paths retain their existing name semantics. A retry of a deleted project's
request ID fails closed instead of recreating it.

## Branch slice: chat transcript export

Entry point: the operator asks interactive Butler to export one saved main chat
in the selected project. Butler finds the exact chat and current revision through
its project roster, then requests a handoff to the requesting browser. The
browser shows the existing sensitive-transcript confirmation and obtains the
full active transcript through its authenticated Core API. The model receives
only the pending action and short outcome, never the full export. Core owns the
chat, revision, and messages; the browser owns confirmation and the local
download. The URL keeps the selected project identity.

| Journey step | Observable invariant | State authority | Test layer |
| --- | --- | --- | --- |
| Discover and select | Butler's catalog names export and resolves an exact listed main chat and current revision | Core capability catalog and chat store | Tool test |
| Confirm and prepare | Cancellation makes no download; a changed or other-project chat fails before data is downloaded | Browser confirmation and Core revision | Component and real Core |
| Download and read back | The requesting browser fetches all active messages, checks the chat again for a stable revision, and starts a Markdown download with the same filename and content as the chat menu | Core transcript and browser | Component and production LAN browser |
| Refresh and retry | No server-side export state is created; a new request reads a fresh chat revision and requires confirmation again | Core chat and browser | Tool and real Core |
| Failure | Fetch, stale revision, or device download failure reports a recoverable error without claiming a saved file | Core error contract and browser | Component and real Core |

Streaming, interruption, fork, and deletion do not apply to a local snapshot
download. A browser download event is evidence that the file transfer started;
the operating system's final save location remains device-owned.

The selected Python and frontend checks passed. A disposable paired LAN Core
served the production bundle for the export journey, and the browser downloaded
the complete synthetic transcript after confirmation in desktop Chromium,
compact Chromium, Android Chromium 320/390/430 px, and iPhone WebKit
320/390/430 px emulation. Cancellation started no download. The model stub
never received the synthetic transcript marker; a refresh showed Butler's
reported device action. Physical Safari and the operating system's final save
location were unavailable to this automated run.

## Branch slice: workspace file control

Entry point: an operator asks interactive Butler to find, inspect, create, edit,
rename, delete, or download a file in the selected project workspace. Core's
workspace service owns path containment, linked-folder behavior, exact bytes,
mutation preconditions, and durable operation events. The URL owns the selected
project; the requesting browser owns local downloads and file picker input.
Butler must list paths before selecting a target and must use a full-file SHA-256
from a current read before replacing, renaming, or deleting a regular file.

| Journey step | Observable invariant | State authority | Test layer |
| --- | --- | --- | --- |
| Discover and select | Butler pages directory entries and reads bounded UTF-8 text with the full-file hash; symlinks, FIFOs and other-project paths are refused | Workspace service and selected project | Tool and real Core |
| Create and replace | Create refuses an existing destination; replace requires the current hash, writes atomically, and reports a matching Core read-back | Workspace file and Core upload precondition | Tool, API, real Core |
| Rename and delete | A stale hash cannot rename or remove a regular file; delete needs the same explicit confirmation as Files, and both actions check the saved path afterward | Workspace service and requesting browser | Tool, component, real Core |
| Download | Only the requesting browser receives file bytes and starts the local download; a changed or wrong-project target fails before the browser reports success | Core download and browser device | Component and production LAN browser |
| Refresh and retry | Files and Code show the Core result after reload; an uncertain write is reconciled by reading the exact path and hash before another attempt | Workspace file, URL, browser state | Real-Core browser |

Binary upload still requires the device file picker. Directory mutation, terminal
lifecycle, and linked host folder selection require separate controls and evidence.
Streaming and fork do not apply to one file operation. A browser download event
proves the transfer started, not the operating system's final save location.

The selected 13 Python and 63 frontend cases passed. A disposable paired LAN
Core served the production bundle for the workspace journey in desktop and
compact Chromium, Android Chromium 320/390/430 px, and iPhone WebKit
320/390/430 px emulation. The browser downloaded the exact synthetic bytes,
cancelled one deletion, confirmed another, and restored Butler's result after
refresh. Physical Safari and the operating system's final save location were
not observed. The file-hash check is a Core precondition before mutation;
external terminal writes are outside the workspace API's serialization.

## Control contracts and remaining work to reach the product goal

### Shared Library control contract

Entry point: an operator asks interactive Butler to find a shared Library item,
add one from the requesting device, reindex it, download its original file, or
remove it from retrieval. The Library is global: one change affects every
project. Core owns the item, revision, immutable artifact, and local index;
the requesting browser owns file selection, download, and confirmation. The
Library control actions return bounded item metadata, not the original file.
When Butler's separate Knowledge setting is enabled, Core may retrieve bounded
excerpts from ready Library items into the model context as reference data.

| Journey step | Observable invariant | State authority | Test layer |
| --- | --- | --- | --- |
| Discover and select | Butler pages global item summaries and reads one current revision before acting; a project switch invalidates its pending browser action | Core Library and selected Butler project | Tool and browser |
| Add | Butler opens a browser-owned picker; cancellation leaves Core unchanged. The browser confirms global impact and sends a supported file to Core. It reports a saved item only after exact detail and list read-back; an uncertain response requires Library inspection before retrying | Browser picker, Core artifact and Library item | Component and real Core |
| Reindex | The exact item and current revision are checked before rebuilding; saved status and new revision appear after refresh | Core item and index | API, tool and real Core |
| Download | The browser confirms sensitive original-file transfer, rereads the exact revision before and after fetching the artifact, then starts a local download; the model receives only an outcome | Core artifact and browser | Component and real Core |
| Remove | A global-impact confirmation names the exact item; stale revisions fail and a successful removal disappears from the refreshed Library | Core item and browser confirmation | API, tool and real Core |
| Failure and reconnect | Cancellation has no effect, uncertain responses prompt a fresh Core read, and stale or missing items cannot be retried blindly | Core and browser | Component and real Core |

These actions belong only to an interactive Butler session. Scheduled Butler
must not mutate the shared Library through project Act authority.

The completed Shared Library slice passed 30 cumulative focused Python cases,
75 frontend cases, the production UI build, and the paired LAN journey in
desktop and compact Chromium plus Chromium and WebKit mobile emulation at
320, 390 and 430 px (8/8). The browser run verified the served asset hash,
upload cancellation with no Core change, browser-owned upload with exact Core
detail and list read-back, a confirmed original-file download with matching
bytes, reindex at a new revision, stale-request refusal, Core 404 after
removal, retained artifact bytes, the Library empty state after navigation,
no horizontal clipping, and an accessibility scan. The model requested upload
and download without receiving file bytes. It separately received a bounded
Library excerpt through the enabled Knowledge setting; the action tool returned
metadata only. Physical Safari and the device operating system's final save
location were unavailable to this automated run.

### Notebook control contract

Entry point: an operator asks the interactive Butler to find a notebook in the
selected project, inspect an exact cell, edit it, or run and interrupt it.
Core's Notebook service owns the project workspace file, its revision and cell
hashes, the execution queue and result events. The project's Notebook grant,
execution mode and Host consent remain Core policy; the Workbench URL owns the
selected notebook in the browser. Butler must list paths before choosing one,
read the exact current revision before a write, and report a saved file or run
receipt only after Core read-back.

| Journey step | Observable invariant | State authority | Test layer |
| --- | --- | --- | --- |
| Discover and select | Butler pages project Notebook paths and bounded cell previews; an exact source read does not return an entire 20 MB document | Notebook service and project workspace | Tool and real Core |
| Create and edit | A missing or revoked Notebook grant fails; creation refuses an existing path; cell writes use the current document revision or source hash, and a stale edit preserves the file | Notebook grant, workspace file and cell hashes | Tool, API and real Core |
| Execute and interrupt | A saved code cell returns a run ID; Core policy and Host consent are enforced; ordered results and interruption are read from that run | Notebook runtime and Core run store | Tool and real Core |
| Refresh and reconnect | The same path, source hash, saved cell and run status appear in Notebook and Butler after reload | Workspace file, Core run store and URL | Production LAN browser |
| Failure and retry | Permission, stale revision, unavailable runtime and uncertain run effects identify the next safe read; no blind duplicate execution | Core error contract | Tool and real Core |

Package installation and project file-index configuration are separate Notebook
operator actions. Their network and Host policies must be exercised before
calling Notebook control complete. Browser keyboard and mobile checks apply to
the selected Notebook journey, even when Butler's tool routing is server-side.

### Chat history rewind contract

Entry point: an operator asks the interactive Butler to edit or retract a saved
user message in a selected project's main conversation. Butler pages the active
transcript to find the exact message, shows its target and consequences, and
rewinds only after an explicit operator instruction. Sending replacement text is
a separate turn and must be reported separately. Core owns the chat revision,
messages, retraction history, harness session and pending activity; the URL owns
the selected conversation. Butler's page is a bounded read, not an export.

| Journey step | Observable invariant | State authority | Test layer |
| --- | --- | --- | --- |
| Discover and select | Butler can find the action and page active user/assistant messages past its recent-chat summary to select one saved user message ID | Core capability catalog and transcript | Tool test |
| Mutate | Core refuses a stale chat revision, non-user or retracted target, cross-project or private child chat, and a chat with an active response; a successful rewind retracts the selected user message and every later active message in one transaction | Core chat service and revision | API and tool tests, real Core |
| Read back | The chat ID stays fixed; active transcript and saved retraction history agree with the result, and a harness chat gets a fresh replay session | Core database and harness runtime | API and tool tests, real Core |
| Refresh and reconnect | The same active transcript and revision reappear in Butler and the conversation after refresh | Core database and URL | Real Core browser |
| Failure and retry | A conflict leaves history unchanged and tells Butler to reread; a second call with the old revision cannot retract a newly selected message | Core conflict contract | API and tool tests |

Creating a new chat, forking, exporting, streaming, and deleting are separate
journeys. Scheduled Butler can read the bounded page but cannot rewind history;
its Act authority does not turn a retrospective edit into a background repair.
The browser's existing edit control continues to use the same Core route without
a caller revision until its own edit journey is separately changed.

### Workflow registry contract

The operator journey is to ask Butler to find and perform an action that is
visible in Nebula. The UI and Butler must name the same Core-owned target, and
Butler's catalog must distinguish actions it can perform from actions requiring
the requesting device or a future typed implementation. The registry records
the UI entry point, Core owner, Butler discovery/read/change operations,
authority, and an observed-result test for each reviewed action. A source guard
must flag newly introduced UI API calls for review; adding a row alone must
never grant Butler a capability. Source and catalog own discovery, Core owns
durable state and authorization, the URL owns selected project identity, and
the browser owns device-local choices and confirmations. For this registry-only
slice, create, select, stream, interrupt, refresh, retry, and delete are the
lifecycles of the recorded workflows, not actions performed by the registry.
Each supported row must name a test of its own observed result; a gap row must
state the missing action or evidence. The source guard is a review alarm, not a
proof that every click has been catalogued.

The first registry lives in `docs/design/butler-workflow-registry.json` and is
checked by `ui/scripts/check-butler-workflow-registry.mjs`. It records 23
reviewed actions, including explicit export, workspace, Notebook, Library, and
storage dispositions. Its source inventory contains 505 distinct `api` call
signatures in production UI TypeScript, of which 44 are linked to those
reviewed actions; 461 still require action-level review. The new Workbench bulk
chat calls map to the existing per-chat archive, priority and deletion actions;
Butler can page and apply those actions to exact chats one at a time. Direct `api.request`
calls are keyed by route expression and HTTP verb. The guard fails when a call
is added, removed, or repeated without refreshing the inventory. It does not
see calls through other receiver names or UI actions with no API call, so the
registry is an incomplete coverage map. `supported` means the named Butler
operation and observed-result test exist; `gap` and `device_handoff` explicitly
name the missing operation or browser handoff. To review a new call, trace its
visible control to Core, add or update an action row, then run the script with
`--write-inventory`. Neither that command nor a registry row changes Butler's
tool catalog.

1. Expand the **workflow coverage registry** to every operator UI action,
   including calls through other receiver names and controls without API calls.
   Keep Core owner, Butler discover/read/change actions, authority, and an
   observed-result test current. The initial source guard detects changes to
   tracked `api` calls without making them model-callable.
2. Add bounded workspace, Notebook run lifecycle, and resource
   actions in their owning services. Each needs target discovery, optimistic
   revisions where applicable, and durable read-back.
3. Add specialized device and administration handoffs for local file selection,
   pairing, storage enrollment, credentials, release updates and service state.
   Keep one-time values on the requesting device and preserve existing
   confirmations and recovery paths.
4. Extend the selected-project browser journey and real-Core regression tests
   for each action family. A passed tool unit test proves routing, not the full
   operator workflow or a deployment.

Avoid a generic `method + path + body` gateway over all Core routes. It would
erase the distinction between an operator action, a device action, and an
internal API, and would make precise effects and read-back difficult to verify.
