# Nebula 3

Nebula 3 is the native, local-first desktop workbench and the only supported
Nebula application.

Structured Error-by-default logging, correlation IDs, the Diagnostics viewer,
and redacted support bundles are documented in the
[Nebula 3 local diagnostics guide](NEBULA3_DIAGNOSTICS.md).

## What is available

- UI-independent Pydantic domain contracts and SQLAlchemy storage.
- SQLite WAL locally and a PostgreSQL-compatible storage URL.
- Append-only, monotonically sequenced run events with authenticated replay.
- SHA-256 content-addressed artifacts and immutable execution evidence.
- Read-only Nebula 2.x data import with source manifests and rollback.
- Versioned REST/OpenAPI resources and authenticated WebSockets.
- Provider-neutral OpenAI Responses, Anthropic, Gemini, Bedrock, and
  OpenAI-compatible adapters.
- Explicit catalog profiles for commercial gateways and local runtimes,
  including Ollama, **vLLM**, llama.cpp, SGLang, LM Studio, Hugging Face
  endpoints, and NVIDIA NIM.
- LangGraph supervisor/specialist missions with durable investigative tool
  turns, approval pauses, independent evidence verification, exception retries,
  explicit blocked outcomes, and hard budgets.
- Typed tool plugins, strict JSON schemas, engagement-owned workspaces,
  broker-owned DNS resolution, scope enforcement, and rootless OCI execution.
- React/TypeScript workspace and Tauri shell with a loopback-only sidecar token
  handshake.
- A human-operated Kali terminal plus reviewed assistant code execution. The
  human terminal has an explicit root/writable/unrestricted boundary; reviewed
  and agent execution remains offline or single-target scoped.
- Deterministic server-rendered PDF reports, operator-triggered AI execution
  notes, review-first AI report drafting and note transforms, and
  integrity-manifested engagement bundle v3 export.

## Run from a source checkout

Nebula is packaged as one application and required imports fail immediately if
the installation is incomplete.

```bash
poetry install --with dev
poetry run nebula-core doctor
poetry run nebula-core migrate
poetry run nebula-core serve --host 127.0.0.1 --port 8765
```

Run the Nebula 3 backend test boundary:

```bash
poetry run pytest -q tests/v3
```

The server prints a generated bearer token. Remote binding requires an explicit
`--allow-remote` acknowledgement and should be placed behind a properly
authenticated deployment boundary. Local mode never exposes a runner socket.
Keep the Core port distinct from local model runtimes; vLLM commonly listens on
8000 or 8001. When developing the Vite workspace against another Core port, set
`NEBULA_DEV_BACKEND=http://127.0.0.1:PORT` before `npm --prefix ui run dev`.

Build and launch the browser workspace:

```bash
npm --prefix ui ci
npm --prefix ui run build
poetry run nebula-core ui
```

`nebula-core ui` is the recommended source-checkout launch path. It chooses an
available loopback port, starts Core, serves the built workspace, and transfers
the generated bearer token through the URL fragment. Running Vite alone starts
only the frontend and leaves durable and command-runtime controls offline.

Native-install users launch the desktop with `nebula`; `nebula-core` is reserved
for diagnostics, migrations, headless serving, imports, exports, and other
administration.

The browser token is carried in the URL fragment, consumed into memory, and
removed immediately. Tauri sends its 256-bit one-time token through the Core
process's stdin instead of a URL or process argument.

## Desktop installers

End users install one native application; they do not install Python, Poetry,
Node, npm, Rust, Cargo, or a compiler. The Tauri bundle contains the browser
workspace and a sibling `nebula-core` one-file executable with Python 3.12,
migrations, notices, and all mandatory Core dependencies. Package audits reject
GUI bindings and in-process model stacks from this build.

Release tags use the form `nebula-v3.x.y`. The protected release workflow builds
native macOS arm64/x64 DMGs and Linux x64 DEB/AppImage artifacts, audits their
contents, exercises the installed `--self-test`, creates SBOMs and provenance,
and stages a draft release. See [the release runbook](../packaging/RELEASING.md).

## vLLM

vLLM is a first-class local provider flavor using its OpenAI-compatible server.
The default profile endpoint is `http://127.0.0.1:8000/v1`, but endpoint and
model identifiers are discovered/configured at runtime and are not compiled
into the GUI.

Example provider profile:

```json
{
  "name": "Local vLLM",
  "provider_type": "vllm",
  "endpoint": "http://127.0.0.1:8000/v1",
  "enabled": true,
  "is_local": true,
  "capabilities": {
    "streaming": true,
    "cancellation": true,
    "tool_calling": false,
    "strict_structured_output": false,
    "parallel_tool_calls": false,
    "vision": false,
    "documents": false,
    "audio": false,
    "embeddings": false,
    "reasoning_controls": false
  },
  "privacy": {"local_only": true}
}
```

