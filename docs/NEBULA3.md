# Nebula 3 developer preview

Nebula 3 is implemented alongside the PyQt maintenance application. It is a
local-first control plane and React/Tauri workspace; it is not yet a production
team release.

## What is available

- UI-independent Pydantic domain contracts and SQLAlchemy storage.
- SQLite WAL locally and a PostgreSQL-compatible storage URL.
- Append-only, monotonically sequenced run events with authenticated replay.
- SHA-256 content-addressed artifacts and immutable execution evidence.
- Side-by-side Nebula 2.x import with source manifests and rollback.
- Versioned REST/OpenAPI resources and authenticated WebSockets.
- Provider-neutral OpenAI Responses, Anthropic, Gemini, Bedrock, and
  OpenAI-compatible adapters.
- Explicit catalog profiles for commercial gateways and local runtimes,
  including Ollama, **vLLM**, llama.cpp, SGLang, LM Studio, Hugging Face
  endpoints, and NVIDIA NIM.
- LangGraph supervisor/specialist missions with durable checkpoints, approval
  pauses, independent evidence verification, retries, and hard budgets.
- Typed tool plugins, strict JSON schemas, engagement-owned workspaces,
  broker-owned DNS resolution, scope enforcement, and rootless OCI execution.
- React/TypeScript workspace and Tauri shell with a loopback-only sidecar token
  handshake.
- A human-operated Kali terminal plus reviewed assistant code execution. The
  human terminal has an explicit root/writable/unrestricted boundary; reviewed
  and agent execution remains offline or single-target scoped.
- Deterministic server-rendered PDF reports, operator-triggered AI execution
  notes, and integrity-manifested engagement bundle v2 export.

## Run the Core

Nebula is packaged as one application and required imports fail immediately if
the installation is incomplete.

```bash
poetry install --without legacy,legacy-dev --with dev
poetry run nebula3 doctor
poetry run nebula3 migrate
poetry run nebula3 serve --host 127.0.0.1 --port 8765
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
poetry run nebula3 ui
```

`nebula3 ui` is the recommended source-checkout launch path. It chooses an
available loopback port, starts Core, serves the built workspace, and transfers
the generated bearer token through the URL fragment. Running Vite alone starts
only the frontend and leaves all durable and Toolbox controls offline.

The browser token is carried in the URL fragment, consumed into memory, and
removed immediately. Tauri sends its 256-bit one-time token through the Core
process's stdin instead of a URL or process argument.

## Desktop installers

End users install one native application; they do not install Python, Poetry,
Node, npm, Rust, Cargo, or a compiler. The Tauri bundle contains the browser
workspace and a sibling `nebula-core` one-file executable with Python 3.12,
migrations, notices, and all mandatory Core dependencies. Nebula 2 and PyQt are
structurally excluded from this build.

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
poetry run nebula3 run ENGAGEMENT_ID "Review the bounded scope" \
  --provider PROVIDER_PROFILE_ID \
  --model SERVED_MODEL_ID \
  --max-tool-calls 0
