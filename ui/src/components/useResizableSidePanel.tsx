import { useCallback, useEffect, useRef, useState, type CSSProperties, type PointerEvent as ReactPointerEvent } from "react";

interface ResizableSidePanelOptions {
  defaultWidth: number;
  enabled?: boolean;
  label: string;
  maxWidth: number;
  minPrimaryWidth: number;
  minWidth: number;
  onWidthChange?: (width: number | undefined) => void;
  storageKey: string;
}

function storedWidth(key: string, fallback: number): number {
  try {
    const value = Number(globalThis.localStorage?.getItem(key));
    return Number.isFinite(value) && value > 0 ? value : fallback;
  } catch {
    // diagnostic-expected: device-local preferences may be blocked or unavailable.
    return fallback;
  }
}

/** Shared, keyboard-accessible sizing behavior for right-hand operator panels. */
export function useResizableSidePanel({
  defaultWidth,
  enabled = true,
  label,
  maxWidth,
  minPrimaryWidth,
  minWidth,
  onWidthChange,
  storageKey,
}: ResizableSidePanelOptions) {
  const panelRef = useRef<HTMLElement | null>(null);
  const drag = useRef<{ pointerId: number; startWidth: number; startX: number } | undefined>(undefined);
  const [width, setWidth] = useState(() => storedWidth(storageKey, defaultWidth));
  const [, setViewportRevision] = useState(0);

  const bounds = useCallback(() => {
    const available = panelRef.current?.parentElement?.clientWidth || globalThis.innerWidth || maxWidth + minPrimaryWidth;
    return {
      min: minWidth,
      max: Math.max(minWidth, Math.min(maxWidth, available - minPrimaryWidth)),
    };
  }, [maxWidth, minPrimaryWidth, minWidth]);

  const resizeTo = useCallback((next: number) => {
    const { min, max } = bounds();
    setWidth(Math.round(Math.max(min, Math.min(max, next))));
  }, [bounds]);

  useEffect(() => {
    if (!enabled) return;
    resizeTo(width);
    const update = () => {
      resizeTo(width);
      setViewportRevision((value) => value + 1);
    };
    globalThis.addEventListener("resize", update);
    return () => globalThis.removeEventListener("resize", update);
  }, [enabled, resizeTo, width]);

  useEffect(() => {
    if (!enabled) return;
    try { globalThis.localStorage?.setItem(storageKey, String(width)); } catch { /* diagnostic-expected: device-local preferences may be unavailable. */ }
  }, [enabled, storageKey, width]);

  const onPointerDown = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (!enabled) return;
    drag.current = { pointerId: event.pointerId, startWidth: width, startX: event.clientX };
    event.currentTarget.setPointerCapture(event.pointerId);
    event.preventDefault();
  };

  const onPointerMove = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (!drag.current || drag.current.pointerId !== event.pointerId) return;
    resizeTo(drag.current.startWidth + drag.current.startX - event.clientX);
  };

  const endPointer = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (drag.current?.pointerId !== event.pointerId) return;
    drag.current = undefined;
    if (event.currentTarget.hasPointerCapture(event.pointerId)) event.currentTarget.releasePointerCapture(event.pointerId);
  };

  const { min, max } = bounds();
  const effectiveWidth = Math.max(min, Math.min(max, width));
  useEffect(() => {
    onWidthChange?.(enabled ? effectiveWidth : undefined);
  }, [effectiveWidth, enabled, onWidthChange]);
  const panelStyle: CSSProperties | undefined = enabled ? { flex: `0 0 ${effectiveWidth}px`, width: effectiveWidth } : undefined;
  const resizeHandle = enabled ? <div
    aria-label={label}
    aria-orientation="vertical"
    aria-valuemax={max}
    aria-valuemin={min}
    aria-valuenow={effectiveWidth}
    className="side-panel-resize-handle"
    onDoubleClick={() => resizeTo(defaultWidth)}
    onKeyDown={(event) => {
      const step = event.shiftKey ? 80 : 24;
      if (event.key === "ArrowLeft") resizeTo(effectiveWidth + step);
      else if (event.key === "ArrowRight") resizeTo(effectiveWidth - step);
      else if (event.key === "Home") resizeTo(min);
      else if (event.key === "End") resizeTo(max);
      else return;
      event.preventDefault();
    }}
    onPointerCancel={endPointer}
    onPointerDown={onPointerDown}
    onPointerMove={onPointerMove}
    onPointerUp={endPointer}
    role="separator"
    tabIndex={0}
    title="Drag to resize. Use Left and Right arrow keys when focused; double-click to reset."
  /> : null;

  return { panelRef, panelStyle, resizeHandle };
}
