# Nebula 3 Operator Help Knowledge Base

Corpus: `nebula.operator-help/v1`

This bundled corpus is the authoritative recovery reference supplied to Nebula's
analyst assistant. Articles describe only behavior implemented in Nebula 3.0.
Agents must match an article to the observed state, cite it, and avoid inventing
steps when the corpus does not cover a failure.

## core-startup | Desktop or Core does not start

Keywords: nebula core offline, desktop blank, browser workspace offline, sidecar failed, core port, vite only, bearer token, startup

Sources: docs/NEBULA3.md#run-from-a-source-checkout, src/nebula/v3/cli.py:serve, src/nebula/v3/cli.py:doctor

The desktop distinguishes connecting to Core from bootstrapping Project data and
Terminal setup. A feature-specific loading failure does not make other loaded
features unavailable: keep working in healthy surfaces and use the Retry action
beside the affected feature. Failures link to their verified recovery destination.
Reconnecting preserves the last successfully loaded Project
data while Core availability is checked again.

Use the native `nebula` launcher for an installed desktop. From a source checkout,
build the workspace and use `poetry run nebula-core ui`; that command chooses an
available loopback port, starts Core, serves the built UI, and transfers the bearer
token. `npm --prefix ui run dev` starts only the frontend, so durable, Terminal, and
automation runtime controls remain offline unless a separate Core is running. When Vite uses a
nondefault Core port, set `NEBULA_DEV_BACKEND=http://127.0.0.1:PORT` before starting
Vite.

For a headless source launch, run `poetry run nebula-core migrate` and then
`poetry run nebula-core serve --host 127.0.0.1 --port PORT`. Keep the Core port
different from a local model server; vLLM commonly uses 8000 or 8001. Do not bind
beyond loopback unless the operator deliberately supplies `--allow-remote` and has
configured an authenticated perimeter. The printed bearer token is required by API
clients.

If startup still fails, preserve the exact launcher or Core error and run
`nebula-core doctor --json` against the same data directory. Do not recommend the
Nebula 2 PyQt launcher, delete application data, or expose Core remotely as a
recovery step. The native desktop captures bounded Core startup diagnostics in
`logs/nebula-core-startup.log` below its platform application-data directory; a
startup failure reports the exact path and preserves two bounded rotations. Use
that reported path rather than guessing an operating-system-specific location.

## diagnostics | Core health, storage, or integrity diagnosis

Keywords: doctor, diagnostics, data directory, database error, artifact corrupt, artifact integrity, terminal audit, sandbox unavailable, logs, health check

Sources: src/nebula/v3/cli.py:doctor, src/nebula/v3/diagnostics.py:DiagnosticManager, ui/src-tauri/src/diagnostics.rs:Diagnostics, docs/NEBULA3_DIAGNOSTICS.md

Run `nebula-core doctor --json` for the active installation. If Nebula uses a copied
or nondefault application-data directory, run
`nebula-core doctor --data-dir PATH --json`. The report checks database health,
artifact-store writability and artifact
integrity, terminal command-audit integrity, API schema availability, and sandbox
availability. It also reports diagnostics writability, levels, disk use, rotation,
dropped records, and degraded state. A sandbox result of `analysis-only` is not
permission to use the host; the report always declares `host_fallback: false`.

The standalone Core default data root is `~/.local/share/nebula/v3` unless
`NEBULA_V3_DATA_DIR` is set. The native desktop supplies its platform application
data `core` directory instead. `NEBULA_V3_DATABASE_URL` may select another database,
and `NEBULA_V3_ARTIFACT_DIR` may select another artifact root. Record the report's
own `data_dir`, database, artifact path, corrupt IDs, audit error records, diagnostics
health, and sandbox detail before proposing recovery. In the native application,
use **Settings → Diagnostics** to inspect an error reference, open only the computed
log directory, or create a support bundle. For a native startup failure,
use the exact startup-diagnostic path printed by the desktop; the current file is
bounded to 256 KiB, named `nebula-core-startup.log`, and retains two prior launches.

