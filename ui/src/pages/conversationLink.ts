import { ApiError } from "../api/client";
import type { ChatSessionSummary, EngagementSummary } from "../api/types";

/**
 * What a `?session=` deep link could not open, or that it is still being
 * resolved. Core owns which project a conversation belongs to; the link only
 * names the conversation.
 */
export type ConversationLinkState =
  | { sessionId: string; status: "resolving" }
  | { sessionId: string; status: "missing" }
  | { sessionId: string; status: "forbidden" }
  | { sessionId: string; status: "archived"; project: EngagementSummary; restoreError?: string }
  | { sessionId: string; status: "unknown_project" }
  | { sessionId: string; status: "failed"; message: string };

export type ConversationLinkTarget =
  | { kind: "current" }
  | { kind: "project"; projectId: string }
  | { kind: "unavailable"; state: ConversationLinkState };

/** Where Core says a linked conversation lives, relative to the open project. */
export function conversationLinkTarget(
  session: Pick<ChatSessionSummary, "id" | "engagementId">,
  currentProjectId: string,
  activeProjects: readonly Pick<EngagementSummary, "id">[],
  archivedProjects: readonly EngagementSummary[],
): ConversationLinkTarget {
  if (session.engagementId === currentProjectId) return { kind: "current" };
  if (activeProjects.some(project => project.id === session.engagementId)) {
    return { kind: "project", projectId: session.engagementId };
  }
  const archived = archivedProjects.find(project => project.id === session.engagementId);
  if (archived) return { kind: "unavailable", state: { sessionId: session.id, status: "archived", project: archived } };
  return { kind: "unavailable", state: { sessionId: session.id, status: "unknown_project" } };
}

/** The recovery state for a conversation Core could not return. */
export function conversationLinkFailure(sessionId: string, error: unknown): ConversationLinkState {
  if (error instanceof ApiError && error.status === 404) return { sessionId, status: "missing" };
  if (error instanceof ApiError && (error.status === 401 || error.status === 403)) return { sessionId, status: "forbidden" };
  return {
    sessionId,
    status: "failed",
    message: error instanceof Error && error.message ? error.message : "Nebula Core did not answer.",
  };
}
