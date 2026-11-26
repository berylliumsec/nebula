import { useLayoutEffect, useRef, useState, type CSSProperties, type KeyboardEvent, type PointerEvent as ReactPointerEvent } from "react";
import { readStorage, removeStorage, writeStorage } from "../state/browserStorage";

/** Device-local widths of the Butler threads rail and the open thread; the board takes the rest. */
export const RAIL_WIDTH_KEY = "nebula.butler-mode.rail-width";
export const MIDDLE_WIDTH_KEY = "nebula.butler-mode.thread-width";

export const RAIL_MIN = 200;
export const RAIL_MAX = 480;
export const MIDDLE_MIN = 320;
export const BOARD_MIN = 360;
const RAIL_DEFAULT = 296;
const MIDDLE_DEFAULT = 520;

type Column = "rail" | "middle";

function stored(key: string): number | undefined {
  const value = Number(readStorage(key));
  return Number.isFinite(value) && value > 0 ? Math.round(value) : undefined;
}

/** A rail width the CSS clamps again, so a narrower window never hides the thread or board. */
function railTrack(width: number): string {
  return `clamp(${RAIL_MIN}px, ${width}px, ${RAIL_MAX}px)`;
}

function middleTrack(width: number): string {
  return `max(${MIDDLE_MIN}px, min(${width}px, calc(100% - var(--butler-rail-track, var(--butler-rail-default)) - ${BOARD_MIN}px)))`;
}

/**
 * Two draggable dividers for the desktop Butler mode body. Widths stay CSS defaults until the
 * operator drags; then they persist on this device. Double-click a divider to restore its default.
 */
export function useButlerColumns(enabled: boolean, className: string) {
  const [widths, setWidths] = useState<Record<Column, number | undefined>>(() => ({ rail: stored(RAIL_WIDTH_KEY), middle: stored(MIDDLE_WIDTH_KEY) }));
  const [shown, setShown] = useState<Record<Column, { now: number; max: number } | undefined>>({ rail: undefined, middle: undefined });
  const handles = useRef<Record<Column, HTMLDivElement | null>>({ rail: null, middle: null });
  const [drag, setDrag] = useState<{ column: Column; pointerId: number; startX: number; startWidth: number }>();

  const geometry = (handle: HTMLElement, column: Column) => {
    const container = handle.parentElement?.clientWidth ?? 0;
    const current = (handle.previousElementSibling as HTMLElement | null)?.getBoundingClientRect().width ?? 0;
    // The rail may be hidden, and then its divider is not rendered either.
    const rail = (handles.current.rail?.previousElementSibling as HTMLElement | null | undefined)?.getBoundingClientRect().width ?? 0;
    // jsdom and a detached body report 0: only the fixed bounds apply then.
    const max = column === "rail"
      ? (container ? Math.min(RAIL_MAX, container - MIDDLE_MIN - BOARD_MIN) : RAIL_MAX)
      : (container ? container - rail - BOARD_MIN : Number.POSITIVE_INFINITY);
    const min = column === "rail" ? RAIL_MIN : MIDDLE_MIN;
    return { current: Math.round(current || widths[column] || min), min, max: Math.max(min, Math.round(max)) };
  };

  const resize = (handle: HTMLElement, column: Column, next: number) => {
    const { min, max } = geometry(handle, column);
    const value = Math.round(Math.max(min, Math.min(max, next)));
    setWidths((prior) => ({ ...prior, [column]: value }));
    writeStorage(column === "rail" ? RAIL_WIDTH_KEY : MIDDLE_WIDTH_KEY, String(value));
  };

  // The separators report the columns' rendered widths, which CSS may clamp in a narrower window.
  useLayoutEffect(() => {
    if (!enabled) return;
    const measure = () => setShown((prior) => {
      const next = { ...prior };
      for (const column of ["rail", "middle"] as const) {
        const element = handles.current[column];
        if (!element) continue;
        const { current, max } = geometry(element, column);
        if (prior[column]?.now !== current || prior[column]?.max !== max) next[column] = { now: current, max };
      }
      return next.rail === prior.rail && next.middle === prior.middle ? prior : next;
    });
    measure();
    globalThis.addEventListener("resize", measure);
    return () => globalThis.removeEventListener("resize", measure);
    // geometry reads the DOM and the stored widths captured in this render.
  }, [enabled, widths]);

  const handle = (column: Column, label: string) => {
    const onPointerDown = (event: ReactPointerEvent<HTMLDivElement>) => {
      if (event.button !== 0) return;
      const { current } = geometry(event.currentTarget, column);
      setDrag({ column, pointerId: event.pointerId, startX: event.clientX, startWidth: current });
      event.currentTarget.focus();
      event.currentTarget.setPointerCapture?.(event.pointerId);
      event.preventDefault();
    };
    const end = (event: ReactPointerEvent<HTMLDivElement>) => {
      if (drag?.pointerId !== event.pointerId) return;
      setDrag(undefined);
      if (event.currentTarget.hasPointerCapture?.(event.pointerId)) event.currentTarget.releasePointerCapture(event.pointerId);
    };
    const onKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
      const { current, min, max } = geometry(event.currentTarget, column);
      const step = event.shiftKey ? 80 : 24;
      if (event.key === "ArrowLeft") resize(event.currentTarget, column, current - step);
      else if (event.key === "ArrowRight") resize(event.currentTarget, column, current + step);
      else if (event.key === "Home") resize(event.currentTarget, column, min);
      else if (event.key === "End") resize(event.currentTarget, column, max);
      else return;
      event.preventDefault();
    };
    return <div
      role="separator"
      tabIndex={0}
      aria-label={label}
      aria-orientation="vertical"
      aria-valuemin={column === "rail" ? RAIL_MIN : MIDDLE_MIN}
      aria-valuemax={shown[column]?.max}
      aria-valuenow={shown[column]?.now ?? widths[column] ?? (column === "rail" ? RAIL_DEFAULT : MIDDLE_DEFAULT)}
      ref={(element) => { handles.current[column] = element; }}
      title="Drag to resize. Arrow keys when focused; double-click to reset."
      className={className}
      data-dragging={drag?.column === column ? "true" : undefined}
      onPointerDown={onPointerDown}
      onPointerMove={(event) => {
        if (drag?.column !== column || drag.pointerId !== event.pointerId) return;
        resize(event.currentTarget, column, drag.startWidth + event.clientX - drag.startX);
      }}
      onPointerUp={end}
      onPointerCancel={end}
      onLostPointerCapture={() => setDrag(undefined)}
      onDoubleClick={() => {
        removeStorage(column === "rail" ? RAIL_WIDTH_KEY : MIDDLE_WIDTH_KEY);
        setWidths((prior) => ({ ...prior, [column]: undefined }));
      }}
      onKeyDown={onKeyDown}
    />;
  };

  const style = (enabled ? {
    ...(widths.rail ? { "--butler-rail-track": railTrack(widths.rail) } : {}),
    ...(widths.middle ? { "--butler-middle-track": middleTrack(widths.middle) } : {}),
  } : {}) as CSSProperties;

  return {
    style,
    dragging: Boolean(drag),
    railHandle: enabled ? handle("rail", "Resize threads list") : null,
    middleHandle: enabled ? handle("middle", "Resize open thread") : null,
  };
}
