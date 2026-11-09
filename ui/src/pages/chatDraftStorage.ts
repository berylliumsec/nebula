import { encodedBytes, type ContinuityScope } from "../api/durableRequests";

const DRAFT_PREFIX = "nebula.assistant.draft.v2";
export const CHAT_DRAFT_LIMITS = { records: 128, totalBytes: 2 * 1024 * 1024, recordBytes: 256 * 1024, ageMs: 30 * 24 * 60 * 60 * 1000 };
type Draft = { text: string; updatedAt: number };

function readRecord(storage: Storage, key: string): Draft | undefined {
  const raw = storage.getItem(key);
  if (!raw || encodedBytes(raw) > CHAT_DRAFT_LIMITS.recordBytes + 128) return undefined;
  let value: unknown;
  try { value = JSON.parse(raw); } catch {
    // diagnostic-expected: corrupt optional draft bytes must not block the composer or enter diagnostics.
    return undefined;
  }
  const record = value as Draft | null;
  return record && typeof record.text === "string" && encodedBytes(record.text) <= CHAT_DRAFT_LIMITS.recordBytes
    && Number.isSafeInteger(record.updatedAt) && record.updatedAt <= Date.now()
    && record.updatedAt >= Date.now() - CHAT_DRAFT_LIMITS.ageMs ? record : undefined;
}

/**
 * Keeps the current tab's drafts authoritative even when browser storage is
 * unavailable or a conversation switch happens before React flushes effects.
 */
export class ChatDraftStore {
  private readonly drafts = new Map<string, string>();

  constructor(private readonly storage: Storage, private readonly onLimitation?: (message: string) => void) {}

  private remember(key: string, value: string): void {
    this.drafts.delete(key);
    if (encodedBytes(value) > CHAT_DRAFT_LIMITS.recordBytes) return;
    this.drafts.set(key, value);
    let total = [...this.drafts.values()].reduce((sum, item) => sum + encodedBytes(item), 0);
    while (this.drafts.size > CHAT_DRAFT_LIMITS.records || total > CHAT_DRAFT_LIMITS.totalBytes) {
      const oldest = this.drafts.keys().next().value;
      if (oldest === undefined) break;
      total -= encodedBytes(this.drafts.get(oldest) ?? "");
      this.drafts.delete(oldest);
    }
  }

  read(key: string): string {
    if (this.drafts.has(key)) return this.drafts.get(key) ?? "";
    const value = readChatDraft(this.storage, key);
    this.remember(key, value);
    return value;
  }

  write(key: string, value: string): void {
    this.remember(key, value);
    if (!writeChatDraft(this.storage, key, value)) this.onLimitation?.("This draft could not be saved on this device. Keep this window open or copy your text before closing it.");
  }

  clear(key: string): void {
    this.drafts.delete(key);
    clearChatDraft(this.storage, key);
  }
}

export function chatDraftStorageKey(engagementId: string, sessionId?: string, scope?: string): string {
  return `${DRAFT_PREFIX}:${encodeURIComponent(scope ?? "unscoped")}:${encodeURIComponent(engagementId)}:${encodeURIComponent(sessionId || "new")}`;
}

export function readChatDraft(storage: Storage, key: string): string {
  try {
    return readRecord(storage, key)?.text ?? "";
  } catch {
    // diagnostic-expected: constrained browsers may deny session storage.
    return "";
  }
}

export function writeChatDraft(storage: Storage, key: string, value: string): boolean {
  try {
    if (!value) { storage.removeItem(key); return true; }
    if (encodedBytes(value) > CHAT_DRAFT_LIMITS.recordBytes) return false;
    const records: { key: string; record: Draft }[] = [];
    for (let index = 0; index < storage.length; index++) {
      const candidate = storage.key(index);
      if (!candidate?.startsWith(`${DRAFT_PREFIX}:`) || candidate === key) continue;
      const record = readRecord(storage, candidate);
      if (record) records.push({ key: candidate, record });
    }
    records.sort((left, right) => left.record.updatedAt - right.record.updatedAt);
    let total = encodedBytes(value) + records.reduce((sum, item) => sum + encodedBytes(item.record.text), 0);
    while (records.length >= CHAT_DRAFT_LIMITS.records || total > CHAT_DRAFT_LIMITS.totalBytes) {
      const oldest = records.shift();
      if (!oldest) break;
      total -= encodedBytes(oldest.record.text);
      storage.removeItem(oldest.key);
    }
    const retained = new Set(records.map(item => item.key));
    for (let index = storage.length - 1; index >= 0; index--) {
      const candidate = storage.key(index);
      if (candidate?.startsWith(`${DRAFT_PREFIX}:`) && candidate !== key && !retained.has(candidate)) storage.removeItem(candidate);
    }
    storage.setItem(key, JSON.stringify({text: value, updatedAt: Date.now()} satisfies Draft));
    return true;
  } catch {
    // diagnostic-expected: draft recovery must not block the composer.
    // Draft recovery is a device-local convenience. Chat remains usable when
    // storage is disabled, full, or unavailable on a constrained LAN browser.
    return false;
  }
}

const volatileStores = new Map<string, Storage>();
/** Identity-less servers never read another Core or user's persisted text. */
export function chatStorageForScope(scope: ContinuityScope): Storage {
  if (scope.persistent) {
    try { return window.localStorage; } catch { /* diagnostic-expected: use a volatile fallback. */ }
  }
  const existing = volatileStores.get(scope.key);
  if (existing) return existing;
  const items = new Map<string, string>();
  const storage: Storage = {
    get length() { return items.size; },
    key: index => [...items.keys()][index] ?? null,
    getItem: key => items.get(key) ?? null,
    setItem: (key, value) => {
      if (scope.persistent) throw new Error("Browser storage is unavailable");
      items.set(key, value);
    },
    removeItem: key => { items.delete(key); },
    clear: () => { items.clear(); },
  };
  volatileStores.set(scope.key, storage);
  if (volatileStores.size > 8) volatileStores.delete(volatileStores.keys().next().value!);
  return storage;
}

export function clearChatDraft(storage: Storage, key: string): void {
  try {
    storage.removeItem(key);
  } catch {
    // diagnostic-expected: draft cleanup must not block session deletion.
    // See writeChatDraft: storage failure must not block the composer.
  }
}
