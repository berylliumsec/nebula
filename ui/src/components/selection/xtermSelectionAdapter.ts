import type { PresentSelectionInput, SelectionSource } from "./selectionActions";

export interface DisposableSelectionListener {
  dispose(): void;
}

/** The public xterm selection surface used by the adapter. */
export interface XtermSelectionTerminal {
  element?: HTMLElement;
  getSelection(): string;
  hasSelection(): boolean;
  onSelectionChange(listener: () => void): DisposableSelectionListener;
}

export interface XtermSelectionAdapterOptions {
  source: SelectionSource | (() => SelectionSource);
  getAnchorRect?: () => Pick<DOMRect, "left" | "top" | "right" | "bottom"> | undefined;
  onClear?: () => void;
}

/**
 * Bridges xterm's public selection API to SelectionActionsProvider. The
 * terminal selection is copied into a draft only; this helper never sends it.
 */
export function bindXtermSelectionActions(
  terminal: XtermSelectionTerminal,
  present: (selection: PresentSelectionInput) => void,
  options: XtermSelectionAdapterOptions,
): DisposableSelectionListener {
  let frame: number | undefined;
  const readSelection = () => {
    if (frame !== undefined) globalThis.cancelAnimationFrame?.(frame);
    const run = () => {
      frame = undefined;
      const text = terminal.getSelection();
      if (!terminal.hasSelection() || !text.length) {
        options.onClear?.();
        return;
      }
      const element = terminal.element;
      if (element?.closest("[data-nebula-sensitive], [data-selection-actions='off']")) return;
      const anchorRect = options.getAnchorRect?.() ?? element?.getBoundingClientRect();
      present({
        text,
        source: typeof options.source === "function" ? options.source() : options.source,
        anchorRect,
      });
    };
    if (globalThis.requestAnimationFrame) frame = globalThis.requestAnimationFrame(run);
    else run();
  };
  const listener = terminal.onSelectionChange(readSelection);
  // Some browser/xterm combinations paint a pointer selection without
  // dispatching onSelectionChange. Pointer-up happens after xterm updates its
  // buffer, so use it as a public-DOM fallback and keep the toolbar reliable.
  terminal.element?.addEventListener("pointerup", readSelection);
  return {
    dispose() {
      if (frame !== undefined) globalThis.cancelAnimationFrame?.(frame);
      terminal.element?.removeEventListener("pointerup", readSelection);
      listener.dispose();
    },
  };
}
