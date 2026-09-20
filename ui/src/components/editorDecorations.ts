import { EditorState, RangeSetBuilder, type Extension } from "@codemirror/state";
import { Decoration, EditorView, ViewPlugin, type DecorationSet, type ViewUpdate } from "@codemirror/view";

/** Colouring a 1 MiB minified bundle costs more than it tells the operator. */
const BRACKET_SCAN_LIMIT = 150_000;
/** Blank lines borrow the indentation of their neighbours, but not forever. */
const BLANK_LOOKAROUND = 200;

const OPENERS = "([{";
const CLOSERS = ")]}";

export interface CommentSyntax {
  line?: string;
  block?: [string, string];
  /** Languages where a quote is an ordinary character, such as plain text. */
  strings: boolean;
}

const SHELL_LIKE: CommentSyntax = { line: "#", strings: true };
const C_LIKE: CommentSyntax = { line: "//", block: ["/*", "*/"], strings: true };

/** Comment and string syntax per extension, so bracket scanning skips prose. */
export function commentSyntaxForPath(path: string): CommentSyntax {
  const extension = path.split(".").pop()?.toLowerCase() ?? "";
  if (["py", "sh", "bash", "zsh", "yaml", "yml", "toml", "rb"].includes(extension)) return SHELL_LIKE;
  if (["sql"].includes(extension)) return { line: "--", block: ["/*", "*/"], strings: true };
  if (["css"].includes(extension)) return { block: ["/*", "*/"], strings: true };
  if (["json"].includes(extension)) return { strings: true };
  if (["md", "markdown", "txt"].includes(extension)) return { strings: false };
  if (["js", "cjs", "mjs", "jsx", "ts", "tsx", "c", "h", "cc", "cpp", "cxx", "hpp", "go", "rs", "java"].includes(extension)) return C_LIKE;
  return C_LIKE;
}

/**
 * Nesting depth for every bracket in the document.
 *
 * A plain text scan beats walking the syntax tree here: it runs in well under a
 * millisecond on the sizes this editor accepts, and it does not need a parsed
 * tree to be ready. Quotes and comments are skipped so a brace inside a string
 * never shifts the colours of real code.
 */
export function bracketDepths(text: string, syntax: CommentSyntax): Map<number, number> {
  const depths = new Map<number, number>();
  const stack: number[] = [];
  let index = 0;
  while (index < text.length) {
    const character = text[index];
    if (syntax.block && text.startsWith(syntax.block[0], index)) {
      const end = text.indexOf(syntax.block[1], index + syntax.block[0].length);
      index = end === -1 ? text.length : end + syntax.block[1].length;
      continue;
    }
    if (syntax.line && text.startsWith(syntax.line, index)) {
      const end = text.indexOf("\n", index);
      index = end === -1 ? text.length : end;
      continue;
    }
    if (syntax.strings && (character === "\"" || character === "'" || character === "`")) {
      index += 1;
      while (index < text.length && text[index] !== character) {
        if (text[index] === "\\") index += 1;
        else if (text[index] === "\n" && character !== "`") break;
        index += 1;
      }
      index += 1;
      continue;
    }
    if (OPENERS.includes(character)) {
      depths.set(index, stack.length);
      stack.push(index);
    } else if (CLOSERS.includes(character)) {
      const opener = stack.pop();
      depths.set(index, opener === undefined ? -1 : stack.length);
    }
    index += 1;
  }
  // Openers that never closed are a mistake, not a nesting level.
  for (const orphan of stack) depths.set(orphan, -1);
  return depths;
}

const bracketMarks = [
  Decoration.mark({ class: "cm-bracket-depth-0" }),
  Decoration.mark({ class: "cm-bracket-depth-1" }),
  Decoration.mark({ class: "cm-bracket-depth-2" }),
];
const unmatchedBracket = Decoration.mark({ class: "cm-bracket-unmatched" });

/** Rotating colours per nesting depth, the way VS Code's bracket pairs read. */
export function bracketPairColors(pathForSyntax: () => string): Extension {
  return ViewPlugin.fromClass(class {
    decorations: DecorationSet;
    private cachedDoc: unknown;
    private cachedDepths = new Map<number, number>();

    constructor(view: EditorView) {
      this.decorations = this.build(view);
    }

    update(update: ViewUpdate) {
      if (update.docChanged || update.viewportChanged) this.decorations = this.build(update.view);
    }

    private depths(view: EditorView): Map<number, number> {
      if (this.cachedDoc === view.state.doc) return this.cachedDepths;
      this.cachedDoc = view.state.doc;
      this.cachedDepths = view.state.doc.length > BRACKET_SCAN_LIMIT
        ? new Map()
        : bracketDepths(view.state.doc.toString(), commentSyntaxForPath(pathForSyntax()));
      return this.cachedDepths;
    }

    private build(view: EditorView): DecorationSet {
      const depths = this.depths(view);
      const builder = new RangeSetBuilder<Decoration>();
      if (!depths.size) return builder.finish();
      for (const { from, to } of view.visibleRanges) {
        const text = view.state.doc.sliceString(from, to);
        for (let offset = 0; offset < text.length; offset += 1) {
          const character = text[offset];
          if (!OPENERS.includes(character) && !CLOSERS.includes(character)) continue;
          const depth = depths.get(from + offset);
          if (depth === undefined) continue;
          builder.add(from + offset, from + offset + 1, depth < 0 ? unmatchedBracket : bracketMarks[depth % bracketMarks.length]);
        }
      }
      return builder.finish();
    }
  }, { decorations: (plugin) => plugin.decorations });
}

