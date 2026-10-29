# Butler capability audit — October 29, 2026

## Meaning of full operator control

Butler should be able to discover and carry out every **operator workflow** that
Core can safely delegate, with the same target, policy, revision, confirmation,
and read-back rules as the visible UI. That is a product coverage goal, not a
grant to call arbitrary API routes or return credentials to a model. A device
action still needs the requesting browser; a Core action still needs Core's
authorization; a long-running action still needs an observed result.

This is a source audit of `origin/main` at `aa797c9` and the capability registry,
not a claim that every action was exercised in a running Core. `api.py` contains
373 HTTP route decorators across 41 tags. Route count is an inventory aid: some
routes are internal, some are read-only, and several implement one workflow.
The branch's project Work catalog has 57 named operations, including discovery
and reads; these counts are not a coverage percentage.

## Current coverage

| Operator area | Butler can do now | Gap or boundary |
| --- | --- | --- |
| Chats and agents | Discover selected-project work; start, message, steer, stop, fork, rename, archive, restore, delete and retry assistant chats; inspect goals, subagents, queues and schedules | The overview shows at most 30 main chats. Side chats, temporary assistants and Butler's own session are excluded from Work actions. Chat rewind and export are UI-only. |
| Chat priority | Open the conversation list and see pinned and flagged chats in the UI | Before this branch, `nebula.work.update_chat` had no pin or flag arguments or read-back. |
| Goals, missions and approvals | Read and change Core-owned goals, missions, follow-up queues, schedules, plans, approvals and nonsecret harness questions through typed Work actions | Native harness goals only expose the controls the harness actually supports. A saved queue or approval receipt does not prove the downstream task completed. |
| Project work board | Main agents can receive and update assigned work through the agent Work gateway | This branch adds Butler's project-scoped item list, read, create, edit, check-in, agent-access setting, and paged check-in history. Browser and real-Core proof is tracked separately below. |
| Butler memory | List and search visible preferences and lessons; save a quoted memory; edit or forget one on operator request with its current revision | Memory is context with provenance, not authority to change another project or certify a result. |
| Assistant runtimes | List available runtimes; change chat defaults and next-turn Assistant settings; inspect model and quota information | A next-turn setting does not rebind a running turn. Some runtime lifecycle controls remain in the visible settings screens. |
| Settings and navigation | Discover listed Settings routes, read schemas, use authenticated Core Settings API calls, open destinations, and request device theme or project mode/scope changes | The Settings gateway covers an allowlisted route family, not all Core APIs. Device actions are pending until that browser confirms them. One-time secrets and local file pickers stay on their protected UI paths. |
| Projects and global oversight | Navigate to projects; interactive Butler can page and read projects and request create, rename, archive, restore, and permanent delete through the requesting browser; scheduled Butler can inspect and act on eligible projects with current Act authority and privacy scope | The browser checks Core revisions, confirms removal, and reads back results. Concurrent creates still need durable Core idempotency. Host folder selection and Host consent remain in their visible controls. |
| Workspace, Code and terminals | Give a configured main agent a task in its project workspace | Butler has no direct bounded file browser/editor or terminal lifecycle action. Linked-host-folder selection is device/host owned. |
| Browser, Notebook and research data | Configure Notebook mode and start or stop the Core-owned headless security browser through the requesting device | Direct browser page control, notebook content operations, resource/library management, evidence, reports and exports lack typed Butler actions. Security testing remains under the project's scope and approval policy. |
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
| Create | A new project has a default scope and an unacknowledged Host policy; the browser checks all project names before posting and lists matching projects after an uncertain response. A sequential retry is blocked when Core saved the first request | Core project creation route and policy | Tool test, component, real Core browser |
| Rename and restore | A stale revision is refused; a saved project can be read again by exact ID and appears under the updated name or active list after refresh | Core project revision and switcher | Tool test, real Core browser |
| Archive | The requesting browser shows the same project-specific confirmation as the switcher before applying an exact revision; cancelling keeps the project active | Browser confirmation and Core project record | Component, real Core browser |
| Delete | Permanent deletion remains behind the switcher's explicit irreversible confirmation and Core's dependency and revision guards | Browser confirmation and Core deletion lock | Existing project-deletion journey plus new Butler handoff test |
| Reconnect | The selected URL and project list agree with Core after refresh or a switch away from the archived project | URL, Workspace context, Core | Real Core browser |

Streaming, interruption, and fork are not project-record operations. A project
create or archive does not stop running work; the switcher already explains this.
Workspace folder selection belongs to the Core host picker and is a separate
handoff. The preflight protects sequential retries, including archived-name
matches. Two browsers can still race between the list and create requests;
Core needs a durable idempotency key or equivalent uniqueness contract before
Butler can guarantee one project per request under concurrency.

## Remaining work to reach the product goal

1. Add a maintained **workflow coverage registry** linking each operator UI
   action to its Core owner, Butler discover/read/change actions, required
   authority, and an observed-result test. Use it to detect newly added UI
   actions without silently making them model-callable.
2. Give project creation a durable Core idempotency contract. Add typed
   conversation rewind/export and bounded workspace, Notebook, and resource
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
