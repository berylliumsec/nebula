# Code Workbench product contract

## Outcome

An operator can investigate and change a project without leaving Nebula or losing
the project, scope, evidence, execution, and assistant context that makes the work
reportable.  The Code view must behave like an IDE, not a single-file textarea.

The long-term parity baseline is the everyday VS Code workbench: multi-document
editing, navigation and search, language intelligence, source control, terminals,
tasks, debugging, settings and keybindings, extension-backed language tooling,
and recoverable workspace state.  Nebula is better only where it adds explicit
security-project scope, isolated execution, evidence lineage, findings, and
operator-reviewed AI without weakening the linked-workspace boundary.

## Acceptance contract

| Journey step | Observable invariant | State authority | Test layer |
| --- | --- | --- | --- |
| Entry and discovery | Code is reachable from Workbench and explains the shared project workspace | URL view plus Core project | component plus Playwright |
| Open and create | Recursive quick open finds files without opaque paths; creating a file opens an editable tab | Core workspace plus React editor session | component plus real Core |
| Multi-file editing | Switching tabs never discards another tab's draft; dirty state is visible per tab | React editor session; Core after save | component plus Playwright |
| Split editing | Two files can remain visible and independently editable; focus selects the authoritative toolbar, save target, and security actions | React editor session plus Core after save | component plus Playwright |
| Hot-exit recovery | A browser reload restores exact dirty and untitled drafts; clean files restore by identity and reload authoritative Core bytes instead of stale cached content | Bounded device-local IndexedDB draft cache plus Core | persistence unit plus component plus real Core |
| Editor preferences | Font size, indentation, word wrap, and conflict-free keybindings apply to every pane and survive browser reload on that device | Validated device-local browser preferences | unit plus component plus Playwright |
| Save and conflict | Saves are atomic and conditional; a Terminal or other-client change cannot be silently overwritten | Core workspace SHA-256 | Core plus component |
| Search and navigate | File and text search are bounded, symlink-safe, keyboard operable, and open the chosen match | Core search result plus editor selection | Core plus component plus Playwright |
| Source-control awareness | Code shows the selected project repository, branch, bounded changes, and hardened staged or working diffs without crossing the project root or executing repository helpers | Core Git query plus shared project workspace | Core plus component plus real Core |
| Language intelligence | Python buffers provide completion, hover, signatures, same-buffer definition/references/rename, formatting, and live Problems without reading host project configuration or following imports outside editor-supplied bytes | Authenticated ephemeral LSP session; open editor bytes only | language unit plus authenticated WebSocket plus CodeMirror integration |
| Tasks and tests | Code discovers bounded declarative npm scripts, Make targets, Python test files, and Go/Rust test suites; selecting one opens the existing exact-source execution review rather than running implicitly | Core workspace discovery plus reviewed-execution preflight/runtime/Activity | Core discovery plus component plus real Core |
| VS Code project compatibility | Supported `tasks.json` process/shell tasks and `launch.json` Python launch profiles are parsed without executing extensions or project code; unsupported profiles remain visible with an explanation | Core JSONC parser plus bounded compatibility allowlist | Core parser plus component plus real Core |
| Debugging | A saved Python buffer can enter an isolated debug session pinned to its path and SHA-256; stepping, pause, continue, stack, scopes, and evaluate remain in Nebula's reviewed, read-only, network-disabled runtime | Existing reviewed-execution and container runtime plus debug adapter session | Core plus component plus real Core launch review |
| Shared environment | Code identifies whether the project is a linked host folder or managed workspace and points to the same `/workspace` used by Terminal, tasks, debuggers, harnesses, and agents | Core project workspace plus existing runtime authorities | component plus real Core |
| Security workflow | A saved file can be handed to Terminal, assistant, or immutable Evidence from the editor with the exact project path | Core project/workspace/evidence plus Workbench view | real Core plus Playwright |
| Finding workflow | A saved exact source buffer is reused or preserved as immutable Evidence before a prefilled informational candidate is handed to Findings; Code never claims validation, exploitability, or impact | Core evidence plus Findings authority and explicit operator review | component plus real Core |
| Refresh and reconnect | Saved files reload from Core; dirty drafts trigger unload protection and survive complete Workbench/provider remounts | Core plus React editor session plus bounded IndexedDB cache | component plus real Core |
| Failure and retry | Listing, open, restore, persistence, search, save, conflict, and evidence failures preserve usable tabs and name the next valid action | Core error contract plus local persistence status | component plus real Core |
| Rename and delete | Renames update every open identity; delete clears only the deleted tab after explicit confirmation | Core workspace plus React editor session | component plus real Core |
| Responsive and accessible use | Desktop and 320-430 px mobile layouts retain editor, tabs, files, results, focus, labels, and 44 px touch controls | CSS plus React presentation | Chromium and WebKit Playwright |

