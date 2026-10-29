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
The branch's project Work catalog has 53 named operations, including discovery
and reads; these counts are not a coverage percentage.

## Current coverage

| Operator area | Butler can do now | Gap or boundary |
| --- | --- | --- |
| Chats and agents | Discover selected-project work; start, message, steer, stop, fork, rename, archive, restore, delete and retry assistant chats; inspect goals, subagents, queues and schedules | The overview shows at most 30 main chats. Side chats, temporary assistants and Butler's own session are excluded from Work actions. Chat rewind and export are UI-only. |
| Chat priority | Open the conversation list and see pinned and flagged chats in the UI | Before this branch, `nebula.work.update_chat` had no pin or flag arguments or read-back. |
| Goals, missions and approvals | Read and change Core-owned goals, missions, follow-up queues, schedules, plans, approvals and nonsecret harness questions through typed Work actions | Native harness goals only expose the controls the harness actually supports. A saved queue or approval receipt does not prove the downstream task completed. |
| Project work board | Main agents can receive and update assigned work through the agent Work gateway | Before this branch Butler had no direct typed item actions. This branch adds project-scoped list, read, create, edit, and check-in. Work agent-access setting and full update history remain gaps. |
| Butler memory | List and search visible preferences and lessons; save a quoted memory; edit or forget one on operator request with its current revision | Memory is context with provenance, not authority to change another project or certify a result. |
| Assistant runtimes | List available runtimes; change chat defaults and next-turn Assistant settings; inspect model and quota information | A next-turn setting does not rebind a running turn. Some runtime lifecycle controls remain in the visible settings screens. |
| Settings and navigation | Discover listed Settings routes, read schemas, use authenticated Core Settings API calls, open destinations, and request device theme or project mode/scope changes | The Settings gateway covers an allowlisted route family, not all Core APIs. Device actions are pending until that browser confirms them. One-time secrets and local file pickers stay on their protected UI paths. |
| Projects and global oversight | Navigate to projects; scheduled Butler can inspect and act on eligible projects with current Act authority and privacy scope | Interactive Butler is bound to the selected project. Project creation, rename, archive, deletion and restore do not have a complete typed Butler workflow. |
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

## Remaining work to reach the product goal

1. Add a maintained **workflow coverage registry** linking each operator UI
   action to its Core owner, Butler discover/read/change actions, required
   authority, and an observed-result test. Use it to detect newly added UI
   actions without silently making them model-callable.
2. Add typed project lifecycle and content actions in the owning Core services:
   project create/rename/archive/restore, Work agent-access setting and complete
   update history, conversation
   rewind/export, and bounded workspace/Notebook/resource management. Each needs target
   discovery, optimistic revisions where applicable, and a durable read-back.
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
