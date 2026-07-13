# Nebula 3 feature plan: assistant code, execution, logging, notes, and exports

## Goal

Bring the useful Nebula 2 operator workflow into the Nebula 3 React/Tauri
workspace:

1. Render code in assistant messages as easy-to-copy, language-aware blocks.
2. Let an operator explicitly run a code block or selected text in an isolated
   engagement environment.
3. Persist commands and output automatically as part of the engagement record.
4. Generate draft notes and next-command suggestions from recorded output.
5. Make PDF the primary report export format.

This work must preserve Nebula 3's existing safety boundary: no command may run
on the host, the webview must never receive a container-runtime socket, and an
unavailable runner must leave execution disabled rather than falling back to a
host shell.

## Product decisions required before implementation

- [ ] **D1 — Environment lifetime:** Decide whether one environment is reused
  for an engagement, for a terminal session, or recreated for every execution.
  Proposed default: reuse an engagement environment while the engagement is
  active, with an explicit reset action.
- [ ] **D2 — Persistence:** Define which engagement directories are mounted,
  their container paths, size limits, backup behavior, and cleanup policy.
  Proposed default: mount only the engagement-owned workspace read/write; do
  not expose arbitrary host paths.
- [ ] **D3 — Network policy:** Define the allowed egress profiles and UI for
  choosing targets. "Can hit the network" must mean broker-approved scoped
  egress, not unrestricted networking. Default to no network.
- [ ] **D4 — Supported interpreters:** Confirm the first-release language map.
  Proposed minimum: `bash`/`sh` and `python`/`python3`. Unknown languages remain
  copy-only and cannot be executed.
- [ ] **D5 — Execution confirmation:** Decide which commands need an additional
  confirmation or approval. The Core policy broker remains authoritative even
  if the UI considers a command low risk.
- [ ] **D6 — Automation controls:** Decide whether automatic notes and command
  suggestions are enabled per engagement or globally, which provider they use,
  and whether output may be sent to a non-local provider. Proposed default:
  opt-in per engagement with the active provider's privacy policy displayed.
- [ ] **D7 — PDF meaning:** Confirm whether "PDF exports by default" applies to
  reports only or to every engagement export. Proposed default: report export
  downloads a PDF; the existing `.nebula.zip` remains the lossless portable
  engagement backup.

## Delivery plan

### Phase 1 — Assistant code-block rendering

- [ ] **UI-1: Render fenced code blocks in assistant messages.**
  - Parse assistant Markdown without allowing raw HTML or unsafe URLs.
  - Render the declared language, syntax highlighting, preserved whitespace,
    horizontal scrolling, and line wrapping appropriate for code.
  - Add a **Copy** button that copies the block exactly and shows success/error
    feedback.
  - Treat malformed or unclosed fences as inert text; never infer execution
    merely because prose resembles a command.
  - Likely scope: the chat/message components in `ui/src` plus focused UI tests.

  **Acceptance criteria**
  - `bash`, `python`, JSON, and language-less fences render legibly in both
    themes.
  - Copy preserves newlines and special characters exactly.
  - HTML/script content is escaped, and rendering never triggers execution.
  - Keyboard users can focus and activate Copy.

- [ ] **UI-2: Add explicit Run and Run selection actions.**
  - Show **Run** only for languages supported by D4 and only when Core reports
    an available runner.
  - Allow the operator to select part of a supported code block and choose
    **Run selection**; otherwise run the whole block.
  - Before submission, show the exact source, interpreter, engagement,
    environment, workspace mounts, network profile, and any required approval.
  - Submit structured data (`language`, `source`, and execution options), never
    concatenate source into a shell command in the UI.

  **Acceptance criteria**
  - No selection runs the entire block; an active selection runs exactly the
    selected characters.
  - Unsupported or ambiguous languages cannot run.
  - Double-clicking cannot create duplicate executions (use an idempotency key
    and disable the action while submitting).
  - Cancel closes the review step without creating an execution record.

### Phase 2 — Isolated engagement environment

