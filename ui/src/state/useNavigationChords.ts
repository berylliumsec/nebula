import { useEffect } from "react";
import { navigationItems } from "../navigation";

const CHORD_WINDOW_MS = 1_200;

function isEditable(target: EventTarget | null): boolean {
  return target instanceof HTMLElement
    && (target.isContentEditable || Boolean(target.closest("input, textarea, select, [contenteditable='true'], .cm-editor, .xterm")));
}

/** "G then a letter" jumps between pages, matching the shortcuts shown in the palette. */
export function useNavigationChords(navigate: (path: string) => void): void {
  useEffect(() => {
    let pendingUntil = 0;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.defaultPrevented || event.metaKey || event.ctrlKey || event.altKey || isEditable(event.target)) return;
      const key = event.key.toLowerCase();
      if (Date.now() < pendingUntil) {
        pendingUntil = 0;
        const item = navigationItems.find(candidate => candidate.shortcut.toLowerCase() === `g ${key}`);
        if (!item) return;
        event.preventDefault();
        navigate(item.path);
        return;
      }
      if (key === "g" && !event.shiftKey) pendingUntil = Date.now() + CHORD_WINDOW_MS;
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [navigate]);
}
