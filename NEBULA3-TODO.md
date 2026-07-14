# Nebula 3 release readiness and parity backlog

This file tracks only work that remains release-gated after the zero-setup
Workbench and Nebula 2 parity implementation. It replaces the obsolete phased
plan for code rendering, reviewed execution, notes, and PDF export; those
capabilities already have implementation and tests and must not be rebuilt.

## Release objective

A user with a supported Docker or Podman installation can open the native
Nebula application and receive a focused terminal without configuring a model,
runner profile, Project, or Toolbox. Safety, evidence, policy, approval, and
provenance internals remain enforced and are progressively disclosed.

The release is blocked until all of these gates pass:

- [ ] A clean install with a cached workstation image reaches a focused prompt
      within 10 seconds on supported macOS arm64/x64 and Linux x64 systems.
- [ ] Cold image preparation reports progress immediately and supports cancel,
      retry, and restart; a cached launch performs no registry request.
- [ ] The workstation image is release-pinned and signed, with verified digest,
      SBOM, provenance, licenses, platform metadata, and an update policy.
- [ ] Fixed-path Docker/Podman detection passes the healthy, multiple, missing,
      stopped, rootful, remote, wrong-platform, and invalid-executable cases.
- [ ] Production startup contains no demo entities and distinguishes starting,
      bootstrapping, ready, degraded, and failed states truthfully.
- [ ] Fresh, upgraded, imported, concurrently opened, and intentionally pruned
      databases create exactly zero or one Scratch Project as specified.
- [ ] A terminal survives Workbench mode changes and reconnects, obeys idle and
      disconnect grace periods, and completes 100 lifecycle cycles without a
      leaked container or workspace lock.
- [ ] Tests prove terminal output is not persisted to SQLite, logs, evidence,
      exports, or model requests without an explicit operator action.
- [ ] Credential tests scan databases, logs, exports, process arguments, and
      browser storage and verify OS-vault restart and session-only fallback.
- [ ] Selection, terminal capture, image annotation/redaction, workspace upload,
      Notes, and commands-only history meet their security and accessibility
      test matrices.
- [ ] Finding → evidence → validation → report → sign-off → PDF passes end to
      end, including revision conflicts and attribution requested only when
      needed.
- [ ] Native installer smoke tests prove `nebula` launches the desktop and
      `nebula-core` performs administration on macOS and Linux.
- [ ] Existing policy, isolation, approval, evidence, provenance, migration,
      package-boundary, accessibility, and visual-regression suites remain green.

## Essential first-release workflow

These items belong in the first parity release and must remain integrated rather
than hidden behind separate top-level destinations:

- Workbench with one persistent terminal per Project and an optional Assistant
  split; Files, Notes, and Activity are secondary surfaces.
- Five primary destinations: Workbench, Findings, Reports, Project, Settings.
- Optional contextual assistant setup with secure credential references and a
  non-generating discovery/liveness check.
- Select → Ask Nebula/Add Note/Copy, with reviewed Run only for code and terminal
  selections; context is bounded, editable, draft-only, and provenance-linked.
- Visible terminal-viewport PNG capture plus a Canvas image editor with crop,
  annotation, blur, solid redaction, undo/redo, and immutable derived lineage.
- Streamed atomic workspace uploads, Markdown observations, evidence promotion,
  and explicit “Use with Assistant” actions.
- Shell-integration command history that stores commands and metadata, never
  output, and is excluded from evidence, export, and model context by default.
- In-place finding actions and revision-aware report sign-off.

## Deliberately deferred

Do not expose placeholder or disabled controls for these capabilities. Track
and deliver each as a separate, tested project:

- Scanner import and normalization.
- Topology and comparison views.
- Full desktop, window, or region capture requiring native permissions.
- Multiple detached terminals per Project.
- Rich HTML notes and the legacy rich-text toolbar.
- Legacy Chroma command search.
- Background AI file watchers and always-on AI suggestions.
- PostgreSQL team authorization, OIDC/RBAC, and remote workers.
- MCP/A2A, signed third-party plugins, and advanced specialist environments.

## Non-negotiable boundaries

- Never search ambient `PATH`, accept a remote runtime for the human terminal,
  install privileged software, expose a runtime socket, or fall back to a host
  shell.
- Terminal remains usable without a provider or Toolbox. Automation prepares
  only official signed tool images and remains policy/approval controlled.
- Original evidence is immutable. Screenshots and image edits retain parent
  lineage, source context, verified hashes, and a bounded versioned recipe.
- Secrets are write-only and referenced as `vault:`, `env:`, or `session:`;
  plaintext persistence is forbidden.
- Terminal output and selection drafts remain in memory unless the operator
  explicitly sends, captures, or promotes them.

## Version and command contract

Nebula 3 version sources are synchronized through `NEBULA3_VERSION` and
`python scripts/nebula3_version.py`; the Nebula 2 Python package retains its own
2.x version. Native installers are the canonical user path:

- `nebula` opens the desktop application.
- `nebula-core` provides doctor, migrate, import, export, headless serve, and
  other administration commands.
- `poetry run nebula3` remains a source-checkout compatibility alias only.

See [Nebula 3](docs/NEBULA3.md),
[Migrating from Nebula 2](docs/MIGRATING-2-TO-3.md), and
[the release runbook](packaging/RELEASING.md).
