const DRAFT_PREFIX = "nebula.assistant.draft.v1";

/**
 * Keeps the current tab's drafts authoritative even when sessionStorage is
 * unavailable or a conversation switch happens before React flushes effects.
 */
export class ChatDraftStore {
  private readonly drafts = new Map<string, string>();

  constructor(private readonly storage: Storage) {}

  read(key: string): string {
    if (this.drafts.has(key)) return this.drafts.get(key) ?? "";
    const value = readChatDraft(this.storage, key);
    this.drafts.set(key, value);
    return value;
  }

  write(key: string, value: string): void {
    this.drafts.set(key, value);
    writeChatDraft(this.storage, key, value);
  }

  clear(key: string): void {
    this.drafts.set(key, "");
    clearChatDraft(this.storage, key);
  }
}

export function chatDraftStorageKey(engagementId: string, sessionId?: string): string {
  return `${DRAFT_PREFIX}:${encodeURIComponent(engagementId)}:${encodeURIComponent(sessionId || "new")}`;
}

export function readChatDraft(storage: Storage, key: string): string {
  try {
    return storage.getItem(key) ?? "";
  } catch {
    // diagnostic-expected: constrained browsers may deny session storage.
    return "";
  }
}

export function writeChatDraft(storage: Storage, key: string, value: string): void {
  try {
    if (value) storage.setItem(key, value);
    else storage.removeItem(key);
  } catch {
    // diagnostic-expected: draft recovery must not block the composer.
    // Draft recovery is a device-local convenience. Chat remains usable when
    // storage is disabled, full, or unavailable on a constrained LAN browser.
  }
}

export function clearChatDraft(storage: Storage, key: string): void {
  try {
    storage.removeItem(key);
  } catch {
    // diagnostic-expected: draft cleanup must not block session deletion.
    // See writeChatDraft: storage failure must not block the composer.
  }
}
