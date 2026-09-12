import { useEffect, useRef, useState, type CSSProperties, type PointerEvent } from "react";
import { logCaughtDiagnostic } from "../diagnostics";

const STORAGE_KEY = "nebula.editor.sidebar-width";
export function useEditorSidebarWidth() {
  const panelRef = useRef<HTMLDivElement>(null);
  const [preferredWidth, setPreferredWidth] = useState(() => {
    try {
      const stored = Number(localStorage.getItem(STORAGE_KEY));
      return Number.isFinite(stored) && stored >= 200 && stored <= 600 ? stored : 250;
    } catch (error) {
      void logCaughtDiagnostic("interface.code_editor.sidebar_width", "Could not read the editor sidebar width.", error, "code_editor");
      return 250;
    }
  });
  const [availableWidth, setAvailableWidth] = useState(920);
  const drag = useRef<{id: number; x: number; width: number} | undefined>(undefined);
  useEffect(() => {
    const panel = panelRef.current;
    if (!panel) return;
    const update = () => { if (panel.clientWidth) setAvailableWidth(panel.clientWidth); };
    update();
    const observer = new ResizeObserver(update);
    observer.observe(panel);
    return () => observer.disconnect();
  }, []);
  const maxWidth = Math.max(200, Math.min(600, availableWidth - 320));
  const width = Math.min(preferredWidth, maxWidth);
  const resize = (next: number) => {
    const value = Math.round(Math.max(200, Math.min(maxWidth, next)));
    setPreferredWidth(value);
    try { localStorage.setItem(STORAGE_KEY, String(value)); }
    catch (error) { void logCaughtDiagnostic("interface.code_editor.sidebar_width", "Could not remember the editor sidebar width.", error, "code_editor"); }
  };
  return {
    panelRef, width, maxWidth, resize,
    style: {"--editor-sidebar-width": `${width}px`} as CSSProperties,
    onPointerDown: (event: PointerEvent<HTMLDivElement>) => {
      if (event.button !== 0) return;
      event.preventDefault();
      event.currentTarget.focus();
      event.currentTarget.setPointerCapture(event.pointerId);
      drag.current = {id: event.pointerId, x: event.clientX, width};
    },
    onPointerMove: (event: PointerEvent<HTMLDivElement>) => {
      if (drag.current?.id === event.pointerId) resize(drag.current.width + event.clientX - drag.current.x);
    },
    onPointerUp: (event: PointerEvent<HTMLDivElement>) => {
      if (drag.current?.id !== event.pointerId) return;
      drag.current = undefined;
      if (event.currentTarget.hasPointerCapture(event.pointerId)) event.currentTarget.releasePointerCapture(event.pointerId);
    },
    onLostPointerCapture: () => { drag.current = undefined; },
  };
}