```

Model-backed specialists receive no executable tools in this path. The run
ledger records the provider, model, request provenance, token usage, and the
analysis result; tool execution remains exclusively behind the policy broker.

## Import and export

```bash
poetry run nebula3 import-2x /path/to/legacy-engagement
poetry run nebula3 export ENGAGEMENT_ID engagement.nebula.zip
```

Import records before/after checksums and does not write to the source folder.
An external Chroma directory is skipped unless the operator supplies
`--allow-external-knowledge` explicitly.

The report workspace exports a saved report revision as a server-rendered PDF.
The separate **Export engagement bundle (.nebula.zip)** action produces bundle
format v2 with entity records, run and operation events, execution streams,
generated drafts, report snapshots/PDFs, and their content-addressed artifacts.
Bundles may contain unredacted evidence and raw execution output and are not
described as backups because Nebula 3 does not yet provide a restore path.
Scratch workspace files are excluded unless an operator promoted them to an
artifact.

## Context compaction

Analyst chats and model-facing mission dependency context are compacted
automatically when the estimated input approaches 75 percent of the configured
model capacity. Provider profiles may declare `context_window` and
`max_output_tokens` in their options; Core conservatively assumes an 8,192-token
window and a 2,048-token output allowance when no limits are configured.

Compaction uses the conversation or mission's selected provider and model, so it
can add model latency, token usage, and cost. The Sessions and Missions
workspaces show the latest compaction status and usage. Compaction fails closed:
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

## Reviewed code execution and workspace limits

Nebula does not provide a host terminal. The Sessions workspace provides a
human-operated **Terminal** in the official minimal
`docker.io/kalilinux/kali-rolling:latest` image. On first use after each Core
start, Core asks the selected verified runner to pull that tag, verifies the
repository and platform, and resolves an immutable repository digest. Core then
builds or reuses a locally cached image from that verified base with
`kali-linux-headless` and `iputils-ping` installed. The build recipe, official
base digest, and derived content-addressed image ID are verified and recorded;
every session launches the derived image ID with `--pull=never`.

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
limited by design.

The named container is removed when the WebSocket disconnects, the operator
stops it, or Core shuts down. Every terminal retains the existing 1 CPU, 512 MiB
RAM, 128 PID, 30-minute hard, 15-minute I/O idle, and workspace limits.
Interactive input/output is not sent to an AI provider or automatically
promoted to evidence.

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
universal filesystem hard quota. The browser is read-only; promotion copies
and verifies exact bytes into immutable artifacts, while reset never follows
symlinks and never removes promoted evidence.

## Tool safety model

Operator setup, installation locations, CLI/API examples, extension authoring,
and release status are documented in the [Toolbox guide](TOOLBOX.md).

Executable tools are disabled unless all of these are present:

1. A typed `ToolSpec` with closed JSON schemas and trusted target/path mappings.
2. An engagement-owned workspace and in-scope, broker-resolved target.
3. A current mission budget reservation.
4. Any approval required for invasive risk classes.
5. An approved rootless Docker/Podman worker and preconfigured egress boundary.
6. A digest-pinned tool image already present locally (`--pull=never`).
7. An immutable evidence recorder.

Missing isolation results in analysis-only mode. There is no host execution
fallback.

The Toolbox source retains release digest placeholders intentionally. The
protected `nebula-toolbox-v*` publisher resolves them from actual registry
outputs, creates SBOM/provenance evidence and OCI signatures, and publishes an
Ed25519-signed catalog. Nebula embeds only the Beryllium public trust key; never
substitute example digests or commit the private release key.

## Operator-workflow release verification

CI exercises the migration upgrade/downgrade cycle and immutable operation
ledger on SQLite and PostgreSQL, the raw code adapter in Linux Docker, a real
rootless Podman execution with workspace persistence, macOS Docker Desktop and
Podman Machine command/profile boundaries, the frozen-Core package audit, the
UI accessibility/visual suite, and the full v3 backend suite.

Before a release, manually smoke-test the signed digest-pinned Toolbox and the
official Kali terminal on Docker Desktop or a rootless Podman Machine. Confirm
Kali pull/digest resolution, root and writable ephemeral state, outbound bridge
connectivity, no added capabilities or published ports, terminal
disconnect/Core-restart cleanup, an offline run, a scoped single-target run,
cancellation cleanup, Core-restart interruption, workspace promotion/reset, raw-output warning,
Draft note/Discuss in chat, cached PDF export, and sensitive bundle v2 export.
The release is blocked if Run appears without both reviewed-execution modes,
non-human execution can request unrestricted/root/writable settings, a runtime
socket or host terminal reaches the webview, or any runner failure falls back
to host execution.

## Current release boundary

This developer preview is the Phase 0/1 foundation plus a connected Phase 2 UI
shell. PostgreSQL team authorization, OIDC/RBAC, remote workers, full scanner
normalization, generated-client drift enforcement, report signing/design tools,
MCP/A2A, signed plugins, and advanced specialist environments remain release-gated.

Nebula 2 remains a separately triggered legacy distribution. Its PyQt licensing
review does not apply to Nebula 3 installers because the legacy dependency and
test groups are absent from the freezer environment and a binary-content gate
rejects legacy GUI modules.
