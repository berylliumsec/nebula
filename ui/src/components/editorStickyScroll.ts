import { syntaxTree } from "@codemirror/language";
import { EditorState, type Extension } from "@codemirror/state";
import { EditorView, ViewPlugin, type ViewUpdate } from "@codemirror/view";
import { highlightTree } from "@lezer/highlight";
import { indentColumns } from "./editorDecorations";
import { nebulaHighlightStyle } from "./editorTheme";

/** More than a few pinned headers costs more editing space than it returns. */
const DEFAULT_MAX_LINES = 3;
/** A declaration that far above the viewport is no longer useful context. */
const LOOKBACK = 4000;

/**
 * Declarations that enclose a line, outermost first.
 *
 * Indentation rather than the syntax tree, because it behaves identically across
 * every language this editor loads — including the legacy stream modes, whose
 * trees have no block structure to walk.
 */
export function enclosingScopes(state: EditorState, lineNumber: number, maxLines = DEFAULT_MAX_LINES): number[] {
  const tabSize = state.tabSize;
  const levelsOf = (number: number): number => {
    const columns = indentColumns(state.doc.line(number).text, tabSize);
    return columns < 0 ? -1 : Math.floor(columns / tabSize);
  };
  const opensBlock = (number: number, levels: number): boolean => {
    for (let probe = number + 1; probe <= state.doc.lines && probe - number < 50; probe += 1) {
      const next = levelsOf(probe);
      if (next < 0) continue;
      return next > levels;
    }
    return false;
  };
  let target = levelsOf(lineNumber);
  for (let probe = lineNumber; target < 0 && probe <= state.doc.lines && probe - lineNumber < 50; probe += 1) target = levelsOf(probe);
  if (target <= 0) return [];
  const scopes: number[] = [];
  for (let probe = lineNumber - 1; probe >= 1 && lineNumber - probe <= LOOKBACK && target > 0; probe -= 1) {
    const levels = levelsOf(probe);
    if (levels < 0 || levels >= target) continue;
    if (opensBlock(probe, levels)) scopes.push(probe);
    target = levels;
  }
  return scopes.reverse().slice(0, maxLines);
}

/**
 * Pins the declarations a line sits inside to the top of the pane.
 *
 * Headers are buttons, not decoration: scrolling away from a function and then
 * needing to get back to its signature is the common case, so clicking one
 * reveals it.
 */
export function stickyScroll(maxLines = DEFAULT_MAX_LINES): Extension {
  return ViewPlugin.fromClass(class {
    private readonly host: HTMLDivElement;
    private readonly onScroll: () => void;
    private frame = 0;
    private rendered = "";

    constructor(private readonly view: EditorView) {
      this.host = document.createElement("div");
      this.host.className = "cm-nebula-sticky";
      this.host.hidden = true;
      view.dom.append(this.host);
      this.onScroll = () => this.refresh();
      view.scrollDOM.addEventListener("scroll", this.onScroll, { passive: true });
      this.frame = requestAnimationFrame(() => this.refresh());
    }

    update(update: ViewUpdate) {
      // Finding the top line reads the layout, which an update may not do.
      if (update.docChanged || update.viewportChanged || update.geometryChanged) {
        cancelAnimationFrame(this.frame);
        this.frame = requestAnimationFrame(() => this.refresh());
      }
    }

    destroy() {
      cancelAnimationFrame(this.frame);
      this.view.scrollDOM.removeEventListener("scroll", this.onScroll);
      this.host.remove();
    }

    private refresh() {
      const { view } = this;
      const scroller = view.scrollDOM;
      // Panels above the scroller must stay clickable, so anchor to the scroller.
      this.host.style.top = `${scroller.offsetTop}px`;
      const top = view.lineBlockAtHeight(scroller.scrollTop + this.host.clientHeight);
      const lineNumber = view.state.doc.lineAt(top.from).number;
      const scopes = scroller.scrollTop <= 0 ? [] : enclosingScopes(view.state, lineNumber, maxLines);
      const signature = scopes.join(",");
      if (signature === this.rendered) return;
      this.rendered = signature;
      this.host.replaceChildren();
      this.host.hidden = scopes.length === 0;
      if (!scopes.length) return;

      // Line numbers line up with the real gutter, and the code starts where
      // the code starts, whatever extra gutters (folds, breakpoints) are on.
      const gutters = view.dom.querySelector(".cm-gutters");
      const numbers = view.dom.querySelector(".cm-lineNumbers");
      const gutterWidth = gutters instanceof HTMLElement ? gutters.clientWidth : 0;
      const numberRight = gutters instanceof HTMLElement && numbers instanceof HTMLElement
        ? numbers.getBoundingClientRect().right - gutters.getBoundingClientRect().left
        : gutterWidth;
      this.host.style.setProperty("--cm-sticky-number-width", `${Math.max(numberRight, 16)}px`);
      this.host.style.setProperty("--cm-sticky-number-gap", `${Math.max(gutterWidth - numberRight + 6, 4)}px`);
      this.host.style.setProperty("--cm-sticky-inset", view.dom.classList.contains("cm-has-minimap") ? "var(--cm-minimap-width, 74px)" : "0px");

      const tree = syntaxTree(view.state);
      for (const number of scopes) {
        const line = view.state.doc.line(number);
        const button = document.createElement("button");
        button.type = "button";
        button.className = "cm-nebula-sticky-line";
        button.title = `Go to line ${number}`;
        const marker = document.createElement("span");
        marker.className = "cm-nebula-sticky-number";
        marker.textContent = String(number);
        button.append(marker);

        let cursor = line.from;
        const push = (from: number, to: number, classes?: string) => {
          if (to <= from) return;
          const span = document.createElement("span");
          if (classes) span.className = classes;
          span.textContent = view.state.doc.sliceString(from, to);
          button.append(span);
        };
        highlightTree(tree, nebulaHighlightStyle, (from, to, classes) => {
          push(cursor, from);
          push(from, to, classes);
          cursor = to;
        }, line.from, line.to);
        push(cursor, line.to);

        button.addEventListener("click", () => {
          view.dispatch({ selection: { anchor: line.from }, effects: EditorView.scrollIntoView(line.from, { y: "start", yMargin: 0 }) });
          view.focus();
        });
        this.host.append(button);
      }
    }
  });
}
