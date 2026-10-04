export interface ProjectWorkItem {
  id: string;
  engagement_id: string;
  title: string;
  status: string;
  priority: string;
  assignee_session_id: string | null;
  source_kind: string;
  source_id: string | null;
  last_update_at: string | null;
}

export interface ProjectWorkUpdate {
  id: string;
  engagement_id: string;
  item_id: string;
  summary: string;
  next_step: string | null;
  blocker: string | null;
  created_at: string;
  source_session_id: string | null;
  source_engagement_id: string | null;
}

export function activeProjectWorkItems<T extends ProjectWorkItem>(items: T[]): T[] {
  return items.filter((item) => item.status !== "done").sort((left, right) => {
    const recent = Date.parse(right.last_update_at ?? "") - Date.parse(left.last_update_at ?? "");
    if (Number.isFinite(recent) && recent !== 0) return recent;
    if (left.last_update_at && !right.last_update_at) return -1;
    if (right.last_update_at && !left.last_update_at) return 1;
    return left.id.localeCompare(right.id);
  });
}

/** The current summary must come from a saved check-in on an unfinished item. */
export function currentProjectWorkItem<T extends ProjectWorkItem>(items: T[], sessionId?: string): T | undefined {
  const active = activeProjectWorkItems(items).filter((item) => item.last_update_at);
  if (!sessionId) return active[0];
  return active.find((item) => item.assignee_session_id === sessionId
    || (item.source_kind === "chat" && item.source_id === sessionId)) ?? active[0];
}
