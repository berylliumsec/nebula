import { forEachDiagnostic } from "@codemirror/lint";
import type { Extension } from "@codemirror/state";
import { EditorView, ViewPlugin, type ViewUpdate } from "@codemirror/view";

const MINIMAP_WIDTH = 74;
/** Columns the strip can show before a line is clipped. */
const COLUMNS = 92;
/** Past this, one document line no longer deserves its own pixel row. */
const MAX_ROWS = 20_000;

function cssColor(element: HTMLElement, name: string, fallback: string): string {
  const value = getComputedStyle(element).getPropertyValue(name).trim();
  return value || fallback;
}

/**
 * Scale-model of the whole file down the right edge.
 *
 * The strip always shows the entire document rather than a scrolling window, so
 * the slider position is an honest "where am I in this file". Lines are drawn as
 * word-shaped blocks instead of tiny glyphs: it reads the same at a glance and
 * costs one canvas pass instead of a second text layout.
 */
export function minimap(): Extension {
  return ViewPlugin.fromClass(class {
    private readonly host: HTMLDivElement;
    private readonly canvas: HTMLCanvasElement;
    private readonly slider: HTMLDivElement;
    private readonly onScroll: () => void;
    private frame = 0;
    private dragging = false;
    private redraw = true;

    constructor(private readonly view: EditorView) {
      this.host = document.createElement("div");
      this.host.className = "cm-nebula-minimap";
      this.host.setAttribute("aria-hidden", "true");
      this.canvas = document.createElement("canvas");
      this.slider = document.createElement("div");
      this.slider.className = "cm-nebula-minimap-slider";
      this.host.append(this.canvas, this.slider);
      view.dom.append(this.host);
      view.dom.classList.add("cm-has-minimap");
      view.dom.style.setProperty("--cm-minimap-width", `${MINIMAP_WIDTH}px`);
      // Reserve the strip on the scroller itself: a themed rule would have to
      // out-specify CodeMirror's own scroller styling on every host page.
      view.scrollDOM.style.marginRight = `${MINIMAP_WIDTH}px`;

      this.host.addEventListener("pointerdown", this.onPointerDown);
      this.host.addEventListener("pointermove", this.onPointerMove);
      this.host.addEventListener("pointerup", this.onPointerUp);
      this.host.addEventListener("pointercancel", this.onPointerUp);
      this.onScroll = () => this.positionSlider();
      view.scrollDOM.addEventListener("scroll", this.onScroll, { passive: true });
      this.schedule();
    }

    update(update: ViewUpdate) {
      // Both the strip and the slider need the layout, which CodeMirror forbids
      // reading inside an update, so every path defers to the next frame.
      this.redraw ||= update.docChanged || update.geometryChanged;
      this.schedule();
    }

    destroy() {
      cancelAnimationFrame(this.frame);
      this.view.scrollDOM.removeEventListener("scroll", this.onScroll);
      this.host.removeEventListener("pointerdown", this.onPointerDown);
      this.host.removeEventListener("pointermove", this.onPointerMove);
      this.host.removeEventListener("pointerup", this.onPointerUp);
      this.host.removeEventListener("pointercancel", this.onPointerUp);
      this.host.remove();
      this.view.dom.classList.remove("cm-has-minimap");
      this.view.scrollDOM.style.marginRight = "";
    }

    private readonly onPointerDown = (event: PointerEvent) => {
      this.dragging = true;
      this.host.setPointerCapture(event.pointerId);
      this.scrollToPointer(event);
    };

    private readonly onPointerMove = (event: PointerEvent) => {
      if (this.dragging) this.scrollToPointer(event);
    };

    private readonly onPointerUp = (event: PointerEvent) => {
      this.dragging = false;
      if (this.host.hasPointerCapture(event.pointerId)) this.host.releasePointerCapture(event.pointerId);
    };

    /** Rows are the strip's unit, so pointer and slider share one scale. */
    private rowHeight(): number {
      const lines = Math.min(this.view.state.doc.lines, MAX_ROWS);
      return Math.min(3, this.host.clientHeight / Math.max(lines, 1));
    }

    private scrollToPointer(event: PointerEvent) {
      const bounds = this.host.getBoundingClientRect();
      const rowHeight = this.rowHeight();
      if (bounds.height <= 0 || rowHeight <= 0) return;
      const { doc } = this.view.state;
      const number = Math.min(Math.max(Math.round((event.clientY - bounds.top) / rowHeight) + 1, 1), doc.lines);
      const block = this.view.lineBlockAt(doc.line(number).from);
      const scroller = this.view.scrollDOM;
      const reachable = Math.max(scroller.scrollHeight - scroller.clientHeight, 0);
      scroller.scrollTop = Math.min(Math.max(block.top - scroller.clientHeight / 2, 0), reachable);
    }

    private schedule() {
      cancelAnimationFrame(this.frame);
      this.frame = requestAnimationFrame(() => {
        if (this.redraw) {
          this.redraw = false;
          this.render();
        } else {
          this.positionSlider();
        }
      });
    }

    /**
     * The slider covers the lines on screen, not a share of the scroll range.
     * A short file draws only a few rows at the top of the strip, and a slider
     * scaled to the scroller would then cover blank space below them.
     */
    private positionSlider() {
      const scroller = this.view.scrollDOM;
      const rowHeight = this.rowHeight();
      if (!this.host.clientHeight || rowHeight <= 0 || !scroller.clientHeight) return;
      const { doc } = this.view.state;
      const first = doc.lineAt(this.view.lineBlockAtHeight(scroller.scrollTop).from).number;
      const last = doc.lineAt(this.view.lineBlockAtHeight(scroller.scrollTop + scroller.clientHeight).from).number;
      this.slider.style.top = `${(first - 1) * rowHeight}px`;
      this.slider.style.height = `${Math.max((last - first + 1) * rowHeight, 10)}px`;
    }

    private render() {
      const scroller = this.view.scrollDOM;
      this.host.style.top = `${scroller.offsetTop}px`;
      this.host.style.height = `${scroller.clientHeight}px`;
      const width = this.host.clientWidth;
      const height = this.host.clientHeight;
      const context = this.canvas.getContext("2d");
      if (!context || width <= 0 || height <= 0) return;
      const ratio = Math.min(globalThis.devicePixelRatio || 1, 2);
      this.canvas.width = Math.round(width * ratio);
      this.canvas.height = Math.round(height * ratio);
      context.setTransform(ratio, 0, 0, ratio, 0, 0);
      context.clearRect(0, 0, width, height);

      const { doc } = this.view.state;
      const lines = Math.min(doc.lines, MAX_ROWS);
      const rowHeight = this.rowHeight();
      const columnWidth = (width - 8) / COLUMNS;
      context.fillStyle = cssColor(this.view.dom, "--code-minimap-ink", "rgba(223,229,236,0.42)");
      for (let number = 1; number <= lines; number += 1) {
        const y = (number - 1) * rowHeight;
        if (y > height) break;
        const text = doc.line(number).text;
        let column = 0;
        let runStart = -1;
        for (let index = 0; index <= text.length && column < COLUMNS; index += 1) {
          const character = text[index];
          const blank = character === undefined || character === " " || character === "\t";
          if (!blank && runStart < 0) runStart = column;
          if (blank && runStart >= 0) {
            context.fillRect(4 + runStart * columnWidth, y, Math.max((column - runStart) * columnWidth, 1), Math.max(rowHeight - 1, 1));
            runStart = -1;
          }
          column += character === "\t" ? this.view.state.tabSize - (column % this.view.state.tabSize) : 1;
        }
      }

      const error = cssColor(this.view.dom, "--red", "#e06e72");
      const warning = cssColor(this.view.dom, "--orange", "#d49a5b");
      forEachDiagnostic(this.view.state, (diagnostic, from) => {
        const number = doc.lineAt(from).number;
        if (number > lines) return;
        context.fillStyle = diagnostic.severity === "error" ? error : warning;
        context.fillRect(width - 10, (number - 1) * rowHeight, 8, Math.max(rowHeight, 2));
      });

      this.positionSlider();
    }
  });
}