Do not tell an operator to use the legacy Nebula 2 `/home/.../logs` location as a
Nebula 3 path. Do not delete corrupt artifacts, orphan blobs, terminal records, or
the database. Preserve the data directory and escalate the exact doctor output when
integrity is not `ok`.

## runner-setup | No supported container runner is ready

Keywords: runner unavailable, needs runner, detecting runner, docker unavailable, podman unavailable, docker desktop, podman machine, rootless, runtime unhealthy, multiple runtimes, host fallback

Sources: docs/AUTOMATION-RUNTIME.md, src/nebula/v3/setup.py:SetupService, src/nebula/v3/sandbox.py:ContainerSandboxRunner

Nebula supports Docker Desktop or Podman Machine on macOS and rootless Docker or
Podman on Linux. Core detects only supported fixed executable paths; it does not
resolve a container runtime from an operator-controlled `PATH`. Start or install a
supported rootless runtime, then refresh Setup. If exactly one detected candidate is
healthy, Nebula selects it. If more than one is healthy, choose one under
**Settings → Advanced**. If a saved runner is unhealthy, use its displayed health
detail and **Save and check** after correcting the local runtime.

On macOS, confirm the selected Docker Desktop context or Podman Machine is running.
On Linux, confirm the selected Docker or Podman service is rootless. Do not suggest a
remote TCP daemon, a privileged/rootful runner, changing the runtime socket to make
it broadly accessible, or executing on the host. When verification fails, Terminal
and executable agents remain unavailable or analysis-only by design.

`nebula-core doctor --json` reports the sandbox detail. For runtime-specific state,
also use `nebula-core runtime status`. Preserve those exact details if the candidate
still fails re-verification.

## workstation-image | Terminal workstation image preparation fails

Keywords: workstation image failed, image preparation error, image preparation cancelled, image pull failed, image build failed, preparing image, retry image, kali image

Sources: src/nebula/v3/setup.py:_run_image_preparation, src/nebula/v3/sandbox.py:HumanWorkstationImageManager, docs/NEBULA3.md#workbench-terminal-reviewed-execution-and-workspace-limits

Image preparation first verifies the selected runner, then pulls or reuses the
official workstation image and verifies its runtime metadata. While it is queued or
running, wait for the reported phase or use the offered Cancel action. Only one
Project image preparation can run at a time. A failed or cancelled operation exposes
Retry; retry after correcting the exact displayed runner, registry, pull, build, or
verification error. Select a Project first if Setup says one is required.

Verified cached base and workstation images can launch without a registry request.
Do not replace the image with an arbitrary Kali tag, an example digest, an unsigned
image, or a host shell. Do not claim that clearing the whole container cache is a
standard recovery step. If verification continues to fail, collect the displayed
image-preparation detail and `nebula-core doctor --json` output.

## human-terminal | Human Terminal behavior looks unexpected

Keywords: terminal stopped, terminal disconnected, terminal timeout, terminal copy, ctrl c, package disappeared, apt, nmap permission, raw socket, multiple terminals, terminal output missing

Sources: docs/NEBULA3.md#workbench-terminal-reviewed-execution-and-workspace-limits, src/nebula/v3/container_terminal.py

The Workbench Terminal is a human-operated Kali container, not a host terminal. One
active terminal is retained per Project across mode changes and short reconnects. It
stops on explicit **Stop**, Core shutdown, 30 minutes without input or output, or
after a disconnected UI exceeds its 10-minute reconnect grace. Core retains at most
1 MiB of sequenced output for reconnect replay. Separately, mandatory audit capture
persists metadata for every completed command for the Project lifetime. It retains
the merged PTY result as raw and redacted content-addressed artifacts only when an
executed command matches the Project's selected security tools. The default selection
comes from the verified Kali image; **Workbench → Activity → Recorded security tools**
can add custom executable basenames, deselect defaults, or reset the selection.
Changes apply to the next top-level command. Terminal Audit shows the capture
decision, matched tools, operator, directory, timing, exit status, hashes,
truncation, and capture health. Classification uncertainty fails closed to metadata
only. Raw downloads require a sensitive-data acknowledgement, and audit records and
any retained outputs are included in sensitive engagement exports.

