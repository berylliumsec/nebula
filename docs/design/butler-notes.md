# Butler project Notes

Entry: an explicit request in interactive Butler to find, create, edit or delete
a project note, from the web app Butler panel, the Workbench chat or the terminal.
Butler pages note identities and link targets and reads bounded body chunks.
Core writes creates and edits itself, reads the saved note back and returns
`core_records_saved` with `read_back: true`; Butler says a note is saved only
after that read-back. Deletion stays an operator-confirmed browser action. Core
owns the note, its references and revision; Notes owns only temporary selection
and unsaved drafts. Scheduled Butler has no note mutation actions.

| Journey step | Observable invariant | Authority | Check |
| --- | --- | --- | --- |
| Discover/select | Only the selected project's editable note types and same-project asset/evidence link targets are returned | Core store and relation projection | Capability tests |
| Create | Core derives the note ID from Butler's `request_id` (UUIDv5); a retry with the same key replays the saved note and a changed note under the same key is refused; the exact title, body and links are read back | Core note validator and relations | Capability and component tests |
| Edit | Only requested fields change at the observed revision; other metadata survives; the saved revision is read back | Core note validator and revision | Capability and component tests |
| Read/use | The `note_saved` receipt refreshes Project Notes in the web app; the saved note can be selected for a report | Core catalog and existing Notes UI | Component tests |
| Refresh/retry | Refresh shows the same note and body; a lost response is retried with the same `request_id` or re-read by note ID | Core record and receipt | Capability tests |
| Delete | Butler returns `deleted: false` and a message that the operator must confirm in the web app; the browser confirms the exact note; Core refuses report dependencies and stale revisions; absence is read back before success | Core dependency and revision guards; browser confirmation | Capability, component and browser |

Terminal and Workbench chats cannot show the deletion confirmation. Their
Butler reports that the note was not deleted and that the operator must confirm
it in the web app Butler panel or delete it from Project Notes.

This record mutation does not execute a notebook, stream a model turn, transform
writing, or retrieve file bytes. Creating or editing notes does not attest that
their claims are verified findings.
