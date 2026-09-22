import type { ChatSessionSummary } from "../api/types";

/**
 * Merge a freshly read conversation list into the one on screen.
 *
 * A list read that started before a save (checking Subagents, picking an
 * effort) can answer after it. Core bumps a conversation's revision on every
 * write, so keep whichever copy is newer; replacing it with the older one
 * would undo the operator's choice on screen. The list still decides which
 * conversations exist, newest activity first.
 */
export function reconcileListedSessions(current: ChatSessionSummary[], listed: ChatSessionSummary[]): ChatSessionSummary[] {
  const shown = new Map(current.map((session) => [session.id, session]));
  return listed
    .map((session) => {
      const existing = shown.get(session.id);
      return existing && existing.revision > session.revision ? existing : session;
    })
    .sort((left, right) => right.updatedAt.localeCompare(left.updatedAt));
}

/** A completed first exchange may still be waiting on Core's optional naming task. */
export function hasRecentPendingTitle(sessions: ChatSessionSummary[], now = Date.now()): boolean {
  return sessions.some((session) =>
    (session.initialTitleState === "pending" || session.initialTitleState === undefined)
    && (session.messageCount ?? 0) >= 2
    && now - Date.parse(session.updatedAt) < 120_000,
  );
}