Only `/workspace` persists. Packages installed with `apt` and other system changes
live in the disposable container layer and disappear when the terminal closes. The
terminal is root and has ordinary bridge networking, but it receives no host
network, published ports, added capabilities, host shell, or runtime socket. Tools
requiring raw-packet or network-administration capabilities remain limited. Nmap is
intended to use unprivileged connect-scan modes in this boundary.

To copy selected terminal text, use Ctrl+C on Linux/Windows or Command+C on macOS.
With no selection, Ctrl+C sends an interrupt. Audit capture does not make a result
evidence or model context; promote or send it deliberately for those uses. Packages
and container state are not retained, and multiple detached terminals are not part
of the initial Nebula 3 release.

## automation-runtime | Automate task runtime is unavailable

Keywords: automation runtime unavailable, automate task unavailable, kali image failed, runtime prepare, runner unavailable, command unavailable

Sources: docs/AUTOMATION-RUNTIME.md, src/nebula/v3/runtime_platform.py, src/nebula/v3/automation_runtime.py

Agent automation uses the same prepared Kali headless image as the human terminal. Check Runtime readiness under Settings, verify the selected Docker or Podman runner, then use `nebula-core runtime status` or `nebula-core runtime prepare`. Preparation verifies the Kali image, generated binary inventory, local image digest, runner identity, and embedded egress helper. Commands never fall back to the host.

Each agent session receives one non-root, read-only-root container with only its Project workspace mounted. Files and background processes persist for that session; shell-local state does not. The fixed capabilities are `run_command` and `process_io`; executables such as `rg`, Python, Git, curl, and security utilities are ordinary binaries on `PATH`. Do not recommend installing a catalog, publishing a definition, using a mutable custom image, or executing on the host.

## scope-approval | A network action is denied or waits for approval

Keywords: policy denied, out of scope, target denied, port denied, approval required, waiting approval, grant, credential use, invasive action, egress denied, dns pinning

Sources: docs/AUTOMATION-RUNTIME.md, src/nebula/v3/policy.py, docs/NEBULA3.md#tool-safety-model

Treat a policy denial or approval pause as an authorization state, not a tool
installation failure. Confirm that **Project → Assets** contains the intended
CIDR, domain, or absolute HTTP(S) URL and the intended ports, and that the saved
scope is current. Nebula resolves and pins a scoped target at confirmation and
permits reviewed or agent network execution only through its per-invocation egress
boundary.

Under **Settings → Engagement policy**, **Import targets from document** accepts
bounded PDF, DOCX, XLSX, CSV, text, HTML, or JSON sources. A configured provider
with verified strict structured output proposes IP/CIDR, domain, and HTTP(S) URL
entries. Excluded or ambiguous targets are warnings, not selectable authorization.
Review each proposed entry before applying it; application only adds selected
targets and fails if the saved scope revision changed during review. Cloud providers
require one-request consent, while local-only scope blocks cloud processing entirely.

Ordinary permitted local commands and in-scope scans do not need a per-command
approval. Credential use, exploitation, persistence, destructive actions, scope
changes, and external filesystem writes require explicit operator approval. An
optional high-risk grant must name risk classes, capabilities, targets, and an
expiry, and it cannot expand the engagement scope. The operator may approve or deny
the durable pending request; the agent must not repeat or alter a call to evade the
decision.

