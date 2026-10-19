import type { ChatSessionSummary } from "../api/types";

type ActivityState = "working" | "waiting" | "idle";

export interface ConversationSidebarRow {
  session: ChatSessionSummary;
  depth: 0 | 1 | 2;
  childCount: number;
  childKind: "chats" | "subagents";
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
  const subagents = new Map<string, ChatSessionSummary[]>();
  const butlerChats = new Map<string, ChatSessionSummary[]>();
  const attached = new Set<string>();

  for (const session of sessions) {
    if (!session.isSubagent || !session.parentSessionId) continue;
    const parent = byId.get(session.parentSessionId);
    if (!parent || parent.isSubagent) continue;
    subagents.set(parent.id, [...(subagents.get(parent.id) ?? []), session]);
    attached.add(session.id);
  }
  // Display ownership stays separate from transcript and fork lineage. A
  // Butler-created main chat can still own its own subagent conversations.
  const butlerByProject = new Map<string, ChatSessionSummary>();
  for (const session of sessions) {
    if (!session.isControlAssistant) continue;
    const current = butlerByProject.get(session.engagementId);
    if (!current || (current.archivedAt && !session.archivedAt)
      || (Boolean(current.archivedAt) === Boolean(session.archivedAt) && Date.parse(session.updatedAt) > Date.parse(current.updatedAt))) {
      butlerByProject.set(session.engagementId, session);
    }
  }
  for (const session of sessions) {
    if (!session.isButlerInitiated || session.isControlAssistant || session.isSubagent) continue;
    const butler = butlerByProject.get(session.engagementId);
    if (!butler || butler.id === session.id) continue;
    butlerChats.set(butler.id, [...(butlerChats.get(butler.id) ?? []), session]);
    attached.add(session.id);
  }

  const startOfToday = new Date(now);
  startOfToday.setHours(0, 0, 0, 0);
  const weekAgo = now - 7 * 24 * 60 * 60 * 1_000;
  const descendants = (session: ChatSessionSummary): ChatSessionSummary[] => {
    const chats = butlerChats.get(session.id) ?? [];
    return [
      ...(subagents.get(session.id) ?? []),
      ...chats,
      ...chats.flatMap(chat => subagents.get(chat.id) ?? []),
    ];
  };
  const newestUpdate = (session: ChatSessionSummary) => Math.max(
    ...[session, ...descendants(session)].map(item => Date.parse(item.updatedAt) || 0),
  );
  const groups = new Map<string, ConversationSidebarGroup>();
  const roots = sessions.filter(session => !attached.has(session.id))
    .sort((a, b) => newestUpdate(b) - newestUpdate(a));
  for (const session of roots) {
    const chats = butlerChats.get(session.id) ?? [];
    const children = [...chats, ...(subagents.get(session.id) ?? [])];
    const childKind = chats.length ? "chats" : "subagents";
    const allDescendants = descendants(session);
    const matchingDescendants = allDescendants.filter(matches);
    const parentMatches = matches(session);
    if (query && !parentMatches && !matchingDescendants.length) continue;
    const waitingChildren = children.filter(child => activity[child.id] === "waiting").length;
    const workingChildren = children.filter(child => activity[child.id] === "working"
      || (subagents.get(child.id) ?? []).some(agent => ["working", "waiting"].includes(activity[agent.id]))).length;
    const childWorking = allDescendants.some(child => ["working", "waiting"].includes(activity[child.id]));
    const label = activity[session.id] === "waiting" || (childKind === "chats" && waitingChildren) ? "Needs you"
      : session.archivedAt ? "Archived"
        : activity[session.id] === "working" || childWorking ? "Working"
          : newestUpdate(session) >= startOfToday.getTime() ? "Today"
            : newestUpdate(session) >= weekAgo ? "Previous 7 days" : "Older";
    const searchRevealed = Boolean(query && matchingDescendants.length);
    const expanded = expandedParents.has(session.id) || searchRevealed;
    const group = groups.get(label) ?? { label, rows: [], rootCount: 0 };
    group.rootCount += 1;
    group.rows.push({ session, depth: 0, childCount: children.length, childKind, waitingChildren, workingChildren, expanded, searchRevealed });
    if (expanded) {
      const visibleChildren = query && !parentMatches
        ? children.filter(child => matches(child) || (subagents.get(child.id) ?? []).some(matches))
        : children;
      for (const child of visibleChildren) {
        const grandChildren = chats.length ? subagents.get(child.id) ?? [] : [];
        const matchingGrandChildren = grandChildren.filter(matches);
        const childExpanded = expandedParents.has(child.id) || Boolean(query && matchingGrandChildren.length);
        group.rows.push({
          session: child, depth: 1, childCount: grandChildren.length, childKind: "subagents",
          waitingChildren: grandChildren.filter(agent => activity[agent.id] === "waiting").length,
          workingChildren: grandChildren.filter(agent => activity[agent.id] === "working").length,
          expanded: childExpanded, searchRevealed: Boolean(query && matchingGrandChildren.length),
        });
        if (childExpanded) {
          for (const agent of query && !matches(child) ? matchingGrandChildren : grandChildren) {
            group.rows.push({ session: agent, depth: 2, childCount: 0, childKind: "subagents", waitingChildren: 0, workingChildren: 0, expanded: false, searchRevealed: false });
          }
        }
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
