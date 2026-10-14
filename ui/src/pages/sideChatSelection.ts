import type { ChatSessionSummary } from "../api/types";

/** The Core list is newest first; an exact link wins only within its parent. */
export function sideChatForConversation(
  sideChats: ChatSessionSummary[],
  parentId: string,
  requestedId?: string | null,
): ChatSessionSummary | undefined {
  const owned = sideChats.filter(item => item.isSideChat && item.parentSessionId === parentId);
  return owned.find(item => item.id === requestedId) ?? owned[0];
}

/** Stable tab order even when a reply changes a side chat's updated time. */
export function sideChatsForConversation(sideChats: ChatSessionSummary[], parentId: string): ChatSessionSummary[] {
  return sideChats
    .filter(item => item.isSideChat && item.parentSessionId === parentId)
    .sort((left, right) => left.createdAt.localeCompare(right.createdAt) || left.id.localeCompare(right.id));
}

function savedSideChatNumber(sideChat: ChatSessionSummary): number | undefined {
  const match = /^Side chat #([1-9]\d*) · /.exec(sideChat.title);
  const number = match ? Number(match[1]) : undefined;
  return number && Number.isSafeInteger(number) ? number : undefined;
}

export function nextSideChatNumber(sideChats: ChatSessionSummary[], parentId: string): number {
  const owned = sideChatsForConversation(sideChats, parentId);
  return Math.max(0, ...owned.map(item => savedSideChatNumber(item) ?? 1)) + 1;
}

export function sideChatTabLabel(sideChat: ChatSessionSummary, legacyIndex: number): string {
  return `Side ${savedSideChatNumber(sideChat) ?? legacyIndex + 1}`;
}
