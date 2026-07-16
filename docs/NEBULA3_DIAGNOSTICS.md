# Nebula 3 local diagnostics

Nebula 3 writes privacy-preserving, structured diagnostics to local JSON-lines
files. Diagnostics are independent of project data, evidence, terminal audit
records, execution output, and the Nebula 2 PyQt logger.

## Levels and preferences

The saved default is **Error**. Operators can select Critical, Error, Warning,
Info, or Debug globally and can override individual feature domains under
**Settings → Diagnostics**. Debug is the most verbose level; Nebula deliberately
has no payload-oriented Trace level.

Preferences use `nebula.diagnostics-settings/v1` and are written atomically to
`diagnostics-settings.json`. The native application stores that file in its
computed application-data directory beside `logs/`; standalone Core stores it
in the selected Core data directory. Invalid preferences fail closed to Error
and produce a diagnostics error. Native Core watches desktop preference writes,
so a level change does not require a restart.

Process-only overrides take precedence without changing saved preferences:

```console
nebula-core serve --diagnostics-level debug
NEBULA_DIAGNOSTICS_LEVEL=warning nebula-core serve
```

Administration is also available without starting the server:

```console
nebula-core diagnostics status
nebula-core diagnostics set-level info
nebula-core diagnostics set-level debug --feature terminal
nebula-core diagnostics reset-levels
nebula-core diagnostics export diagnostics.zip
```

## Record and correlation contract

Each line is one `nebula.diagnostic/v1` record with a UTC timestamp, monotonic
process sequence, level, feature, source, stable event code, safe message,
application version, and launch ID. Records add request, operation, parent
operation, error, project, run, execution, and session IDs when available.
Failure records can also carry a safe cause, stage, outcome, duration,
retryability, exception class/chain, and stack locations without local values.

The interface creates an operation ID for API actions. Core creates and returns
an `X-Request-ID`, preserves both IDs across streamed work, and gives supervised
background work a correlated child operation ID. Every Error or Critical record
has an `error_id`. The same ID appears in the feature file, `errors.log`, API or
stream error envelope, and the interface reference shown to the operator.

## Files and ownership

One file is maintained for each domain:

| File | Domain |
| --- | --- |
| `desktop.log` | Native lifecycle, sidecar, menus, updater, panic, and cleanup |
| `interface.log` | React lifecycle, routing, settings, dialogs, render and action failures |
| `api.log` | Authentication, REST, validation, static serving, WebSocket and SSE transport |
| `setup.log` | Scratch setup, runner discovery/selection, image preparation and readiness |
| `storage.log` | Database, migrations, transactions, artifacts, import, bundles and cleanup |
| `projects.log` | Projects, operator profiles, scope, inventory, correlations and CRUD |
| `terminal.log` | Human terminal/container/PTY lifecycle and connection summaries |
| `terminal-audit.log` | Shell framing, selective capture, spool recovery and audit integrity |
| `workspace.log` | Listing, preview, transfer, quotas, traversal rejection and cleanup |
| `notes.log` | Observation lifecycle, links, AI-draft decisions and revision conflicts |
| `capture.log` | Viewport capture, editor operations, lineage, encoding and immutable saves |
| `providers.log` | Provider discovery, health, capabilities, adapters, credentials and consent |
| `chat.log` | Conversation/message lifecycle, streaming, approvals and handoffs |
| `knowledge.log` | Parsing, ingestion, indexing, retrieval, citations and compaction |
| `harnesses.log` | Codex/Claude processes, protocols, sessions, MCP and cleanup |
| `missions.log` | Missions, tasks, attempts, budgets, approvals, retries and terminalization |
| `runtime.log` | Kali preparation, inventory, sessions, commands, processes and policy grants |
| `sandbox.log` | Runners, images, containers, DNS/egress policy, limits and cleanup |
| `executions.log` | Reviewed preflight, execution stream summaries, results and generated drafts |
| `findings.log` | Finding lifecycle, validation, evidence, transitions and report assignment |
| `evidence.log` | Content addressing, provenance, lineage, integrity and sensitive acknowledgement |
| `reports.log` | Report lifecycle, sign-off, PDF rendering, artifacts and sensitive export |
| `diagnostics.log` | Logger settings, queues, rotation, pruning, viewer, export and health |
| `errors.log` | Exact aggregate copy of every Error and Critical record |

In a native launch, Tauri owns `desktop.log`, `interface.log`, and `errors.log`.
Core owns the remaining files and sends only complete, validated Error
and Critical frames over the supervised stderr channel for aggregation. In a
headless launch, Core owns every file. `nebula-core-startup.log` is a separate,
bounded emergency capture used before normal logging is ready.

Current files rotate at 5 MiB with two retained generations. Rotations older
than 14 days are removed, and the log directory is capped at 256 MiB. Files are
pre-created for the current user only. Error/Critical appends are synchronously
flushed; lower levels use a bounded queue. Queue pressure never drops an Error
and emits a rate-limited `diagnostics.records_dropped` failure.

## Privacy boundary

Metadata is recursively key-allowlisted before queueing and sanitized again on
export. Diagnostics never contain request headers or bodies, credentials,
tokens, prompts/responses, source code, commands/arguments, stdout/stderr,
terminal bytes, selected text, document contents, screenshots, evidence bytes,
SQL parameters, or sensitive filenames. Streams record lifecycle, counts,
durations, truncation and sequence gaps—not individual frames or deltas.
Python and dependency warnings pass through a message-free adapter that records
only their safe category; raw warning text and source paths are not copied into
diagnostics.

The diagnostics ZIP includes unredacted current/rotated logs, emergency startup
logs when present, build/platform metadata, active settings, logger health, and
a SHA-256 manifest. It excludes databases, workspaces, artifacts, evidence,
terminal audit results, provider configuration, and credentials. Nebula never
uploads it automatically.

## Operator workflow and health

Use **Settings → Diagnostics** to see live Core, Terminal-runtime, and diagnostic
storage checks before reviewing historical failures. A retained failure does not
mean that the subsystem is still unhealthy; only the **Current status** checks
describe present health. Failure cards lead with the affected subsystem and
operation, sanitized cause, retry classification, and a verified Nebula
destination. Event codes, exception chains, sanitized stack locations, metadata,
and correlation references remain available under **Technical details**.

Links containing `?diagnostic=err_...` or `?diagnostic=req_...` select, expand,
and focus the retained matching failure. If rotation has removed the record, the
viewer says that the reference is no longer present instead of showing unrelated
guidance. Settings, health, files, and failures load independently, so one
unavailable source does not hide the remaining local evidence.

The advanced section can configure logging detail, open the computed log
directory, or confirm and export a support bundle. The open-folder command reveals
only Nebula's computed directory; the webview cannot request an arbitrary path.
When Core is unavailable, the native viewer still reads desktop-owned errors.

`nebula-core doctor --json` and `/api/v1/health` report logger writability,
active levels, disk use, last rotation, dropped-record count, and degraded
state. If normal storage becomes unavailable, bounded errors go to the
emergency supervised sink, remain in memory for the viewer, and the interface
shows a persistent diagnostics-unavailable warning.

The authenticated API exposes settings, file inventory, recent errors,
development-browser event ingress, and local export at `/api/v1/diagnostics/*`.
Browser ingress is bounded and enabled only in browser-development mode.

## Enforcement

CI runs Python AST, TypeScript compiler-API, and Rust/clippy checks. A catch,
Promise rejection handler, background task, or ignored Rust result must log,
rethrow, or carry a reviewed `diagnostic-expected:` control-flow annotation.
The release gate requires zero unclassified failure paths.
