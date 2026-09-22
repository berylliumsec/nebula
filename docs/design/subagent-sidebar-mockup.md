# Nested subagents in the assistant sidebar

[Open the Figma mockup](https://www.figma.com/design/UbP5nXrRdXYbzbYG0gHShq?node-id=2-2)

![Collapsed and expanded sidebar mockup](subagent-sidebar-mockup.svg)

## Proposed operator behavior

- The conversation list shows each main conversation once. Its subagent sessions sit directly below it and start collapsed.
- The disclosure button toggles the children; the main row still selects the main conversation. A child row selects that child's conversation.
- The collapsed row reports the child count and any child needing attention. A main conversation with a waiting child appears in **Needs you** so the request stays discoverable.
- A waiting child keeps its parent in **Needs you** even if the parent was archived.
- Opening a child through a link, search result, or restored selection expands its ancestor. Search results show the matching child with its parent context.
- Ordinary conversation branches retain their own navigation treatment. A shared `parentSessionId` alone does not establish that a session is a subagent.
- The same hierarchy appears in the mobile conversation drawer, with disclosure and child actions meeting the 44 px touch target.

## Implementation contract

| Journey step | Observable invariant | State authority | Focused test layer |
| --- | --- | --- | --- |
| Discover | A main conversation appears once with a child count; true subagents start hidden | Core session list plus local disclosure state | Component and Playwright |
| Create | A newly spawned subagent joins its parent's group after the list refreshes | Core session metadata and parent ID | Real Core and Playwright |
| Select and use | Selecting the parent opens the parent; selecting a child opens that child's URL and transcript | URL and Core | Component and Playwright |
| Stream and interrupt | Child activity appears on its row; collapsed parents surface working and waiting activity | Core session activity | Component and real Core |
| Refresh and reconnect | The selected child remains reachable and its ancestor opens after reload; other groups default collapsed | URL, Core, local component state | Playwright and real Core |
| Search | A child title match shows the child beneath its parent; an unmatched child remains hidden | Core session list and local search query | Component and Playwright |
| Failure and retry | A failed session-list refresh retains the prior visible list and in-place recovery | Core response plus local error state | Existing session recovery tests |
| Delete and archive | A removed child disappears and the count updates; ordinary branches are not nested | Core session list | Component and Playwright |

Disclosure is temporary presentation state. The URL owns the selected conversation;
Core owns session lineage and activity. No migration or durable UI preference is
needed. The search query remains local to the sidebar. The selected child's parent
opens automatically on navigation so the active row is visible, including after a
reload or back/forward navigation.

## Evidence and implementation boundary

The current sidebar in `ui/src/pages/SessionsPage.tsx` groups a flat session array by activity and date. Subagent sessions already carry `parent_session_id` and a private `subagent_id` marker in Core, while ordinary branched sessions also carry `parent_session_id`. Implementation therefore needs a reliable public discriminator or Core-backed subagent relationship before it can safely nest only subagents. This is a design proposal; no product behavior has changed.
