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
