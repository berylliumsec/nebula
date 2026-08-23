import { describe, expect, it } from "vitest";
import { readConversationPanelOpen, writeConversationPanelOpen } from "./workbenchPreferences";

function storageWith(entries: Record<string, string> = {}) {
  const values = new Map(Object.entries(entries));
  return {
    getItem: (key: string) => values.get(key) ?? null,
    setItem: (key: string, value: string) => { values.set(key, value); },
    removeItem: (key: string) => { values.delete(key); },
    values,
  };
}

describe("Workbench conversation preferences", () => {
  it("keeps the conversation pane closed on first use", () => {
    const storage = storageWith();
    expect(readConversationPanelOpen(storage)).toBe(false);
    expect(storage.values.has("nebula.conversations.expanded")).toBe(false);
  });

  it("migrates the legacy expanded preference to the new open preference", () => {
    const storage = storageWith({ "nebula.conversations.expanded": "true" });
    expect(readConversationPanelOpen(storage)).toBe(true);
    expect(storage.values.get("nebula.conversations.open")).toBe("true");
    expect(storage.values.has("nebula.conversations.expanded")).toBe(false);
  });

  it("persists the operator's explicit pane choice", () => {
    const storage = storageWith();
    writeConversationPanelOpen(storage, true);
    expect(readConversationPanelOpen(storage)).toBe(true);
    writeConversationPanelOpen(storage, false);
    expect(readConversationPanelOpen(storage)).toBe(false);
  });
});
