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
| Save and conflict | Saves are atomic and conditional; a Terminal or other-client change cannot be silently overwritten | Core workspace SHA-256 | Core plus component |
| Search and navigate | File and text search are bounded, symlink-safe, keyboard operable, and open the chosen match | Core search result plus editor selection | Core plus component plus Playwright |
| Source-control awareness | Code shows the selected project repository, branch, bounded changes, and hardened staged or working diffs without crossing the project root or executing repository helpers | Core Git query plus shared project workspace | Core plus component plus real Core |
| Security workflow | A saved file can be handed to Terminal, assistant, or immutable Evidence from the editor with the exact project path | Core project/workspace/evidence plus Workbench view | real Core plus Playwright |
| Refresh and reconnect | Saved files reload from Core; dirty drafts trigger unload protection and remain available while persistent Workbench state is mounted | Core plus React editor session | component plus real Core |
| Failure and retry | Listing, open, search, save, conflict, and evidence failures preserve usable tabs and name the next valid action | Core error contract | component plus real Core |
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
- React editor session: open tabs, active tab, selections, and unsaved drafts.
- Browser storage: device-local editor preferences only; never authoritative file
  content or project mutations.
- Terminal/harness: execution state and output; it reads the same project folder.
- Terminal/reviewed execution: Git mutations, builds, tasks, debuggers, and other
  command lifecycles. Code hands off to these existing authorities instead of
  embedding a second terminal or approval system.

## Parity boundary

The current native editor can own the Nebula-specific shell and safe workspace
lifecycle.  Full language-server, debugger, source-control, remote-development,
settings/keybinding, and extension-host parity requires a sandboxed IDE service
or equivalent protocol hosts; it must not be simulated with isolated UI buttons.
Those hosts are admitted only with project-scoped filesystem access, explicit
network/capability policy, authenticated transport, lifecycle cleanup, and
production/LAN tests.
