# Nebula settings assistant contract

Entry: the persistent assistant icon in the shared shell opens one movable,
hideable control conversation. Closing the window preserves its Core transcript.
The operator may open Settings from a catalog result without losing the draft.

| Journey | Observable invariant | Authority | Test layer |
| --- | --- | --- | --- |
| Discover and open | Icon is reachable on every route and at phone widths | Shell React state | Component and browser |
| Select runtime | Healthy configured provider or harness is chosen and named | Core profiles, shell selection | Component and real Core |
| Ask and stream | One durable control conversation receives ordered output | Core transcript and provider or harness turn | Real Core |
| Change setting | Exact target and value are validated; saved state is read back | Browser preference or Core entity | Browser and real Core |
| Confirm sensitive change | The operator sees scope and consequence before a write | Core approval and UI | Real Core |
| Hide and restore | Draft, turn, and transcript remain available | React presentation, Core turn | Browser and real Core |
| Refresh and reconnect | Transcript and pending action return without duplicates | Core and saved conversation identity | Real Core |
| Stop and retry | Only this assistant turn stops; the question remains editable | Core turn, React draft | Real Core |
| Close | Window closes without deleting history | React presentation, Core transcript | Browser and real Core |

The setting catalog lists every Settings section. Current direct actions cover
device appearance, provider and harness default models from their advertised
lists, and project post-tool note and suggestion preferences. Other sections
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

The focused test selection must cover the capability catalog, provider and
harness tool paths, Core authorization and read-back, shell visibility, settings
handoff, desktop and mobile browser geometry, reconnect, and production LAN
origin. Physical-device behavior is unverified until exercised on a device.
