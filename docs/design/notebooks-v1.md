# Bundled notebooks: operator contract

Entry: a project Workbench Notebook tab and the Notebook control beside chat.
The operator can create or choose a project `.ipynb`, pin it to the current
conversation, enable the managed Notebook MCP, and see cell edits and execution
output while continuing to use chat. Host execution requires the project's
one-time acknowledgement; Docker execution uses its selected runner and never
falls back to Host.

| Journey | Observable invariant | Authority | Verification |
| --- | --- | --- | --- |
| Discover and create | Notebook is visible in the Workbench and a new file appears in project Files | Workspace and Core catalog | Component and real Core |
| Select and pin | Tab URL and chat side panel identify the intended notebook after reload | URL for tab; Core chat session for pin | Browser and real Core |
| Edit and run | Concurrent operator and agent edits never silently overwrite a cell; queued execution uses the submitted source | Workspace file and Core run state | Unit and real Core |
| Stream and interrupt | Ordered output and final status appear in both views; interrupt is visible | Core run events | Real Core and browser |
| Background and reconnect | Tab changes do not stop the kernel; reload recovers saved cells, output, and current run | Core run state and workspace | Real Core and production browser |
| Failure and retry | Consent, package, Docker, and stale-revision failures name the next action | Core error contract | Component and real Core |
| Mode change | Active or queued cells block a Host/Docker switch; Project settings offers an explicit interrupt, then saves the new mode | Core run state and project policy | Component and real Core |
| Revoke and delete | Disabling Notebook MCP blocks new calls; deleting the file clears stale selection | Core grant and workspace | Real Core |

React owns only unsaved drafts and panel presentation. The Core database owns
consent, MCP grants, chat pins, and run state. The project workspace owns `.ipynb`
content. Browser storage does not determine the active notebook.
Host package lookup and installation use the Project settings package-download
switch; Host code itself follows the server OS network permissions. One-time
Host consent remains saved if the operator moves between modes.

The focused test selection covers backend policy, notebook service and MCP,
frontend editor and selection states, production browser desktop/mobile/LAN
journeys, and packaged runtime audit. Physical-device evidence is required when
available; an emulator is identified as such.