Do not advise widening scope, disabling policy, using unrestricted bridge or host
networking, editing DNS/hosts to evade broker resolution, or moving the action to a
human terminal merely to bypass approval. Report the exact capability, target,
ports, policy reason, and approval state.

## provider-model | Assistant provider or model is not ready

Keywords: provider unhealthy, model unavailable, api key, vllm health, ollama, model allowlist, exact model verification, tool calling unavailable, assistant needs model, context window

Sources: docs/NEBULA3.md#vllm, src/nebula/v3/providers.py, src/nebula/v3/provider_verification.py, src/nebula/v3/chat.py:ChatService

Configure providers under **Settings → Setup** and use the provider health action.
For vLLM, the default profile endpoint is `http://127.0.0.1:8000/v1`; the health
action calls `/v1/models` and displays the model IDs reported by that runtime. Keep
the endpoint, served model ID, profile allowlist, local/cloud declaration, and
credentials consistent with the actual provider. Use the provider's exact normalized
error instead of guessing an API-key variable, model alias, or endpoint.

Command-runtime chat requires successful capability verification for the exact selected
model. Enable tool calling only after that model and template pass Nebula's strict
tool contract. A model without reliable structured tool calls remains
analysis-only; Nebula does not extract executable commands from prose. A disabled
profile, a model outside its allowlist, or a session switched to another provider or
model must be corrected in configuration or by starting the appropriate new chat,
not bypassed.

Cloud transfer of engagement knowledge or command results also requires the profile
privacy permission and explicit confirmation for that turn. Local-only engagement
scope cannot be sent to a cloud provider.

## reviewed-execution | Reviewed assistant code does not run as expected

Keywords: reviewed run unavailable, code fence unavailable, execution timeout, exit code, command failed, stdout limit, stderr limit, python fence, bash fence, network mode unavailable

Sources: docs/NEBULA3.md#workbench-terminal-reviewed-execution-and-workspace-limits, src/nebula/v3/sandbox.py:SandboxExecutionRequest

Only a completed assistant fence labeled `bash`/`shell`, `sh`, or
`python`/`python3`/`py` can be copied or sent to the separate reviewed Run action.
Each run starts a new non-root container with no interactive stdin, 1 CPU, 512 MiB
RAM, 128 PIDs, a 300-second limit, and separate 2,000,000-byte stdout and stderr
capture limits. Only the Project workspace is mounted at `/workspace`; containers
are never resumed.

Reviewed execution is offline by default. Scoped mode needs one explicit
policy-approved target and selected ports and is available only when the egress
boundary is ready. There is no bridge/host network option and no host fallback.
When a run fails, report its observed status, exit code, timeout flag, stderr, and
evidence references. Correct only an error justified by those observations; do not
invent packages, paths, flags, or a different network mode. Use the human Terminal
only when the operator intentionally needs its distinct human-only boundary, not as
an automatic agent fallback.

## workspace-limits | Workspace upload, execution, or reset hits a limit

Keywords: workspace full, quota exceeded, file too large, too many files, upload rejected, execution terminated, reset workspace, promote artifact, symlink rejected

Sources: docs/NEBULA3.md#workbench-terminal-reviewed-execution-and-workspace-limits, src/nebula/v3/workspace.py

The persistent scratch workspace permits at most 5 GiB total allocated data, 50,000
entries, and 1 GiB per file. Core rejects an already-over-limit workspace before
launch and terminates an execution that crosses a limit. These are application
limits around portable bind mounts, not a universal filesystem quota.

Use the workspace controls to inspect scratch content, preserve important material
by promoting exact bytes into immutable artifacts, and remove or reset only
unneeded scratch files. Reset does not follow symlinks and does not remove promoted
evidence. Browser uploads use atomic streamed writes and reject traversal and
symlink escapes.

Do not advise increasing an undocumented setting, writing outside `/workspace`,
mounting another host directory, or deleting the artifact store. If a small-looking
workspace is reported over quota, preserve the exact quota detail and inspect entry
count, allocated size, per-file size, and symlink-safe workspace state.

