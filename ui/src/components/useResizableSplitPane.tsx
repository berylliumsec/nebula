import { useEffect, useRef, useState, type CSSProperties, type PointerEvent as ReactPointerEvent } from "react";

const DIVIDER_WIDTH = 12;
const MIN_PANE_WIDTH = 320;

function storedRatio(key: string): number {
  try {
    const value = Number(localStorage.getItem(key));
    return Number.isFinite(value) && value > 0 && value < 1 ? value : 0.5;
  } catch {
    // diagnostic-expected: blocked device storage falls back to an even split.
    return 0.5;
  }
}

/** Two independent chat panes start equally sized and retain a device-local ratio. */
export function useResizableSplitPane(storageKey: string, enabled: boolean) {
  const containerRef = useRef<HTMLElement | null>(null);
  const drag = useRef<{ pointerId: number; startX: number; startRatio: number } | undefined>(undefined);
  const [ratio, setRatio] = useState(() => storedRatio(storageKey));
  const [width, setWidth] = useState(0);

  useEffect(() => {
    if (!enabled || !containerRef.current) return;
    const container = containerRef.current;
    const observer = new ResizeObserver(() => setWidth(container.clientWidth));
    observer.observe(container);
    setWidth(container.clientWidth);
    return () => observer.disconnect();
  }, [enabled]);

  const available = Math.max(1, width - DIVIDER_WIDTH);
  const minRatio = Math.min(0.5, MIN_PANE_WIDTH / available);
  const maxRatio = 1 - minRatio;
  const clamp = (value: number) => Math.max(minRatio, Math.min(maxRatio, value));
  const effectiveRatio = clamp(ratio);
  const resizeTo = (value: number) => setRatio(clamp(value));

  useEffect(() => {
    if (!enabled) return;
    try { localStorage.setItem(storageKey, String(ratio)); }
    catch { /* diagnostic-expected: a blocked preference store leaves this layout adjustable in memory. */ }
  }, [enabled, ratio, storageKey]);

  const endDrag = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (drag.current?.pointerId !== event.pointerId) return;
    drag.current = undefined;
    if (event.currentTarget.hasPointerCapture(event.pointerId)) event.currentTarget.releasePointerCapture(event.pointerId);
  };

  const resizeHandle = enabled ? <div className="side-chat-divider">
    <div
      role="separator"
      tabIndex={0}
      aria-label="Resize side chat"
      aria-orientation="vertical"
      aria-valuemin={Math.round(minRatio * 100)}
      aria-valuemax={Math.round(maxRatio * 100)}
      aria-valuenow={Math.round(effectiveRatio * 100)}
      title="Drag to resize. Use Left and Right arrow keys when focused; double-click to reset."
      className="side-chat-resize-handle"
      onDoubleClick={() => resizeTo(0.5)}
      onPointerDown={(event) => {
        if (event.button !== 0) return;
        drag.current = { pointerId: event.pointerId, startX: event.clientX, startRatio: effectiveRatio };
        event.currentTarget.focus();
        event.currentTarget.setPointerCapture(event.pointerId);
        event.preventDefault();
      }}
      onPointerMove={(event) => {
        if (drag.current?.pointerId !== event.pointerId) return;
        resizeTo(drag.current.startRatio + (event.clientX - drag.current.startX) / available);
      }}
      onPointerUp={endDrag}
      onPointerCancel={endDrag}
      onLostPointerCapture={() => { drag.current = undefined; }}
      onKeyDown={(event) => {
        const step = (event.shiftKey ? 80 : 24) / available;
        if (event.key === "ArrowLeft") resizeTo(effectiveRatio - step);
        else if (event.key === "ArrowRight") resizeTo(effectiveRatio + step);
        else if (event.key === "Home") resizeTo(minRatio);
        else if (event.key === "End") resizeTo(maxRatio);
        else return;
        event.preventDefault();
      }}
    ><span aria-hidden="true">⋮</span></div>
  </div> : null;

  const style: CSSProperties | undefined = enabled
    ? { gridTemplateColumns: `${effectiveRatio}fr ${DIVIDER_WIDTH}px ${1 - effectiveRatio}fr` }
    : undefined;
  return { containerRef, resizeHandle, style };
}
