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
