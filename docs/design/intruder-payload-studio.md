# Intruder Payload Studio

Status: implemented v1
Branch: `codex/intruder-payload-studio`
Figma: https://www.figma.com/design/4XL4PWfQ5ALB1azQRQL0NL

## Outcome

An operator can build Intruder payload sets from four coherent sources: manual
entry, a local upload, an assistant-generated proposal, or a deterministic
declarative script. Every source converges on the existing bounded, inert payload
list consumed by `BrowserAttack`. Nothing in Payload Studio sends a request.
The operator must review the resulting values and save an attack draft before
the existing explicit queue action can run it on the owning desktop.

This preserves the current Intruder guarantees: project scope, selected browser
identity, strategy cardinality, request budget, concurrency, rate limits, durable
progress, and explicit queue/cancel/retry controls remain authoritative.

## Operator journey and invariants

Real entry point: Security Browser → Automate / Intruder → Payload source.

| Journey step | Observable invariant | State authority | Test layer |
| --- | --- | --- | --- |
| Discover | Manual, Upload file, Assistant, and Script are visible as one payload-source choice; unsupported sources explain why they are unavailable | Core capability response + transient UI selection | component + Playwright |
| Upload | Choosing or dropping a supported file shows filename, byte size, parsed count, warnings, and a bounded preview before values are added | browser `File` during parsing; unsaved React draft afterward | unit + component + Playwright |
| Generate | The operator chooses project sources and reviews a proposal with provenance; generation never saves or queues an attack | Core generation job and result; Core project context | real Core + Playwright |
| Script preview | A deterministic declarative program emits strings through the bounded `values`, `range`, and `prefix` operations | local parser/evaluator; unsaved React preview | unit + component + Playwright |
| Review | Values can be selected, removed, deduplicated, searched, and assigned to the required strategy position without losing their source summary | unsaved React draft | component + Playwright |
| Save draft | Success creates a durable `BrowserAttack` containing materialized inert values plus bounded provenance; the saved attack immediately appears and is selectable | Core database | real Core + Playwright |
| Queue/use | Only the existing Queue action can make the owning desktop execute the already-materialized values | Core attack state + desktop worker | real Core production workflow |
| Refresh/reconnect | Saved attacks and provenance return without rerunning a script or assistant; unsaved file bytes and proposals are not falsely presented as saved | Core database | real Core + Playwright |
| Failure/retry | Parse, generation, sandbox, stale-context, and save failures preserve editable input and name one safe recovery action | Core error contract + transient UI error | component + real Core |
| Delete | Existing attack deletion removes its retained results; payload source provenance does not outlive the attack | Core database | real Core |

Additional invariants:

- File contents, generated values, and script output are always data, never code.
- Previewing a source cannot send browser traffic or consume an attack request.
- Secret headers, cookies, captured bodies, and artifact bytes are excluded from
  assistant and script context unless a future separately approved disclosure
  flow is designed.
- An uploaded filename is metadata only; Core never receives or opens a client
  filesystem path.
- Attack execution uses materialized values stored at save time. It never reruns
  an assistant or script and therefore stays deterministic and resumable.
- Changing positions or strategy invalidates incompatible position assignments
  visibly; it never silently reorders payload sets.
- Mobile/LAN operators can create and review sources, while only the owning
  paired desktop retains authority to execute the queued attack.

## State model

`payloadSource` is a tagged draft union owned by the Intruder editor:

```text
manual    { sets, edited }
upload    { filename, mediaType, bytes, parseOptions, sets, warnings, digest }
assistant { request, selectedContextRefs, proposalId, sets, provenance, warnings }
script    { language, source, inputRefs, previewId, sets, receipt, warnings }
```

The durable attack remains the execution authority. Add bounded provenance to
`BrowserAttack.metadata` initially, or promote it to a typed field if query or
retention requirements emerge:

```json
{
  "payload_source": {
    "kind": "upload | assistant | script | manual",
    "display_name": "users.txt",
    "sha256": "...",
    "value_count": 248,
    "generated_at": "...",
    "context_references": [],
    "runtime": {"language": "nebula-payload-v1", "budget": "intruder-payload-v1"}
  }
}
```

Raw uploaded files and assistant prompts are not required for execution and are
not retained by default. Script source should be retained only when the operator
explicitly saves it as part of the draft; otherwise retain its digest and bounded
execution receipt with the materialized values.

## API plan

### Parse uploads in the client

Use a hidden accessible `<input type="file">` and quiet upload target. Parse text,
CSV, and JSON arrays only after enforcing the 2 MiB input bound.
Limits must match or be stricter than Core:

