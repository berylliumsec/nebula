export type ChatFollowUpStatus = "queued" | "sending" | "failed";

export interface ChatFollowUp {
  id: string;
  text: string;
  createdAt: string;
  status: ChatFollowUpStatus;
  detail?: string;
}

const FOLLOW_UP_PREFIX = "nebula.assistant.follow-up.v1";
const MAX_FOLLOW_UPS = 20;
const MAX_FOLLOW_UP_LENGTH = 32_000;

export function chatFollowUpStorageKey(engagementId: string, sessionId?: string): string {
  return `${FOLLOW_UP_PREFIX}:${encodeURIComponent(engagementId)}:${encodeURIComponent(sessionId || "new")}`;
}

function isFollowUp(value: unknown): value is ChatFollowUp {
  if (!value || typeof value !== "object") return false;
  const candidate = value as Partial<ChatFollowUp>;
  return typeof candidate.id === "string"
    && typeof candidate.text === "string"
    && typeof candidate.createdAt === "string"
    && (candidate.status === "queued" || candidate.status === "sending" || candidate.status === "failed");
}

/**
 * Recover a queue after a browser reload without replaying an item that may
 * already have reached Core. A sending item therefore needs operator review.
 */
export function readChatFollowUps(storage: Storage, key: string): ChatFollowUp[] {
  try {
    const raw = storage.getItem(key);
    if (!raw) return [];
    const parsed: unknown = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    return parsed
      .filter(isFollowUp)
      .slice(0, MAX_FOLLOW_UPS)
      .map((item) => item.status === "sending"
        ? {
          ...item,
          status: "failed" as const,
          detail: "The browser was reloaded while this message was being sent. Review it before retrying.",
        }
        : item);
  } catch {
    // diagnostic-expected: queue recovery must not block the composer when
    // constrained browsers deny, corrupt, or fill session storage.
    return [];
  }
}

export function writeChatFollowUps(storage: Storage, key: string, items: ChatFollowUp[]): void {
  try {
    if (!items.length) {
      storage.removeItem(key);
      return;
    }
    storage.setItem(key, JSON.stringify(items.slice(0, MAX_FOLLOW_UPS)));
  } catch {
    // diagnostic-expected: queue persistence is a recovery convenience and
    // must never prevent the active assistant from continuing.
  }
}

export function clearChatFollowUps(storage: Storage, key: string): void {
  try {
    storage.removeItem(key);
  } catch {
    // diagnostic-expected: cleanup must not block conversation navigation.
  }
}

export function validateChatFollowUpText(text: string): string | undefined {
  const normalized = text.trim();
  if (!normalized) return "Write a message before queueing it.";
  if (normalized.length > MAX_FOLLOW_UP_LENGTH) {
    return `Queued messages are limited to ${MAX_FOLLOW_UP_LENGTH.toLocaleString()} characters.`;
  }
  return undefined;
}

export function maxChatFollowUps(): number {
  return MAX_FOLLOW_UPS;
}
