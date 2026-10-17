# Butler improvement issues

Butler can review finalized messages saved in a Nebula project and incidents
with evidence references. Each enabled issue destination belongs to one project.
Creating a destination starts observation from that point forward; it does not
publish old transcripts. Butler skips its own control chat. A host Codex profile
reviews bounded evidence with Nebula tools disabled and proposes at most five
separate issue drafts per source. Core stores source references, drafts, and
publication receipts. A proposed `verified` confidence is reduced to
`hypothesis` because transcript claims alone do not independently verify a bug.

## Operator setup

In Butler oversight, add a destination, choose an enabled host Codex reviewer,
and select either GitHub Issues or a trusted MCP issue tool. For GitHub, save a
token in the credential store and choose a repository from the accessible list.
For MCP, select an enabled trusted server with the contract below and enter its
`owner/name` repository identity. Automatic publication starts only when the
operator checks the grant for that destination. Disabling the destination stops
new review and publication. A destination's repository, publisher, and MCP
server cannot be changed after creation; create another destination to change
the target. This keeps existing candidates attached to their original target.

The host Codex turn has no issue-publishing credential. Core rechecks target
identity and visibility before every create, searches for the candidate's
deduplication key, scans all outgoing fields, and adds an opaque key marker to
the issue body. Public drafts require a reproduction that stands independently
of private research; a draft with uncertain or private wording is held in Core
for revision. Private destinations still reject detected secrets. Review the
held and published lists in Butler oversight. A create whose outcome is
uncertain is never retried automatically: the next check searches by key and
records an existing issue if found. An empty lookup does not prove the create
failed. The later review and implementation of published issues are separate.

## Trusted MCP issue tool contract

The selected server advertises `issue_target`, `issue_find`, and `issue_create`.
Each tool returns a JSON object through `structuredContent` or JSON text
content. The server must authenticate its own backend and must ensure that the
reported repository identity and visibility correspond to the actual create
target. Core checks these fields but cannot independently prove a third-party
server's statements.

| Tool | Input | Required output |
| --- | --- | --- |
| `issue_target` | `{ "repository": "owner/name" }` | `{ "repository": "owner/name", "visibility": "public" \| "private" }` |
| `issue_find` | `{ "repository": "owner/name", "dedup_key": "..." }` | `{ "found": false }` or `{ "repository": "owner/name", "dedup_key": "...", "id": "...", "url": "https://..." }` |
| `issue_create` | `{ "repository": "owner/name", "dedup_key": "...", "title": "...", "body": "..." }` | `{ "repository": "owner/name", "dedup_key": "...", "id": "...", "url": "https://..." }` |

`issue_find` must search the destination by the exact key and include recently
created issues. After an ambiguous create, Core retains an uncertain receipt
even if lookup temporarily returns no result. An operator can inspect the
destination before any manual recovery.
