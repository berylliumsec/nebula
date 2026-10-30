import type { ApiClient } from "../api/client";
import type { ChatSessionSummary } from "../api/types";

export type ChatBulkAction = "archive" | "restore" | "pin" | "unpin" | "clear-flag" | "delete"
  | `flag:${NonNullable<ChatSessionSummary["flagColor"]>}`;

export function conversationMatchesSearch(session: ChatSessionSummary, search: string): boolean {
  const query = search.trim().toLocaleLowerCase();
  return !query || [
    session.title,
    session.model,
    session.backend === "harness" ? "agent harness" : "provider",
  ].some(value => value?.toLocaleLowerCase().includes(query));
}

function hasDesiredState(session: ChatSessionSummary | undefined, action: ChatBulkAction): boolean {
  if (action === "delete") return !session;
  if (!session) return false;
  if (action === "archive") return Boolean(session.archivedAt);
  if (action === "restore") return !session.archivedAt;
  if (action === "pin") return Boolean(session.pinned);
  if (action === "unpin") return !session.pinned;
  if (action === "clear-flag") return !session.flagColor;
  return session.flagColor === action.slice(5);
}

export interface ChatBulkResult {
  sessions: ChatSessionSummary[];
  succeededIds: string[];
  failures: { id: string; title: string; error: unknown }[];
}

/** Re-read revisions before writes and verify every outcome against Core afterward. */
export async function applyChatBulkAction(
  api: Pick<ApiClient, "listChatSessions" | "setChatSessionArchived" | "setChatSessionPriority" | "deleteChatSession">,
  projectId: string,
  ids: readonly string[],
  action: ChatBulkAction,
): Promise<ChatBulkResult> {
  const before = (await api.listChatSessions(projectId, undefined, true)).items;
  const beforeById = new Map(before.map(session => [session.id, session]));
  const errors = new Map<string, unknown>();
  let next = 0;
  await Promise.all(Array.from({ length: Math.min(4, ids.length) }, async () => {
    while (next < ids.length) {
      const id = ids[next++];
      const session = beforeById.get(id);
      if (hasDesiredState(session, action)) continue;
      if (!session || session.engagementId !== projectId || session.isSideChat) {
        errors.set(id, new Error("Conversation is no longer available in this project."));
        continue;
      }
      try {
        if (action === "delete") await api.deleteChatSession(id);
        else if (action === "archive" || action === "restore") {
          await api.setChatSessionArchived(id, action === "archive", session.revision);
        } else {
          await api.setChatSessionPriority(id, action === "pin" ? { pinned: true }
            : action === "unpin" ? { pinned: false }
              : { flagColor: action === "clear-flag" ? null : action.slice(5) as ChatSessionSummary["flagColor"] }, session.revision);
        }
      } catch (error) {
        // diagnostic-expected: Core readback decides whether this was a lost response;
        // the caller records a diagnostic only for outcomes that actually failed.
        // The response may be lost after Core commits. Readback below decides.
        errors.set(id, error);
      }
    }
  }));
  const sessions = (await api.listChatSessions(projectId, undefined, true)).items;
  const afterById = new Map(sessions.map(session => [session.id, session]));
  const succeededIds: string[] = [];
  const failures: ChatBulkResult["failures"] = [];
  for (const id of ids) {
    const saved = afterById.get(id);
    if (hasDesiredState(saved, action)) succeededIds.push(id);
    else failures.push({ id, title: saved?.title ?? beforeById.get(id)?.title ?? "Conversation", error: errors.get(id) ?? new Error("Core did not save the requested change.") });
  }
  return { sessions, succeededIds, failures };
}