## Lifecycle classification

- Discover, create, open, select, edit, save, conflict, refresh, retry, rename,
  delete, and reconnect: required.
- Stream and interrupt: required for Terminal/tasks/debugger integration, not for
  a local editor mutation.
- Fork: not applicable to a file; source-control branches own that lifecycle.
- Revoke: evidence retention policy owns revocation after file preservation.

## State authorities

- Core workspace: file bytes, paths, metadata, conditional-save hashes, search,
  rename, delete, and linked-folder boundaries.
- Core source-control query: read-only Git status and diff for a repository whose
  top level is exactly the selected project folder. Repository-configured hooks,
  textconv, external diffs, prompts, and global configuration are disabled.
- Core evidence store: immutable promoted bytes and lineage.
- Workbench URL: selected project and Code view.
- React editor session: open tabs, focused pane, split layout, selections, and
  live unsaved drafts.
- IndexedDB hot-exit cache: bounded, schema-validated device-local recovery for
  open identities and exact dirty or untitled drafts. Clean workspace content is
  compacted away and reloaded from Core. The cache is never authoritative file
  content and never mutates a project until an operator saves.
- Local storage: validated device-local editor preferences and keybindings only.
- Ephemeral language session: bounded open-buffer text and monotonically
  increasing versions. It has no durable authority and is cleared on socket
  close. Jedi runs against a nonexistent virtual root; Ruff receives stdin with
  isolated configuration and no cache.
- Terminal/harness: execution state and output; it reads the same project folder.
- Terminal/reviewed execution: Git mutations, builds, tasks, debuggers, and other
  command lifecycles. Code discovers task entry points and hands exact commands
  to these existing authorities instead of embedding a second terminal or
  approval system.

## Practical parity and deliberate boundaries

The workbench covers the everyday VS Code capability families that belong in an
editor: multi-document and split editing, syntax and search, palette and
conflict-free keybindings, durable hot exit, language intelligence and Problems,
source status and safe diffs, declarative task discovery, launch configuration,
debug controls, workspace awareness, and settings.  It uses CodeMirror 6,
`@codemirror/lsp-client`, JSONC parsing, Jedi, Ruff, and the Debug Adapter
Protocol instead of implementing editor, protocol, or parser machinery locally.

Parity does not mean cloning every VS Code surface.  These exclusions are product
decisions, not hidden gaps:

- Terminal, agents, execution review, containers, approvals, Evidence, and
  Findings retain their existing Nebula authorities. Code makes exact-context
  handoffs to them instead of embedding weaker copies.
- Git mutation stays in the existing reviewed Terminal workflow. Code provides
  repository identity, branch, changes, staged and working diffs without running
  repository helpers, hooks, text conversion, or prompts.
- Python language navigation is explicitly open-buffer bounded. Project-wide
  indexes, imports, plugins, and configuration would execute or trust project
  material and therefore require a separately admitted sandboxed language host.
- Supported VS Code task and launch files are a compatibility boundary, not an
  extension marketplace. Extension-owned task types and attach profiles are
  visible but disabled with an explanation.
- A Nebula project is one coherent workspace. Arbitrary multi-root and duplicate
  Remote-SSH UIs are excluded; linked host folders and managed isolated
  workspaces already provide the remote/runtime boundary shared by Code and the
  rest of Nebula.

Any future extension or project-wide language host must have project-scoped
filesystem access, explicit network and capability policy, authenticated
transport, lifecycle cleanup, and production/LAN evidence before Code advertises
the capability.
