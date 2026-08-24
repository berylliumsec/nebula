import { beforeEach, describe, expect, it } from "vitest";
import { codeMirrorKey, DEFAULT_EDITOR_PREFERENCES, eventMatchesShortcut, normalizeEditorPreferences, readEditorPreferences, shortcutFromKeyboardEvent, writeEditorPreferences } from "./editorPreferences";

beforeEach(() => localStorage.clear());

describe("editorPreferences", () => {
  it("validates durable preferences and rejects malformed shortcuts", () => {
    const preferences = normalizeEditorPreferences({
      fontSize: 16,
      tabSize: 4,
      wordWrap: true,
      keybindings: { ...DEFAULT_EDITOR_PREFERENCES.keybindings, save: "invalid", splitEditor: "Mod+Shift+\\" },
    });
    expect(preferences).toMatchObject({ fontSize: 16, tabSize: 4, wordWrap: true });
    expect(preferences.keybindings.save).toBe("Mod+S");
    expect(preferences.keybindings.splitEditor).toBe("Mod+Shift+\\");
    writeEditorPreferences(preferences);
    expect(readEditorPreferences()).toEqual(preferences);
  });

  it("normalizes browser shortcuts and CodeMirror keys", () => {
    const event = { altKey: false, ctrlKey: true, key: "p", metaKey: false, shiftKey: true };
    expect(shortcutFromKeyboardEvent(event)).toBe("Mod+Shift+P");
    expect(eventMatchesShortcut(event, "Mod+Shift+P")).toBe(true);
    expect(codeMirrorKey("Mod+Shift+S")).toBe("Mod-Shift-s");
    expect(shortcutFromKeyboardEvent({ altKey: false, ctrlKey: false, key: "F2", metaKey: false, shiftKey: false })).toBe("F2");
    expect(eventMatchesShortcut({ altKey: false, ctrlKey: false, key: "F5", metaKey: false, shiftKey: false }, "F5")).toBe(true);
  });

  it("fills newly introduced actions when loading an older preference record", () => {
    const preferences = normalizeEditorPreferences({
      fontSize: 14,
      keybindings: { save: "Mod+Alt+S" },
    });
    expect(preferences.keybindings.save).toBe("Mod+Alt+S");
    expect(preferences.keybindings).toMatchObject({
      commandPalette: "Mod+Shift+P",
      debug: "F5",
      find: "Mod+F",
      format: "Alt+Shift+F",
      problems: "Mod+Shift+M",
      rename: "F2",
      tasks: "Mod+Shift+B",
    });
  });
});
