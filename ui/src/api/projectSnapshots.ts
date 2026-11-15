import type { ChatAttentionItem, ChatSessionActivity, ChatSessionSummary, EngagementSummary } from "./types";

export interface ProjectChatSnapshot {
  id: string;
  title: string;
  state: ChatSessionActivity["state"];
  updatedAt: string;
}

export interface ProjectSnapshot {
  id: string;
  name: string;
  description: string;
  chatCount: number;
  working: number;
  waiting: number;
  needsYou: number;
  lastActivityAt: string;
  chats: ProjectChatSnapshot[];
}

/** Ordinary operator conversations: not archived, not Butler, not delegated subagents or side chats. */
export function isProjectChat(session: ChatSessionSummary): boolean {
  return !session.archivedAt && !session.isControlAssistant && !session.isSubagent && !session.isSideChat;
}

/**
 * The iPhone app shows only projects Core marks active that hold at least one chat.
 * Projects needing the operator sort first, then running ones, then most recent.
 */
export function buildProjectSnapshots(
  projects: readonly EngagementSummary[],
  sessions: ReadonlyMap<string, readonly ChatSessionSummary[]>,
  activity: ReadonlyMap<string, readonly ChatSessionActivity[]>,
  attention: readonly ChatAttentionItem[],
  chatLimit = 5,
): ProjectSnapshot[] {
  const snapshots: ProjectSnapshot[] = [];
  for (const project of projects) {
    if (project.status !== "active") continue;
    const chats = (sessions.get(project.id) ?? []).filter(isProjectChat);
    if (!chats.length) continue;
    const states = new Map((activity.get(project.id) ?? []).map(item => [item.sessionId, item.state]));
    const ordered = [...chats].sort((left, right) => Date.parse(right.updatedAt) - Date.parse(left.updatedAt));
    const stateOf = (id: string) => states.get(id) ?? "idle";
    snapshots.push({
      id: project.id,
      name: project.name,
      description: project.description,
      chatCount: chats.length,
      working: chats.filter(chat => stateOf(chat.id) === "working").length,
      waiting: chats.filter(chat => stateOf(chat.id) === "waiting").length,
      needsYou: attention.filter(item => item.engagementId === project.id).length,
      lastActivityAt: ordered[0].updatedAt,
      chats: ordered.slice(0, chatLimit).map(chat => ({ id: chat.id, title: chat.title || "Untitled chat", state: stateOf(chat.id), updatedAt: chat.updatedAt })),
    });
  }
  const rank = (item: ProjectSnapshot) => item.needsYou > 0 ? 0 : item.working + item.waiting > 0 ? 1 : 2;
  return snapshots.sort((left, right) => rank(left) - rank(right) || Date.parse(right.lastActivityAt) - Date.parse(left.lastActivityAt));
}

export function relativeAge(iso: string, now = Date.now()): string {
  const seconds = Math.max(0, Math.round((now - Date.parse(iso)) / 1000));
  if (!Number.isFinite(seconds)) return "";
  if (seconds < 60) return "just now";
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 48) return `${hours}h ago`;
  return `${Math.round(hours / 24)}d ago`;
}
