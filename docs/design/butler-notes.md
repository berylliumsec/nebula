# Butler project Notes

Entry: an explicit request in interactive Butler to find, create, edit or delete
a project note. Butler pages note identities and link targets, reads bounded
body chunks, then prepares exact values for the requesting browser. Core owns
the note, its references and revision; Notes owns only temporary selection and
unsaved drafts. Scheduled Butler has no note mutation actions.

| Journey step | Observable invariant | Authority | Check |
| --- | --- | --- | --- |
| Discover/select | Only the selected project's editable note types and same-project asset/evidence link targets are returned | Core store and relation projection | Capability tests |
| Create | One browser-owned note ID reconciles an uncertain create; the exact title, body and links are read from Core | Dedicated Core draft route | API, component and real-Core browser |
| Edit | Only requested fields change at the observed revision; other metadata survives | Core note validator and revision | API, component and browser |
| Read/use | The saved note appears by title in Project Notes and can be selected for a report | Core catalog and existing Notes UI | Production-LAN browser |
| Refresh/retry | Refresh shows the same note and body; lost responses read the exact ID before another attempt | Core record and browser receipt | Component and browser |
| Delete | Browser confirms the exact note; Core refuses report dependencies and stale revisions; absence is read back before success | Core dependency and revision guards | API, component and browser |

This record mutation does not execute a notebook, stream a model turn, transform
writing, or retrieve file bytes. Creating or editing notes does not attest that
their claims are verified findings.