- [ ] **CORE-1: Define the execution and environment contracts.**
  - Add typed request/response models for environment create/status/reset and
    code execution start/cancel/result.
  - Represent interpreter, working directory, environment ID, timeout,
    resource limits, mount policy, network/target policy, and idempotency key as
    explicit fields.
  - Return stable error codes for unavailable runner, unsupported language,
    policy denial, approval required, timeout, cancellation, and output limit.
  - Record environment and execution state durably so the UI can reconnect.

  **Acceptance criteria**
  - Closed schemas reject unknown fields, invalid paths, unsupported languages,
    and attempts to request host execution.
  - API and WebSocket contracts have unit/contract tests.
  - Repeating an idempotent request does not run the source twice.

- [ ] **CORE-2: Implement a devcontainer-like engagement environment.**
  - Extend the existing rootless Docker/Podman sandbox rather than adding a
    second execution path.
  - Use a digest-pinned image containing the approved Nebula toolbox and the
    supported interpreters.
  - Run as a non-root user with bounded CPU, memory, PIDs, runtime, and output.
  - Mount only paths approved by D2. Normalize and validate working directories
    to prevent traversal or symlink escape.
  - Implement create, health check, reconnect, reset, and guaranteed cleanup.
  - Route network access through the existing policy broker and certified
    per-invocation egress boundary. Do not use unrestricted bridge/host mode.

  **Acceptance criteria**
  - Source executes inside the approved container and cannot access the host
    filesystem or container-runtime socket.
  - Workspace files persist according to D1/D2 and survive UI/Core reconnects.
  - Network is off by default; allowed targets work only through the selected
    scoped policy, while other destinations are blocked.
  - Missing/unhealthy Docker or Podman produces analysis-only mode.
  - Reset removes environment-local state without deleting engagement evidence.

- [ ] **CORE-3: Stream and control executions.**
  - Stream ordered stdout/stderr/status events over the authenticated event
    channel and support cancellation.
  - Apply bounded chunks, total output limits, terminal-safe encoding, and
    explicit truncation markers.
  - Reuse the existing run-event replay rules so reconnecting clients resume
    after the last accepted sequence.

  **Acceptance criteria**
  - stdout and stderr remain distinguishable and ordered.
  - Refresh/reconnect does not lose or duplicate displayed events.
  - Timeout, cancellation, non-zero exit, and output truncation are visible and
    stored with the final status.

### Phase 3 — Automatic command and output records

- [ ] **DATA-1: Persist an immutable execution record.**
  - Store the exact submitted source, interpreter, operator, timestamps,
    engagement/environment IDs, working directory, policy decision/approval,
    selected network scope, image digest, exit status, and resource outcome.
  - Store stdout/stderr as content-addressed artifacts or chunked evidence, not
    as unbounded database fields. Include hashes and truncation metadata.
  - Link records to the originating assistant message/code block when present.
  - Redact configured secrets before display or downstream AI processing while
    retaining a clear audit indication that redaction occurred.

  **Acceptance criteria**
  - Every attempted execution, including denied, failed, timed-out, and
    cancelled attempts, has an auditable terminal state.
  - Records and artifacts remain available after restart and are included in
    the engagement bundle export.
  - Evidence integrity verification detects modified output.
  - Secrets covered by the redaction policy do not appear in UI logs or model
    prompts.

- [ ] **UI-3: Add an execution history/output view.**
  - Display live and historical executions with command/source, status, start
    time, duration, exit code, stdout, and stderr.
  - Add search/filter, copy, download-output, cancel-running, and rerun actions.
  - Rerun must open the review step with the original values; it must never
    execute immediately.

  **Acceptance criteria**
  - History is engagement-scoped, paginated, and restored after restart.
  - Large output remains responsive and clearly shows truncation/redaction.
  - The view meets keyboard navigation and screen-reader labeling requirements.

### Phase 4 — Automated notes and command suggestions

- [ ] **AI-1: Generate draft notes from completed executions.**
  - Trigger only when automatic notes are enabled and an execution reaches a
    terminal state.
  - Send bounded, redacted output plus relevant engagement context to the
    configured provider.
  - Produce a structured draft containing summary, observations, potential
    findings, evidence links, and source execution IDs.
  - Require operator review/edit/accept/reject; generated text must not silently
    become a verified finding or final report content.
  - Deduplicate retries using the source execution ID and prompt/version key.

  **Acceptance criteria**
  - Drafts cite their source execution/evidence and preserve provenance.
  - Provider failure never affects the execution record and can be retried.
  - Disabling the feature stops new drafts without deleting existing notes.
  - Empty, binary, oversized, and redacted-only output are handled explicitly.