- 2 MiB maximum input file;
- UTF-8 only in v1, with actionable decode errors;
- 10,000 values per set and 50,000 combined;
- 16,384 characters per value;
- text lines, one selected CSV column, or a flat JSON string/number array;
- empty-row, trimming, and duplicate behavior shown before adding values.

The existing attack-create endpoint receives materialized `kind=list` values.
Core repeats all cardinality and length validation and never trusts the parser.

### Assistant proposal

The existing project-scoped writing-transform API receives a bounded request
summary: attack name, method, URL template, and position names. Headers, cookies,
bodies, captured content, and artifact bytes are excluded. The configured
normalized assistant must return a flat JSON string/number array. The client
validates and previews it, retaining model/provider provenance. Generation does
not create or mutate a `BrowserAttack`.

Generation requires a configured provider/harness capability. When unavailable,
the Assistant source remains discoverable but disabled with an in-place setup
explanation. Cancellation and stale-context detection are explicit.

### Script preview

V1 deliberately uses a declarative payload language evaluated in the client.
It supports only `values(...)`, `range(start, end[, step])`, and
`prefix(text, start, end[, step])`, with comments and quoted JSON strings. The
parser rejects every other statement. It has no request hooks, network,
filesystem, environment, imports, clocks, randomness, or general code execution.
Output is capped at 10,000 values and uses the same per-value length limits as
upload and attack creation. The operator reviews materialized output before
saving; execution never reruns source text.

This avoids `eval`, `Function`, Node `vm`, and direct Python execution while
still covering the common generated-sequence workflow. A future general-purpose
language requires a separately reviewed isolated Core sandbox and API.

## Interface design

Payload source is a quiet tab row inside the existing Intruder editor, not a new
top-level Security Browser tool. The source panel and review panel stay visible
together on desktop. At 320–430 px they stack into source → validation summary →
preview → primary action, with no horizontal editor dependency.

- Upload emphasizes file selection, parse settings, warnings, and a bounded
  preview. It does not show an opaque server path.
- Assistant emphasizes selected context, a concise instruction, provenance,
  per-value selection, and Regenerate versus Add selected values.
- Script uses a compact monospaced editor, names the language capabilities,
  separates Run preview from Add previewed values, and shows limits adjacent to
  the action.
- The primary action adds reviewed values to the unsaved attack. Saving and
  queueing remain separate decisions.
- Long lists virtualize; counts and validation stay pinned while details remain
  progressively disclosed.
- Every icon-only action has a tooltip, accessible name, visible focus, and a
  44 px touch target. The unfamiliar and consequential actions retain text.

## Delivery slices

1. Shared payload-source draft model, review UI, parser worker, and upload tests.
2. Existing typed writing-transform API, capability/readiness state,
   cancellation, provenance, and assistant review UI.
3. Declarative script preview, bounded parser/evaluator, and failure UI.
4. Attack provenance persistence, reload/reconnect behavior, mobile polish, and
   production real-Core acceptance.

Each slice is independently shippable only if unsupported future source tabs are
hidden or clearly disabled and the existing manual path is unaffected.

## Verification plan

Before running tests, bind the selected files/projects/counts to the current diff
with `scripts/test_selection.py`; do not run the full suite.

- Unit: parsers, normalization, deduplication, cardinality, strategy assignment,
  proposal schema, and script output validation.
- Component: upload/drop/keyboard flows; invalid encoding/type/size; assistant
  disabled/loading/cancel/error/proposal/review; script edit/preview/timeout/error;
  long values, empty states, focus restoration, and unsaved-source switching.
- API: scope and ownership, provider unavailability, cancellation, durable
  provenance, and no attack mutation during assistant generation.
- Real-Core Playwright: create each source from the visible Intruder entry point,
  save, refresh, queue on the owning desktop, observe results, pause/resume/cancel,
  retry failure, and delete.
- Permanent browser matrix: desktop Chromium at 1440 and 1024; emulated Android
  Chromium and iPhone WebKit at 320, 390, and 430.
- Production bundle and non-loopback LAN origin, including upload and downloads,
  because browser file APIs and origin behavior are involved.
- Accessibility: automated scan plus keyboard, touch, focus order, labels, zoom,
  reduced motion, and no horizontal clipping.

Physical device and live configured-provider evidence must be reported honestly;
their absence makes the corresponding capability partially verified, not complete.

## Decisions to retain

- Materialize values before attack execution; never execute source logic in the
  desktop attack loop.
- Assistant proposals require explicit source selection and value review.
- Scripting is deterministic preview generation, not a request hook and not an
  arbitrary automation surface.
- Use the current attack endpoint and limits for v1 instead of introducing a
  second competing payload execution model.
