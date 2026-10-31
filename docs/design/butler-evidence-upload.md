# Butler evidence upload contract

An operator asks interactive Butler to add an immutable evidence file to the
selected project. Butler can page existing evidence, findings, and assets to
identify the intended links. The requesting browser chooses the local file;
Butler receives neither its path nor its bytes. Core owns the artifact, evidence
record, hash, and relations.

| Journey step | Observable invariant | State authority | Test layer |
| --- | --- | --- | --- |
| Discover | Butler lists the selected project's evidence and link targets with bounded pages; scheduled Butler cannot upload | Core capability catalog and records | Tool test |
| Prepare | A typed action carries only project, title, type, description and validated same-project links | Core store and Butler tool | Tool test |
| Choose and cancel | The requesting browser opens its file picker; cancellation creates no record | Browser device | Component and real-Core browser |
| Confirm | Before upload, the browser shows the file name and Butler's exact proposed title, type, description, finding, and asset names; declining creates no record | Browser confirmation | Component and real-Core browser |
| Upload | The browser sends a file of at most 25 MiB directly to authenticated Core and keeps its bytes out of Butler | Browser, Core evidence API and artifact store | Core API and real-Core browser |
| Read back | Core's exact evidence record, project list, SHA-256, size, artifact ID and selected links agree with the file and request before Butler reports success | Core database and artifact store | Core API and real-Core browser |
| Refresh and reconnect | The saved evidence appears in Evidence after reload; the browser action outcome remains with Butler's saved reply | Core, URL and browser action outcome | Real-Core browser |
| Failure and retry | A changed project or failed Core read prevents a success claim. An uncertain upload asks the operator to inspect Evidence before choosing the file again | Core and browser | Component and real-Core browser |

Existing evidence is immutable; deletion and revocation are separate workflows.
No stream, interrupt, or fork applies to one file upload. The device operating
system's final file-picker path is not exposed to the model.
