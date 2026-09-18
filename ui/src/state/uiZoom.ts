import { useCallback, useEffect, useState } from "react";
import { invoke } from "@tauri-apps/api/core";
import { isTauriRuntime } from "../api/runtime";
import { logDiagnostic } from "../diagnostics";

// Font sizes are authored in px throughout the stylesheet, so text size is
// adjusted with native webview zoom. It scales text and layout together, like
// browser zoom, and keeps pointer coordinates correct for the terminal/editor.
export const UI_ZOOM_STEPS = [0.67, 0.75, 0.8, 0.9, 1, 1.1, 1.25, 1.5, 1.75, 2] as const;
export const UI_ZOOM_DEFAULT = 1;
const STORAGE_KEY = "nebula.ui-zoom";
const CHANGE_EVENT = "nebula:ui-zoom";

export type ZoomDirection = "in" | "out" | "reset";

export function normalizeZoom(value: unknown): number {
  const numeric = typeof value === "string" ? Number(value) : value;
  return typeof numeric === "number" && (UI_ZOOM_STEPS as readonly number[]).includes(numeric) ? numeric : UI_ZOOM_DEFAULT;
}

export function nextZoom(current: number, direction: ZoomDirection): number {
  if (direction === "reset") return UI_ZOOM_DEFAULT;
  const index = UI_ZOOM_STEPS.indexOf(normalizeZoom(current) as (typeof UI_ZOOM_STEPS)[number]);
  const next = direction === "in" ? Math.min(UI_ZOOM_STEPS.length - 1, index + 1) : Math.max(0, index - 1);
  return UI_ZOOM_STEPS[next];
}

/** Map ⌘/Ctrl with +, =, -, _ or 0 to a zoom direction; ignore every other chord. */
export function zoomShortcut(
  event: Pick<KeyboardEvent, "key" | "metaKey" | "ctrlKey" | "altKey">,
  mac: boolean,
): ZoomDirection | null {
  const primary = mac ? event.metaKey && !event.ctrlKey : event.ctrlKey && !event.metaKey;
  if (!primary || event.altKey) return null;
  if (event.key === "=" || event.key === "+") return "in";
  if (event.key === "-" || event.key === "_") return "out";
  if (event.key === "0") return "reset";
  return null;
}

export function storedZoom(): number {
  try {
    return normalizeZoom(localStorage.getItem(STORAGE_KEY));
  } catch {
    return UI_ZOOM_DEFAULT;
  }
}

export function uiZoomSupported(): boolean {
  return isTauriRuntime();
}

export async function applyZoom(level: number): Promise<number> {
  const normalized = normalizeZoom(level);
  if (!uiZoomSupported()) return normalized;
  try {
    await invoke("set_interface_zoom", { scale: normalized });
  } catch (error) {
    void logDiagnostic({
      level: "error",
      eventCode: "interface.zoom.apply_failed",
      message: "The interface could not change the text size.",
      outcome: "fallback",
      stage: "zoom",
      retryable: true,
      exception: error,
    });
    return storedZoom();
  }
  try {
    localStorage.setItem(STORAGE_KEY, String(normalized));
  } catch {
    // The zoom still applies for this launch when storage is unavailable.
  }
  window.dispatchEvent(new CustomEvent<number>(CHANGE_EVENT, { detail: normalized }));
  return normalized;
}

function isMac(): boolean {
  return /mac|iphone|ipad/i.test(navigator.platform || navigator.userAgent);
}

/** Restore the saved zoom and bind ⌘+ / ⌘− / ⌘0 in the desktop app. */
export function installZoomShortcuts(): () => void {
  if (!uiZoomSupported()) return () => undefined;
  const saved = storedZoom();
  if (saved !== UI_ZOOM_DEFAULT) void applyZoom(saved);
  const mac = isMac();
  const onKeyDown = (event: KeyboardEvent) => {
    const direction = zoomShortcut(event, mac);
    if (!direction) return;
    event.preventDefault();
    event.stopPropagation();
    void applyZoom(nextZoom(storedZoom(), direction));
  };
  window.addEventListener("keydown", onKeyDown, { capture: true });
  return () => window.removeEventListener("keydown", onKeyDown, { capture: true });
}

export function useUiZoom(): { zoom: number; supported: boolean; change: (direction: ZoomDirection) => void } {
  const [zoom, setZoom] = useState(storedZoom);
  useEffect(() => {
    const sync = (event: Event) => setZoom(normalizeZoom((event as CustomEvent<number>).detail));
    window.addEventListener(CHANGE_EVENT, sync);
    return () => window.removeEventListener(CHANGE_EVENT, sync);
  }, []);
  const change = useCallback((direction: ZoomDirection) => {
    void applyZoom(nextZoom(storedZoom(), direction)).then(setZoom);
  }, []);
  return { zoom, supported: uiZoomSupported(), change };
}