Enable tool calling only after the served model/template passes Nebula's strict
tool contract. A model without reliable structured calls remains analysis-only;
Nebula never extracts executable commands from prose.

The Settings workspace discovers provider types from Core. Choose **Add
provider → vLLM**, keep or edit the loopback endpoint, optionally set the served
model ID, then use the health button to call `/v1/models` and display the models
reported by that runtime.

An explicit vLLM profile can also back a durable CLI mission:

```bash
poetry run nebula-core run ENGAGEMENT_ID "Review the bounded scope" \
  --provider PROVIDER_PROFILE_ID \
  --model SERVED_MODEL_ID \
  --max-tool-calls 0
```

Model-backed specialists receive no executable tools in this path. The run
ledger records the provider, model, request provenance, token usage, and the
analysis result; tool execution remains exclusively behind the policy broker.

## Import and export

```bash
nebula-core import-2x /path/to/legacy-engagement
nebula-core export ENGAGEMENT_ID engagement.nebula.zip
```

See [Migrating from Nebula 2](MIGRATING-2-TO-3.md) before importing production
engagement data.

Import records before/after checksums and does not write to the source folder.
An external Chroma directory is skipped unless the operator supplies
`--allow-external-knowledge` explicitly.

The report workspace can draft an executive summary from selected findings and
notes, or transform a linked note into an editable report-local section. AI
output records provider, model, prompt, source hash, and operator instruction;
it is not persisted until the operator applies and saves it. Cloud transforms
require an eligible provider profile and per-request confirmation. PDF export
always renders the saved report revision.
The separate **Export engagement bundle (.nebula.zip)** action produces bundle
format v3 with entity records, run and operation events, execution streams,
human-terminal audit records and any selected-tool transcripts, generated drafts, report
snapshots/PDFs, and their content-addressed artifacts. Bundles may contain
unredacted evidence, raw execution output, and recorded security-tool terminal
results. Treat them
as sensitive data. They are not described as backups because Nebula 3 does not
yet provide a restore path. Scratch workspace files are excluded unless an
operator promoted them to an artifact.

## Context compaction

Analyst chats and model-facing mission dependency context are compacted
automatically when the estimated input approaches 75 percent of the configured
model capacity. Provider profiles may declare `context_window` and
`max_output_tokens` in their options; Core conservatively assumes an 8,192-token
window and a 2,048-token output allowance when no limits are configured.

Compaction uses the conversation or mission's selected provider and model, so it
can add model latency, token usage, and cost. Workbench and Activity show the
latest compaction status and usage. Compaction fails closed:
Nebula returns a retryable error instead of silently dropping older context when
a required summary cannot be validated.

Authenticated operators can inspect the same read-only status through
`GET /api/v1/chat/sessions/{session_id}/context` and
`GET /api/v1/runs/{run_id}/context`. These responses expose resolved limits,
active-context estimates, compaction usage and cost, coverage, and canonical
source references; they do not provide snapshot mutation routes.

Every original chat message, mission result, event, evidence record, and
LangGraph checkpoint remains unchanged. Derived working-memory snapshots cite
their canonical message sequences or task/result identifiers, are included in
engagement exports, and are never treated as evidence. Memory is isolated to a
single chat session or mission run; it is not shared across an engagement.

## Built-in operator help

Analyst chat deterministically retrieves a small number of matching articles from
the release-bundled [Nebula 3 Operator Help Knowledge Base](../src/nebula/v3/operator_help.md).
This product corpus is separate from Project knowledge uploads: it is available even
when a Project has no documents, contains no engagement data, and does not require a
cloud-knowledge transfer confirmation. Product-help citations use stable
`nebula-help:<article>` source IDs and a content-derived chunk ID.

The corpus covers startup and diagnostics, runner and workstation setup, Terminal,
automation runtime, policy/approval states, providers, reviewed execution, workspace limits,
context compaction, migration/import/export, and the current release boundary. If no
article matches an observed Nebula failure, the assistant is instructed to report
the exact error and say that no verified recovery procedure is available instead of
inventing a step. Command final synthesis also searches the observed failed result,
so recovery guidance can match an error that was not known when the turn began.

Treat the bundled Markdown as release material: update implementation references and
recovery steps in the same change as behavior, keep every command and UI label
verifiable, and never add host-execution, policy-bypass, destructive-data, mutable
image, example-digest, or guessed-log-path advice. The frozen-Core package audit
requires the corpus so installed agents cannot silently lose it.

## Workbench terminal, reviewed execution, and workspace limits

### Integrated Project browser

