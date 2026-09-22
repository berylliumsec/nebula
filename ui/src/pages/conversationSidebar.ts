import type { ChatSessionSummary } from "../api/types";

type ActivityState = "working" | "waiting" | "idle";

export interface ConversationSidebarRow {
  session: ChatSessionSummary;
  depth: 0 | 1;
  childCount: number;
  waitingChildren: number;
  workingChildren: number;
  expanded: boolean;
  searchRevealed: boolean;
}

export interface ConversationSidebarGroup {
  label: string;
  rows: ConversationSidebarRow[];
  rootCount: number;
}

export function groupSidebarConversations(
  sessions: ChatSessionSummary[],
  activity: Record<string, ActivityState>,
  search: string,
  expandedParents: ReadonlySet<string>,
  now = Date.now(),
): { groups: ConversationSidebarGroup[]; matchCount: number } {
  const query = search.trim().toLocaleLowerCase();
  const matches = (session: ChatSessionSummary) => !query || [
    session.title,
    session.model,
    session.backend === "harness" ? "agent harness" : "provider",
  ].some(value => value?.toLocaleLowerCase().includes(query));
  const matchCount = sessions.filter(matches).length;
  const byId = new Map(sessions.map(session => [session.id, session]));
  const children = new Map<string, ChatSessionSummary[]>();
  const attached = new Set<string>();
  for (const session of sessions) {
    if (!session.isSubagent || !session.parentSessionId) continue;
    const parent = byId.get(session.parentSessionId);
    if (!parent || parent.isSubagent) continue;
    children.set(parent.id, [...(children.get(parent.id) ?? []), session]);
    attached.add(session.id);
  }

  const startOfToday = new Date(now);
  startOfToday.setHours(0, 0, 0, 0);
  const weekAgo = now - 7 * 24 * 60 * 60 * 1_000;
  const groups = new Map<string, ConversationSidebarGroup>();
  for (const session of sessions) {
    if (attached.has(session.id)) continue;
    const childSessions = children.get(session.id) ?? [];
    const matchingChildren = childSessions.filter(matches);
    const parentMatches = matches(session);
    if (query && !parentMatches && !matchingChildren.length) continue;
    const waitingChildren = childSessions.filter(child => activity[child.id] === "waiting").length;
    const workingChildren = childSessions.filter(child => activity[child.id] === "working").length;
    const states = [activity[session.id], ...childSessions.map(child => activity[child.id])];
    const newestUpdate = Math.max(...[session, ...childSessions].map(item => Date.parse(item.updatedAt) || 0));
    const label = states.includes("waiting") ? "Needs you"
      : session.archivedAt ? "Archived"
        : states.includes("working") ? "Working"
          : newestUpdate >= startOfToday.getTime() ? "Today"
            : newestUpdate >= weekAgo ? "Previous 7 days" : "Older";
    const expanded = expandedParents.has(session.id) || Boolean(query && matchingChildren.length);
    const group = groups.get(label) ?? { label, rows: [], rootCount: 0 };
    group.rootCount += 1;
    group.rows.push({ session, depth: 0, childCount: childSessions.length, waitingChildren, workingChildren, expanded, searchRevealed: Boolean(query && matchingChildren.length) });
    if (expanded) {
      for (const child of query && !parentMatches ? matchingChildren : childSessions) {
        group.rows.push({ session: child, depth: 1, childCount: 0, waitingChildren: 0, workingChildren: 0, expanded: false, searchRevealed: false });
      }
    }
    groups.set(label, group);
  }
  return {
    groups: ["Needs you", "Working", "Today", "Previous 7 days", "Older", "Archived"]
      .flatMap(label => groups.has(label) ? [groups.get(label)!] : []),
    matchCount,
  };
}