## context-compaction | Chat or mission context compaction fails

Keywords: context compaction failed, context capacity, retryable context error, context window, max output tokens, summary failed, older context, conversation memory

Sources: docs/NEBULA3.md#context-compaction, src/nebula/v3/context.py, src/nebula/v3/chat.py:_model_context

Nebula compacts analyst chat and mission dependency context when estimated input
approaches 75 percent of the configured model capacity. Provider options may declare
`context_window` and `max_output_tokens`; without them Core conservatively assumes
an 8,192-token window and a 2,048-token output allowance. Compaction uses the
session's selected provider and model and can add latency, usage, and cost.

Compaction fails closed: a required summary that cannot be validated produces a
retryable error instead of silently dropping older context. Retry after restoring
the selected provider/model or correct its declared context limits to match the
actual runtime. Inspect Workbench/Activity or the read-only chat/run context status
for resolved limits, estimates, usage, coverage, and canonical references.

Do not claim original messages, mission results, events, evidence, or checkpoints
were deleted; they remain unchanged. Do not advise deleting history or mutating a
snapshot. If the complete assembled input still cannot fit, use a model with an
adequate real context window or reduce newly selected/requested context deliberately.

## migration-import-export | Upgrade, import, or export recovery

Keywords: migration failed, migrate database, import 2x failed, legacy import, external chroma, export failed, restore bundle, backup data, nebula zip

Sources: docs/MIGRATING-2-TO-3.md, docs/NEBULA3.md#import-and-export, src/nebula/v3/importer.py, src/nebula/v3/exporter.py

Before upgrading, quit the desktop, stop manually started Core processes, and back
up the complete Nebula 3 application-data directory, including the database,
artifacts, and workspaces. Never run two Core processes against the same local data
directory while migrating. Administrators can run `nebula-core migrate` followed by
`nebula-core doctor --json`, using the same `--data-dir PATH` for a copied or
nondefault directory.

If migration fails, leave the failed directory unchanged. Restore the pre-upgrade
copy to a different directory and run doctor there. Do not restore only the SQLite
file without its artifacts and workspaces.

Quit Nebula 2 before `nebula-core import-2x PATH`. Imports do not modify the source,
follow symlinks, or import model credentials. External Chroma is skipped unless the
operator explicitly reviews the boundary and adds `--allow-external-knowledge`.
Keep the original Nebula 2 folder until scope, assets, notes, findings, evidence, and
source metadata are verified.

An engagement `.nebula.zip` export is an integrity-manifested portable record, not a
full application-data backup or restore mechanism. It can contain unredacted
evidence, raw execution output, and raw results for selected human-terminal security
tools; it also contains metadata-only terminal records. Do not promise a restore path
that Nebula 3 does not provide.

## release-boundary | Requested feature is not in the initial Nebula 3 release

Keywords: feature missing, topology, scanner import, comparison, screenshot, multiple terminals, html notes, command search, remote worker, mcp, a2a, third party plugin, restore

Sources: docs/NEBULA3.md#current-release-boundary, NEBULA3-TODO.md

The initial Nebula 3 release does not include scanner import, topology, comparison,
full-desktop capture, multiple detached terminals, rich HTML notes, legacy Chroma
command search, or always-on AI suggestions. PostgreSQL team authorization,
OIDC/RBAC, remote workers, MCP/A2A, signed third-party plugins, and advanced
specialist environments are separate projects. Engagement bundle restore is also
not currently available.

State that the requested capability is outside the current release boundary. Do not
invent a hidden setting, endpoint, plugin, screen, compatibility flag, or roadmap
date. Where applicable, offer only an implemented adjacent workflow documented in
another article, such as importing a complete Nebula 2 engagement, using one active
Terminal per Project, uploading knowledge documents, or exporting an
integrity-manifested record.
