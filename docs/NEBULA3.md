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

## Run the Core

Nebula is packaged as one application and required imports fail immediately if
the installation is incomplete.

```bash
poetry install --without legacy,legacy-dev --with dev
poetry run nebula3 doctor
poetry run nebula3 migrate
poetry run nebula3 serve --host 127.0.0.1 --port 8000
```

The server prints a generated bearer token. Remote binding requires an explicit
`--allow-remote` acknowledgement and should be placed behind a properly
authenticated deployment boundary. Local mode never exposes a runner socket.

Build and launch the browser workspace:

```bash
npm --prefix ui ci
npm --prefix ui run build
poetry run nebula3 ui
```

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

## Tool safety model

Operator setup, installation locations, CLI/API examples, extension authoring,
and the truthful Safe Foundation release status are documented in the
[tool-pack guide](TOOL_PACKS.md).

Executable tools are disabled unless all of these are present:

1. A typed `ToolSpec` with closed JSON schemas and trusted target/path mappings.
2. An engagement-owned workspace and in-scope, broker-resolved target.
3. A current mission budget reservation.
4. Any approval required by the risk class.
5. An approved rootless Docker/Podman worker and preconfigured egress boundary.
6. A digest-pinned tool image already present locally (`--pull=never`).
7. An immutable evidence recorder.

Missing isolation results in analysis-only mode. There is no host execution
fallback.

The Safe Foundation manifests currently bundled with this repository are
unresolved release source, not installable signed packs. They contain digest
placeholders and have no published SBOM/provenance payloads or trust key. Do not
substitute example digests to enable them.

## Current release boundary

This developer preview is the Phase 0/1 foundation plus a connected Phase 2 UI
shell. PostgreSQL team authorization, OIDC/RBAC, remote workers, full scanner
normalization, production report rendering, generated-client drift enforcement,
MCP/A2A, signed plugins, and advanced specialist packs remain release-gated.

Nebula 2 remains a separately triggered legacy distribution. Its PyQt licensing
review does not apply to Nebula 3 installers because the legacy dependency and
test groups are absent from the freezer environment and a binary-content gate
rejects legacy GUI modules.
