# Coding editor parity

Figma: [Code editor parity](https://www.figma.com/design/tVEa0PXZaa7H4ZLflwgcb7?node-id=89-2)
(C1 desktop, C2 syntax tokens, C3 editor settings, C4 phone).

This covers **Project → Workbench → Coding**. The panel already had the
capabilities a code editor needs — multiple buffers, split panes, quick open,
workspace search, source control, a Python language server, a debugger, reviewed
execution and evidence preservation. What it did not have was the rendering an
operator judges an editor by in the first ten seconds, and it was losing that
comparison to VS Code before any of the capability mattered.

## What the review found

**Syntax colour was the largest defect, and it was not cosmetic.** The surface
mounted `syntaxHighlighting(defaultHighlightStyle)`. That palette is CodeMirror's
default for *white* backgrounds: `#708` keywords, `#a11` strings, `#219` names.
Nebula's calm, dark and Zero dark themes paint code on `#0c0f13`–`#020203`, so
keywords rendered at roughly 1.3:1 and strings at about 2.1:1 — below any
legibility floor, in the one surface whose entire job is reading text. Only the
light themes were ever readable.

The rest of the gap was rendering the editor never had: no minimap, no sticky
scroll, no indent guides, no bracket-pair colours, no symbol breadcrumbs, one
generic grey file glyph for every language, a status bar that displayed state but
could not change it, and no auto-save.

## What this change ships

*Surface (CodeMirror extensions, `ui/src/components/editor*.ts`)*

- **`nebulaHighlightStyle`** — every token colour resolves through a `--code-*`
  CSS variable, so one highlight style serves all five themes. Custom properties
  inherit through the editor's shadow root, which is what makes this possible
  without a per-theme editor theme. Declared in `tokens.css`.
- **Minimap** — a canvas scale model of the whole file with a viewport slider,
  problem markers from the lint state, and click/drag to scroll. Off when the
  pane is narrower than 760 px.
- **Sticky scroll** — up to three enclosing declarations pinned to the top of the
  pane, syntax-highlighted, each one a button that reveals its line. Scope
  detection is indentation-based rather than tree-based so it behaves the same in
  the legacy stream modes (Go, Rust, Java, shell, YAML) as in the Lezer ones.
- **Indent guides** — one rule per level with the block holding the cursor
  emphasised. Painted as a line-decoration gradient, so a 40-level file costs one
  decoration per line rather than one node per level.
- **Bracket-pair colours** — three rotating colours by nesting depth and red for
  a bracket with no partner, from a text scan that skips strings and comments.
  Disabled above 150 KB, where the cost stops paying for itself.
- **Keys VS Code operators already have in their hands** — `Ctrl/Cmd+G` go to
  line, `Ctrl/Cmd+Alt+↑/↓` add a cursor above or below, `Ctrl/Cmd+Shift+\` jump
  to the matching bracket. `Ctrl+D`, `Ctrl+Shift+L`, line move/copy/delete and
  comment toggling were already bound by CodeMirror's own keymaps.

*Chrome (`CodeEditorPanel`)*

- **Symbol breadcrumbs** above the editor; each segment reveals that folder in
  Files. Hidden below 760 px, where the toolbar already shows the path.
- **Language-coloured file glyphs** in the explorer, the tabs and the trail.
- **Tabs** show an unsaved dot where the close control sits and swap to close on
  hover; middle-click closes. Pointer-coarse devices keep both controls in place.
- **Status bar cells are controls**: position opens Go to line, language and
  indentation open editor settings.
- **Auto save** — off, after one second, or when focus leaves the editor. It only
  writes files that already exist in the workspace, and never while a newer
  version is waiting for a decision, so it can never invent a file or resolve a
  conflict on the operator's behalf.

## State and quality contract

Core and the linked workspace stay authoritative for file bytes and
source-control state. React buffer state owns the unsaved draft; durable editor
recovery owns restored tabs; the URL owns the active workbench route; device
preferences own rendering and auto-save. Rendering preferences are device-local
by design: a phone turning the minimap off must not change the desktop.

Auto save is a save, not a new path: it reuses `save()` with the same conditional
`expectedSha256`, so an external edit still returns 412 and still raises the
conflict banner instead of overwriting.

## Deliberately not in this change

- **A nested file tree.** The explorer is still a flat directory browser with
  breadcrumbs. A tree changes what clicking a directory means, which is covered
  by an existing journey (`editor-files-scroll`), so it belongs in its own change
  with its own test update rather than riding along with the rendering work.
- **Format on save.** The Ruff formatter applies its edits asynchronously through
  the language server with no completion signal, so "format then save" would be a
  timing guess. Explicit *Format document* stays as it is.
- **Git change markers in the gutter and the minimap.** Source control already
  fetches per-file diffs; wiring them into the gutter needs its own refresh and
  invalidation story.

## Verification

Component and state tests cover the bracket scanner, indent measurement, active
scope, sticky scopes, glyph mapping, token-only colours, preference migration,
auto-save behaviour including the untitled-draft refusal, and the breadcrumb and
status-bar controls. The production bundle was exercised at 1440×900 and 390×844:
the pinned scope, the reserved minimap column and slider, the indent step
measured from the real character width, three distinct bracket colours, and the
keyword colour resolving to `#b493ff` in dark and `#7b3fd4` in light. The
`coding-makeover`, `editor-files-scroll` and caret-alignment journeys pass on the
preview bundle. No physical device and no real-Core run was available.