The desktop Workbench includes a multi-tab **Browser** backed by Tauri child
webviews (WKWebView on macOS and WebKitGTK on Linux). Browser pages are remote,
untrusted surfaces: they receive no Nebula IPC capability, Core token,
filesystem bridge, opener permission, Project data, evidence, or model context.
Navigation and pop-ups are limited to HTTP and HTTPS. Open tab URLs and history
remain memory-only and are discarded when Nebula closes.

Cookies, cache, and site storage use a separate persistent profile per Project
on Linux and macOS 14 or newer. macOS 13 uses an isolated non-persistent store
that is cleared when Nebula closes because that operating-system WebKit version
does not provide named persistent stores. **Clear Project browser data** closes
the Project's browser tabs and removes only that profile.

Website downloads are staged in Nebula's private cache, capped at the existing
1 GiB workspace-file limit, and streamed through the authenticated workspace
upload endpoint. Core therefore applies the same atomic write, total quota,
entry limit, traversal, symlink, and overwrite-confirmation rules as an
operator upload. A downloaded file enters Project Files only; it is never
promoted to evidence or sent to a model without another explicit action.

Nebula does not provide a host terminal. Workbench uses a human-operated
**Terminal** in a verified human-workstation image containing the
`kali-linux-headless` baseline. Core verifies the official base repository,
platform, immutable digest, derived image ID, and versioned build-recipe labels,
then persists the verified metadata locally. A fully verified cached base and
workstation image are inspected before any pull or build, so cached launches
make no registry request and remain usable offline.

The release configuration seam `NEBULA_HUMAN_TERMINAL_SOURCE_IMAGE` accepts
only a digest-pinned image in the official Kali repository. Source builds retain
the official tagged development default. Publishing a prebuilt signed Nebula
workstation image, its final digest, signature trust root, SBOM, provenance,
licenses, and update policy remains a release gate; the repository does not
invent or claim a digest before that external artifact exists.

The Kali container runs as root with a writable disposable container layer and
ordinary unrestricted outbound bridge networking. This deliberate human-only
exception can reach the public Internet and host-addressable services. It does
not receive host networking, published ports, added Linux capabilities, a host
shell, or a container-runtime socket. The runner itself must remain rootless or
inside an approved desktop VM. Only the engagement workspace is mounted at
`/workspace`; packages and system changes disappear when the session closes.
The cached baseline supplies Kali's command-line default toolset and `ping`.
Operators may install additional packages for that session with `apt`; the
derived image config keeps APT usable despite the empty runtime capability set.
Tools that require raw-packet or network-administration capabilities remain
limited by design. The derived workstation removes Nmap's packaged file
capabilities so it can start under the terminal's empty capability set and use
its unprivileged connect-scan modes instead of failing during executable
startup.

Workbench presents active Project terminals as persistent browser-style tabs.
Each tab owns an independent container, PTY, replay buffer, audit parser, and
WebSocket while sharing only the Project's `/workspace`. Core admits at most 32
pending or running human terminals globally. Tabs survive Workbench mode changes
and short webview reconnects; bulk recovery restores all active tabs in creation
order. A terminal stops on explicit **Stop**, confirmed tab close, Core shutdown,
or 30 minutes with no input and no output; a disconnected UI has a 10-minute
reconnect grace. Core keeps at most 1 MiB of sequenced output per terminal for
reconnect replay. Separately, mandatory shell framing records metadata for every
completed command as a Project-lifetime audit. The derived image contains a verified,
schema-versioned catalog of security executables from the installed Kali
baseline. Image preparation resolves the installed direct dependencies of
`kali-linux-headless`, removes Kali core plus the checked-in deployment/system
package denylist, and inventories package-owned executables in standard `PATH`
directories. Core reads that manifest back from the immutable image with
networking disabled and persists its hash, image digest, package provenance, and
sorted tool list; a missing or unsafe manifest rejects terminal preparation.
Workbench Activity lets each Project add custom executable names or
deselect image defaults. If an executed simple command matches the snapshotted
Project selection, the top-level command's merged PTY result is stored as raw and
redacted content-addressed artifacts; otherwise provisional result bytes are
discarded. Each record includes the capture decision, matched tools, policy
revision, image digest, operator, session, directory, timing, status, and exit
code. Selected capture is capped at 10 MiB while the full observed stream is
still counted and hashed. Classification uncertainty fails closed to metadata
only. Interrupted, truncated, recovered, framing-loss, classification, and
persistence-gap conditions remain visible in audit health. Existing records and
artifacts remain unchanged and are labeled as legacy full-output or metadata-only.

Authenticated clients read the catalog and Project overlay from
`GET /api/v1/engagements/{id}/terminal/recording-tools` and replace the overlay
with `PUT` using the displayed manifest digest and policy revision. A stale image
catalog or revision returns `409`; executable names containing paths, whitespace,
control characters, or shell syntax are rejected. The existing history status
update and delete routes remain immutable `409` responses.