- [ ] **AI-2: Generate safe next-command suggestions.**
  - Generate structured suggestions from the latest execution, active scope,
    and prior accepted context.
  - Show rationale, expected purpose, risk class, target, and required approval
    beside each suggested command.
  - Suggestions are inert text until the operator chooses Review and Run; they
    follow the same policy and execution path as manually submitted code.
  - Never extract or execute commands from arbitrary model prose.

  **Acceptance criteria**
  - Suggestions outside engagement scope are rejected or visibly marked
    unavailable by Core policy.
  - Accepting a suggestion still displays the exact command in the review step.
  - Suggestions link back to their model/provider/prompt provenance and source
    execution.
  - Duplicate completion events do not create duplicate suggestions.

### Phase 5 — PDF-first report export

- [ ] **REPORT-1: Add deterministic server-side PDF rendering.**
  - Define a versioned report template with engagement metadata, scope,
    executive summary, findings, notes, evidence references, and page numbers.
  - Sanitize all rich text and safely handle long code/output, Unicode, images,
    page breaks, and missing evidence.
  - Pin the renderer and fonts so the same stored report revision produces a
    reproducible result. Store the generated PDF as an artifact with its hash.
  - Keep `.nebula.zip` as the complete machine-readable engagement export.

  **Acceptance criteria**
  - A report can be exported as a valid PDF without internet access.
  - Untrusted report content cannot load local files, execute scripts, or make
    network requests during rendering.
  - The PDF identifies its report revision and generation timestamp and links
    evidence consistently.
  - Representative long, empty, Unicode, image-heavy, and malicious-content
    fixtures pass automated tests.

- [ ] **UI-4: Make PDF the primary report action.**
  - Label the main Reports action **Export PDF** and show progress/failure state.
  - Offer the portable `.nebula.zip` bundle separately as **Export engagement
    backup** so users do not confuse a presentation document with a full backup.

  **Acceptance criteria**
  - Export PDF downloads/opens the artifact returned by Core and does not rely
    on browser print-to-PDF behavior.
  - Repeated export of an unchanged report follows the chosen cache/revision
    policy and does not create confusing duplicate records.

## Cross-cutting requirements

- [ ] Add database migrations with downgrade/rollback coverage for every new
  persistent entity.
- [ ] Enforce engagement authorization and scope in Core; UI hiding is never an
  authorization control.
- [ ] Add structured audit events for environment lifecycle, execution,
  approvals, AI drafts/suggestions, and exports.
- [ ] Add retention controls for environments, execution artifacts, AI drafts,
  and rendered PDFs.
- [ ] Document setup, supported runtimes/languages, network profiles, volume
  behavior, reset/cleanup, privacy implications, and recovery steps.
- [ ] Add observability for queue time, runtime, failure reason, output size,
  environment health, AI latency/cost, and PDF render failures without logging
  command secrets.
- [ ] Test Linux and macOS/Tauri behavior with the approved Docker/Podman
  profiles; keep browser-only mode functional in analysis-only mode.

## Recommended implementation order

1. Resolve D1–D7 and write the API/domain contracts (CORE-1).
2. Implement immutable execution records and the isolated environment
   (DATA-1, CORE-2, CORE-3).
3. Build code rendering and operator-triggered execution (UI-1, UI-2, UI-3).
4. Add automated notes and suggestions on top of recorded execution events
   (AI-1, AI-2).
5. Implement deterministic PDF rendering and make it the primary report action
   (REPORT-1, UI-4).
6. Complete security, migration, end-to-end, packaging, and recovery tests.

## Definition of done

The feature set is complete only when an operator can copy or explicitly run a
supported assistant code block, review all execution parameters, observe live
output, reconnect without data loss, find the immutable command/output record,
review AI-created notes and suggestions linked to that record, and export a
report as a safe PDF. All execution must remain container-isolated and
policy-controlled, with no host-shell fallback and no unrestricted network or
host-volume access.