/** Leading whitespace of a line measured in columns, with tabs expanded. */
export function indentColumns(text: string, tabSize: number): number {
  let columns = 0;
  for (const character of text) {
    if (character === " ") columns += 1;
    else if (character === "\t") columns += tabSize - (columns % tabSize);
    else return columns;
  }
  // A whitespace-only line has no content of its own to align to.
  return -1;
}

function levelsForLine(state: EditorState, lineNumber: number, tabSize: number): number {
  const own = indentColumns(state.doc.line(lineNumber).text, tabSize);
  if (own >= 0) return Math.floor(own / tabSize);
  let before = -1;
  for (let probe = lineNumber - 1; probe >= 1 && lineNumber - probe <= BLANK_LOOKAROUND; probe -= 1) {
    const columns = indentColumns(state.doc.line(probe).text, tabSize);
    if (columns >= 0) { before = columns; break; }
  }
  let after = -1;
  for (let probe = lineNumber + 1; probe <= state.doc.lines && probe - lineNumber <= BLANK_LOOKAROUND; probe += 1) {
    const columns = indentColumns(state.doc.line(probe).text, tabSize);
    if (columns >= 0) { after = columns; break; }
  }
  if (before < 0 && after < 0) return 0;
  const columns = before < 0 ? after : after < 0 ? before : Math.min(before, after);
  return Math.floor(columns / tabSize);
}

/**
 * The guide of the block holding the cursor, and the lines it spans.
 *
 * Sitting on a line that opens a block highlights the block it opens, which is
 * what an operator means by "where am I"; anywhere else highlights the block the
 * line belongs to.
 */
export function activeIndentScope(state: EditorState, tabSize: number): { level: number; from: number; to: number } | undefined {
  const cursorLine = state.doc.lineAt(state.selection.main.head).number;
  const own = levelsForLine(state, cursorLine, tabSize);
  const next = cursorLine < state.doc.lines ? levelsForLine(state, cursorLine + 1, tabSize) : own;
  const level = next > own ? own : own - 1;
  if (level < 0) return undefined;
  const inside = level + 1;
  // On an opening line the block is what follows it, not the line itself.
  const body = levelsForLine(state, cursorLine, tabSize) >= inside ? cursorLine : cursorLine + 1;
  if (body > state.doc.lines || levelsForLine(state, body, tabSize) < inside) return undefined;
  let from = body;
  let to = body;
  while (from > 1 && levelsForLine(state, from - 1, tabSize) >= inside) from -= 1;
  while (to < state.doc.lines && levelsForLine(state, to + 1, tabSize) >= inside) to += 1;
  return { level, from, to };
}

/** Vertical rules per indent level, with the enclosing block emphasised. */
export function indentGuides(): Extension {
  return ViewPlugin.fromClass(class {
    decorations: DecorationSet;

    constructor(view: EditorView) {
      this.decorations = this.build(view);
      this.measure(view);
    }

    update(update: ViewUpdate) {
      if (update.docChanged || update.viewportChanged || update.selectionSet) this.decorations = this.build(update.view);
      if (update.geometryChanged || update.docChanged) this.measure(update.view);
    }

    /** Guides are painted with a gradient, so they need the real column width. */
    private measure(view: EditorView) {
      const tabSize = view.state.tabSize;
      const step = view.defaultCharacterWidth * tabSize;
      if (!Number.isFinite(step) || step <= 0) return;
      view.dom.style.setProperty("--cm-indent-step", `${step}px`);
      const line = view.contentDOM.firstElementChild;
      const origin = line ? Number.parseFloat(getComputedStyle(line).paddingLeft) : Number.NaN;
      view.dom.style.setProperty("--cm-indent-origin", `${Number.isFinite(origin) ? origin : 6}px`);
    }

    private build(view: EditorView): DecorationSet {
      const { state } = view;
      const tabSize = state.tabSize;
      const active = activeIndentScope(state, tabSize);
      const builder = new RangeSetBuilder<Decoration>();
      for (const { from, to } of view.visibleRanges) {
        const first = state.doc.lineAt(from).number;
        const last = state.doc.lineAt(to).number;
        for (let number = first; number <= last; number += 1) {
          const levels = levelsForLine(state, number, tabSize);
          if (levels <= 0) continue;
          const line = state.doc.line(number);
          const highlighted = active && number >= active.from && number <= active.to && active.level < levels;
          builder.add(line.from, line.from, Decoration.line({
            class: highlighted ? "cm-indent-guides cm-indent-guide-active" : "cm-indent-guides",
            attributes: highlighted
              ? { style: `--cm-indent-levels:${levels};--cm-indent-active:${active.level}` }
              : { style: `--cm-indent-levels:${levels}` },
          }));
        }
      }
      return builder.finish();
    }
  }, { decorations: (plugin) => plugin.decorations });
}
