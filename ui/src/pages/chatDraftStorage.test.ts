import { describe, expect, it } from "vitest";
import { CHAT_DRAFT_LIMITS, ChatDraftStore, chatDraftStorageKey, clearChatDraft, readChatDraft, writeChatDraft } from "./chatDraftStorage";

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

  it("keeps independent drafts in memory when browser storage is unavailable", () => {
    const unavailable = {
      getItem: () => { throw new DOMException("Storage blocked", "SecurityError"); },
      setItem: () => { throw new DOMException("Storage blocked", "SecurityError"); },
      removeItem: () => { throw new DOMException("Storage blocked", "SecurityError"); },
    } as unknown as Storage;
    const drafts = new ChatDraftStore(unavailable);
    const unattached = chatDraftStorageKey("project", undefined);
    const saved = chatDraftStorageKey("project", "saved-chat");

    drafts.write(unattached, "unsent new-chat draft");
    drafts.write(saved, "saved-chat follow-up");

    expect(drafts.read(unattached)).toBe("unsent new-chat draft");
    expect(drafts.read(saved)).toBe("saved-chat follow-up");
    drafts.clear(unattached);
    expect(drafts.read(unattached)).toBe("");
    expect(drafts.read(saved)).toBe("saved-chat follow-up");
  });

  it("restores across store instances only in the exact Core and principal scope", () => {
    const first = chatDraftStorageKey("project", "session", "core-a:operator");
    const otherUser = chatDraftStorageKey("project", "session", "core-a:device-two");
    const otherCore = chatDraftStorageKey("project", "session", "core-b:operator");
    new ChatDraftStore(localStorage).write(first, "Keep after close");
    const reopened = new ChatDraftStore(localStorage);
    expect(reopened.read(first)).toBe("Keep after close");
    expect(reopened.read(otherUser)).toBe("");
    expect(reopened.read(otherCore)).toBe("");
    localStorage.setItem("nebula.assistant.draft.v1:project:session", "ambiguous legacy text");
    expect(reopened.read(otherCore)).toBe("");
  });

  it("enforces UTF-8 per-draft limits without replacing the saved text", () => {
    const key = chatDraftStorageKey("project", "session", "core:operator");
    writeChatDraft(localStorage, key, "saved");
    expect(writeChatDraft(localStorage, key, "🙂".repeat(CHAT_DRAFT_LIMITS.recordBytes / 4 + 1))).toBe(false);
    expect(readChatDraft(localStorage, key)).toBe("saved");
  });

  it("retains at most 128 draft records", () => {
    for (let index = 0; index <= CHAT_DRAFT_LIMITS.records; index++) writeChatDraft(localStorage, chatDraftStorageKey("project", String(index), "scope"), String(index));
    expect(localStorage.length).toBe(CHAT_DRAFT_LIMITS.records);
    expect(readChatDraft(localStorage, chatDraftStorageKey("project", "0", "scope"))).toBe("");
    expect(readChatDraft(localStorage, chatDraftStorageKey("project", "128", "scope"))).toBe("128");
  });

  it("caps total text bytes independently of the record count", () => {
    for (let index = 0; index < 9; index++) writeChatDraft(localStorage, chatDraftStorageKey("project", String(index), "scope"), "x".repeat(CHAT_DRAFT_LIMITS.recordBytes));
    expect(localStorage.length).toBe(8);
    expect(readChatDraft(localStorage, chatDraftStorageKey("project", "0", "scope"))).toBe("");
  });

  it("does not restore expired drafts and prunes them on the next write", () => {
    const key = chatDraftStorageKey("project", "expired", "scope");
    localStorage.setItem(key, JSON.stringify({text: "old", updatedAt: Date.now() - CHAT_DRAFT_LIMITS.ageMs - 1}));
    expect(readChatDraft(localStorage, key)).toBe("");
    writeChatDraft(localStorage, chatDraftStorageKey("project", "current", "scope"), "new");
    expect(localStorage.getItem(key)).toBeNull();
  });
});
