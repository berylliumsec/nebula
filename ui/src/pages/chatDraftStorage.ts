const DRAFT_PREFIX = "nebula.assistant.draft.v1";

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