Command metadata cannot be disabled or cleared independently of Project deletion
and is included in sensitive bundle v3 exports. Tool selection controls only
future result retention and never deletes historical artifacts. Raw result
downloads require a sensitive-data acknowledgement. Inline secrets and selected
tool output may therefore enter raw audit artifacts, while interactive password
responses are not recorded as shell commands. Terminal commands and results are
not sent to an AI provider or promoted to evidence without an explicit operator
action. Highlight terminal text and press Ctrl+C on Linux or Windows, or
Command+C on macOS, to copy it;
with no selection, Ctrl+C remains a terminal interrupt. Because an operator
controls an unrestricted root shell, the audit supplies durable attribution and
integrity checking rather than protection from a deliberately malicious root
operator.

A supported completed assistant fence (`bash`/`shell`, `sh`, or
`python`/`python3`/`py`) can also be copied or sent through an exact one-shot
review. Every run starts a new non-root container with fixed v1 limits:
1 CPU, 512 MiB RAM, 128 PIDs, 300 seconds, and independent 2,000,000-byte stdout
and stderr capture limits. The program has no interactive stdin. Only the
engagement workspace is mounted at `/workspace`; containers are never resumed.

Reviewed and agent execution remains offline by default. Scoped execution
accepts one explicit policy-approved target and selected ports, resolves and pins its addresses at
confirmation, and uses the per-invocation egress helper. Run is exposed as one
release-gated feature only when both offline and scoped paths are ready. There
is no bridge/host network mode, host shell fallback, or runtime socket exposed
to the webview. The human terminal exception above does not widen either
execution API and cannot be requested by an agent or assistant code block.

The persistent scratch workspace is limited to 5 GiB total allocated data,
50,000 entries, and 1 GiB per file. Core rejects an already-over-limit
workspace before launch and terminates an execution that crosses a limit.
These are application-enforced limits: portable bind mounts do not provide a
universal filesystem hard quota. Browser uploads use streamed atomic writes
with traversal and symlink protection. Promotion copies and verifies exact
bytes into immutable artifacts, while reset never follows symlinks and never
removes promoted evidence.

## Automation safety model

Agent automation exposes only `run_command` and `process_io` backed by one prepared Kali container per agent session. Programs are ordinary binaries on `PATH`; there is no catalog, installation, publishing, assignment, or per-program adapter layer. The runtime is non-root, read-only-root, resource-limited, mounted only to the Project workspace, and never falls back to the host. See [Automation runtime](AUTOMATION-RUNTIME.md).

The complete Project network boundary is installed at session creation but starts disabled. A `project_scope` request activates CIDR, domain, wildcard-domain, and TCP-port policy according to the Project approval setting. The policy DNS resolver blocks alternate DNS, direct bypasses, unauthorized private destinations, and rebinding responses. URL-path-only entries fail closed for arbitrary shell access.

Command stdout and stderr are immutable artifacts. Models receive compact redacted receipts and bounded retrieval capabilities. Selected MCP profiles remain separate and are frozen into durable session or run state. Human Terminal and reviewed code execution keep their distinct human-confirmed boundaries.

## Operator-workflow release verification

CI exercises the migration upgrade/downgrade cycle and immutable operation
ledger on SQLite and PostgreSQL, the raw code adapter in Linux Docker, a real
rootless Podman execution with workspace persistence, macOS Docker Desktop and
Podman Machine command/profile boundaries, the frozen-Core package audit, the
UI accessibility/visual suite, and the full v3 backend suite.

Before a release, manually smoke-test the prepared Kali agent runtime and the
official Kali terminal on Docker Desktop or a rootless Podman Machine. Confirm
Kali pull/digest resolution, root and writable ephemeral state, outbound bridge
connectivity, no added capabilities or published ports, terminal
disconnect/Core-restart cleanup, an offline run, a scoped single-target run,
cancellation cleanup, Core-restart interruption, workspace promotion/reset,
terminal catalog validation, harmless `nmap --version` selected recording beside
an unselected shell command with no artifacts, recovery/truncation warnings,
acknowledged raw-output download,
Draft note/Discuss in chat, cached PDF export, and sensitive bundle v3 export.
The release is blocked if Run appears without both reviewed-execution modes,
non-human execution can request unrestricted/root/writable settings, a runtime
socket or host terminal reaches the webview, or any runner failure falls back
to host execution.

## Current release boundary

The native desktop is the canonical user path. Scanner import, topology,
comparison, full-desktop capture, a manager for intentionally detached or hidden
terminal containers, rich HTML notes,
legacy Chroma command search, and always-on AI suggestions remain deliberately
out of the initial parity release. PostgreSQL team authorization, OIDC/RBAC,
remote workers, A2A, signed third-party plugins, and advanced specialist
environments remain separate projects.
