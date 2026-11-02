# Butler note review recovery

This checkpoint recovers the manual note generation and accepted-note visibility
repairs described in the interrupted PR #899 handoff. The old uncommitted files
and patches were not present in the replacement workspace or its unreachable Git
trees. The live PR was still at `7da378c6`; main `7332d9a0` has been reconciled.

## Acceptance contract

| Journey step | Observable invariant | State authority | Test layer |
| --- | --- | --- | --- |
| Discover | Accepted execution notes appear in Notes and Butler's paginated note catalog | Core observations; UI filters | component, Core capability, production browser |
| Generate | Choosing a harness for a manual draft does not accept it or create an observation | Explicit request flag; Core draft | actual API client, Core service, production browser |
| Review | Runtime choice and per-request cloud consent survive serialization | Browser input; Core privacy checks | client/component, Core service |
| Accept | Only an explicit operator acceptance creates the manual observation | Core transaction and draft revision | Core service, production browser |
| Select/use | Selecting the accepted note shows its saved body and AI/operator provenance | Core observation; transient editor state | component, production browser |
| Refresh/reconnect | Saved notes remain visible after reload; existing stored types and metadata remain intact | Core database | component, production browser |
| Failure/retry | Stale note edits are refused and no success is claimed without exact readback | Core revisions; browser handoff | Core capability, browser action |
| Delete/revoke | Existing project, revision and report-dependency guards also apply to accepted notes | Core relations/database | Core capability/API, browser action |
| Stream/interrupt | Not applicable: this checkpoint creates no execution and starts no work or chat turn | No command boundary is used | inert fixtures only |

Automatic post-tool note processing remains an explicitly requested mode of its
existing callers. Routing options do not select it. Notes recognize exactly
`note`, `ai_tool_note` and `ai_execution_note`; stored note types, original
content, evidence and acceptance metadata are preserved.

## Diagnosis and evidence

Fresh, independent read-only diagnosis traced manual harness generation through
the dialog's routing options to `automatic: Boolean(modes)` in the client and
Core's automatic acceptance branch. Core manual acceptance saves
`ai_execution_note`, while Notes and Butler's guards recognized only the other
two note types. Core and the wire execution type already carry a revision; the
client mapper and public UI type omitted it.

Reconciliation with main introduced two Alembic leaves sharing the same parent.
A second fresh independent diagnosis reproduced the failure without an existing
database. The additive `0025_merge_project_and_lookups` revision converges both
leaves without rewriting either migration. Tests cover fresh bootstrap, upgrades
from each leaf, retained envelopes and retry receipts, and repeated startup.

The lost Notes test was not recovered. Its replacement asserts the saved body
after React has synchronized the selected record and controlled editor, without
removing the saved-body assertion or adding a delay to product code.

## Verification status

The local focused cut passed 11 Python and 55 frontend cases. Production build,
Ruff, mypy for `butler_notes.py`, both diagnostic audits and the workflow-registry
check passed. The complete PR plan was recollected as 394 Python and 517 frontend
cases; the eight new permanent browser entries collect exactly eight cases and
bring the plan to 270 Playwright cases. Native tests remain unselected because no
native source changed. Collection is not execution evidence.

Local commands:

```sh
.venv/bin/python -m pytest -q tests/v3/test_butler_migration_merge.py tests/v3/test_entity_hot_lookup_migration.py tests/v3/test_butler_notes.py tests/v3/test_execution_ai.py::test_manual_harness_note_stays_ready_until_explicit_acceptance tests/v3/test_execution_ai.py::test_draft_context_is_bounded_redacted_deduplicated_and_accepts_once tests/v3/test_execution_ai.py::test_cloud_transfer_requires_confirmation_and_chat_attachment_stays_inert
npm --prefix ui test -- src/api/executionDraftTransport.test.ts src/components/ExecutionInsightDialog.test.tsx src/components/PostToolAssistant.test.tsx src/components/NotesPanel.test.tsx src/components/NotesPanel.executionDraft.test.tsx src/components/butlerNoteAction.test.ts src/components/ExecutionHistory.test.tsx
npm --prefix ui run build
```

Browser downloads from `cdn.playwright.dev` returned HTTP 403 `Domain forbidden`.
No managed Chromium or WebKit binary was available. The canonical LAN journey
with installed system Chromium 154 reached an Envoy HTTP 403 `Domain forbidden`
when the browser loaded `http://172.26.91.250:44233/`, before pairing or any draft
action. Core API, build manifest, entry JavaScript and pairing creation had
returned 200. Fresh independent trace diagnosis confirmed the environment gate.

A temporary copy changing only `localNetworkIpv4()` to loopback passed the same
journey in system Chromium 154 at 1440 by 900: manual generation stayed ready
with zero observations; operator acceptance saved exactly one note; its original
type/provenance and reviewed body were selectable after reload. The scoped Axe
and no-horizontal-clipping checks passed. The diagnostic copy was removed.
This is loopback evidence and does not replace the permanent LAN/browser matrix.
Physical devices are unavailable.

Fresh independent diagnoses also confirmed two fixture omissions: nested source
origin label/hash metadata, and an unchecked harness catalog with missing saved
output artifacts. The fixture now preserves Core validation, the journey checks
the inert harness through the real health route and rereads its advertised
models, and selecting the saved execution verifies real stored redacted output.
GitHub's original WebKit run had reproduced the missing-catalog failure; that
fixture correction is included in the next checkpoint.

The locally served bundle was built from `12793bd2` with `dirty=true`; its entry
JavaScript SHA-256 is
`2bda4de5ae36596cdfda5d399fafe60557088e28ab89c4a56f3fbe2c4dde6c21`.
The test verifies the served manifest and asset hash. This is not a claim that
the final committed head's complete production/LAN matrix has passed.

The generated product index is absent in this replacement workspace. Its normal
builder attempted to fetch the embedding model from
`chroma-onnx-models.s3.amazonaws.com` and received HTTP 403. Current-source index
generation/verification, the new guidance retrieval case, and current-source
Chroma synchronization remain unverified. Source extraction finds 342 passages
at digest `0e9fbef7f25d06b26d6408c0a4df428eb4a315fe654d090e624b6ba57b2848da`;
this is not a generated-index receipt. No environment configuration is published.

The workflow registry at the recovered head has 592 UI API signatures, 554
linked signatures and 38 unreviewed signatures. These repairs do not implement
the missing full-control capabilities and do not justify marking them covered.
PR #899 remains incomplete and must stay draft and unmerged.
