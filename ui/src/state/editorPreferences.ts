import { useCallback, useEffect, useState } from "react";
import { logCaughtDiagnostic } from "../diagnostics";

export type EditorAction =
  | "closeEditor"
  | "commandPalette"
  | "debug"
  | "find"
  | "format"
  | "definition"
  | "references"
  | "nextEditor"
  | "problems"
  | "quickOpen"
  | "rename"
  | "save"
  | "splitEditor"
  | "tasks"
  | "workspaceSearch";

export interface EditorPreferences {
  fontSize: 12 | 13 | 14 | 16;
  keybindings: Record<EditorAction, string>;
  tabSize: 2 | 4;
  wordWrap: boolean;
}

export const DEFAULT_EDITOR_PREFERENCES: EditorPreferences = {
  fontSize: 13,
  keybindings: {
    closeEditor: "Mod+W",
    commandPalette: "Mod+Shift+P",
    debug: "F5",
    find: "Mod+F",
    format: "Alt+Shift+F",
    definition: "F12",
    references: "Shift+F12",
    nextEditor: "Mod+Tab",
    problems: "Mod+Shift+M",
    quickOpen: "Mod+P",
    rename: "F2",
    save: "Mod+S",
    splitEditor: "Mod+\\",
    tasks: "Mod+Shift+B",
    workspaceSearch: "Mod+Shift+F",
  },
  tabSize: 2,
  wordWrap: false,
};

const STORAGE_KEY = "nebula.editor.preferences.v1";
const CHANGE_EVENT = "nebula-editor-preferences";
const ACTIONS: EditorAction[] = ["closeEditor", "commandPalette", "debug", "definition", "find", "format", "nextEditor", "problems", "quickOpen", "references", "rename", "save", "splitEditor", "tasks", "workspaceSearch"];
const SHORTCUT = /^(?:(?:Mod|Alt|Shift)\+)*(?:[A-Z0-9]|Tab|\\|F(?:[1-9]|1[0-2]))$/;

function validShortcut(shortcut: string): boolean {
  return shortcut.length <= 40 && SHORTCUT.test(shortcut) && (shortcut.includes("+") || /^F(?:[1-9]|1[0-2])$/.test(shortcut));
}

export function normalizeEditorPreferences(value: unknown): EditorPreferences {
  if (!value || typeof value !== "object") return DEFAULT_EDITOR_PREFERENCES;
  const candidate = value as Partial<EditorPreferences>;
  const keybindings = { ...DEFAULT_EDITOR_PREFERENCES.keybindings };
  if (candidate.keybindings && typeof candidate.keybindings === "object") {
    for (const action of ACTIONS) {
      const shortcut = candidate.keybindings[action];
      if (typeof shortcut === "string" && validShortcut(shortcut)) keybindings[action] = shortcut;
    }
  }
  return {
    fontSize: [12, 13, 14, 16].includes(Number(candidate.fontSize)) ? candidate.fontSize as EditorPreferences["fontSize"] : DEFAULT_EDITOR_PREFERENCES.fontSize,
    keybindings,
    tabSize: candidate.tabSize === 4 ? 4 : 2,
    wordWrap: candidate.wordWrap === true,
  };
}

export function readEditorPreferences(): EditorPreferences {
  try {
    return normalizeEditorPreferences(JSON.parse(localStorage.getItem(STORAGE_KEY) ?? "null"));
  } catch { /* diagnostic-expected: malformed or unavailable device-local preferences fall back to safe defaults. */
    return DEFAULT_EDITOR_PREFERENCES;
  }
}

export function writeEditorPreferences(preferences: EditorPreferences): void {
  const normalized = normalizeEditorPreferences(preferences);
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(normalized));
    globalThis.dispatchEvent(new CustomEvent(CHANGE_EVENT, { detail: normalized }));
  } catch (error) {
    void logCaughtDiagnostic("interface.editor_preferences.persist_failed", "Editor preferences could not be saved on this device.", error, "code_editor");
    throw error;
  }
}

export function useEditorPreferences() {
  const [preferences, setPreferences] = useState(readEditorPreferences);
  useEffect(() => {
    const update = (event: Event) => setPreferences(normalizeEditorPreferences((event as CustomEvent).detail));
    globalThis.addEventListener(CHANGE_EVENT, update);
    return () => globalThis.removeEventListener(CHANGE_EVENT, update);
  }, []);
  const savePreferences = useCallback((next: EditorPreferences) => writeEditorPreferences(next), []);
  return { preferences, savePreferences };
}

export function shortcutFromKeyboardEvent(event: Pick<KeyboardEvent, "altKey" | "ctrlKey" | "key" | "metaKey" | "shiftKey">): string | undefined {
  const key = event.key === " " ? "Space" : event.key.length === 1 ? event.key.toUpperCase() : event.key;
  if (["Alt", "Control", "Meta", "Shift"].includes(key) || !["Tab", "\\", ..."ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789", ...Array.from({ length: 12 }, (_, index) => `F${index + 1}`)].includes(key)) return undefined;
  const modifiers = [event.metaKey || event.ctrlKey ? "Mod" : undefined, event.altKey ? "Alt" : undefined, event.shiftKey ? "Shift" : undefined].filter(Boolean);
  return modifiers.length || /^F(?:[1-9]|1[0-2])$/.test(key) ? [...modifiers, key].join("+") : undefined;
}

export function eventMatchesShortcut(event: Pick<KeyboardEvent, "altKey" | "ctrlKey" | "key" | "metaKey" | "shiftKey">, shortcut: string): boolean {
  return shortcutFromKeyboardEvent(event) === shortcut;
}

export function codeMirrorKey(shortcut: string): string {
  return shortcut.replaceAll("+", "-").replace(/-([A-Z])$/, (_, key: string) => `-${key.toLowerCase()}`);
}
