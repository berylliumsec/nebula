import { describe, expect, it } from "vitest";
import { chatDraftStorageKey, clearChatDraft, readChatDraft, writeChatDraft } from "./chatDraftStorage";

describe("assistant draft recovery", () => {
  it("isolates unsent text by project and conversation", () => {
    const storage = window.sessionStorage;
    const first = chatDraftStorageKey("project/a", "session 1");
    const second = chatDraftStorageKey("project/a", "session 2");
    writeChatDraft(storage, first, "  exact unsent text\n");
    writeChatDraft(storage, second, "another draft");

    expect(readChatDraft(storage, first)).toBe("  exact unsent text\n");
    expect(readChatDraft(storage, second)).toBe("another draft");
    clearChatDraft(storage, first);
    expect(readChatDraft(storage, first)).toBe("");
    expect(readChatDraft(storage, second)).toBe("another draft");
  });

  it("removes empty drafts instead of retaining stale values", () => {
    const key = chatDraftStorageKey("project", undefined);
    writeChatDraft(window.sessionStorage, key, "draft");
    writeChatDraft(window.sessionStorage, key, "");
    expect(window.sessionStorage.getItem(key)).toBeNull();
  });
});
