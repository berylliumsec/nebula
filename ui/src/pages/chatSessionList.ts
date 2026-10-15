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

/** Refresh the recent page without discarding older pages the operator loaded. */
export function mergeRecentListedSessions(current: ChatSessionSummary[], recent: ChatSessionSummary[], exhaustive = false): ChatSessionSummary[] {
  if (exhaustive) return reconcileListedSessions(current, recent);
  const merged = new Map(current.map((session) => [session.id, session]));
  for (const session of recent) {
    const existing = merged.get(session.id);
    if (!existing || existing.revision <= session.revision) merged.set(session.id, session);
  }
  return [...merged.values()].sort((left, right) => right.updatedAt.localeCompare(left.updatedAt));
}

/** A durable first message may still be waiting on Core's optional naming task. */
export function hasRecentPendingTitle(sessions: ChatSessionSummary[], now = Date.now()): boolean {
  return sessions.some((session) =>
    (session.initialTitleState === "pending" || session.initialTitleState === undefined)
    && (session.messageCount ?? 0) >= 1
    && now - Date.parse(session.updatedAt) < 120_000,
  );
}
