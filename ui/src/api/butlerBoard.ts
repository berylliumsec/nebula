import type { ApiClient } from "./client";

export type ButlerBoardState = "waiting" | "working" | "failed" | "idle";

export interface ButlerBoardItem {
  sessionId: string;
  title: string;
  project: { id: string; name: string };
  state: ButlerBoardState;
  backend: "provider" | "harness";
  providerProfileId?: string;
  harnessProfileId?: string;
  model?: string;
  parentSessionId?: string;
  isSubagent: boolean;
  turnId?: string;
  turnStatus?: string;
  turnStartedAt?: string;
  updatedAt: string;
  /** One line from Core: what the chat is doing or waiting for. */
  now?: string;
  attention?: { kind: "approval" | "question" | "recovery" | "proposal"; actionId?: string; summary: string; requestedAt?: string };
  goal?: { objective: string; status: string; currentStep: number; planLength: number; revision?: number };
  /** "butler" when Butler started this chat on its own authority. */
  startedBy?: "butler";
  /** "butler" when Butler proposed this chat; a draft until the operator starts it. */
  proposedBy?: "butler";
  sessionRevision?: number;
}

export interface ButlerBoard {
  counts: Record<ButlerBoardState, number>;
  items: ButlerBoardItem[];
  nextOffset?: number;
  /** More active chats exist than Core returned; the counts stay exact. */
  truncated: boolean;
}

interface WireItem {
  session_id: string;
  title: string;
  project: { id: string; name: string };
  state: ButlerBoardState;
  backend: "provider" | "harness";
  provider_profile_id?: string | null;
  harness_profile_id?: string | null;
  model?: string | null;
  parent_session_id?: string | null;
  is_subagent: boolean;
  turn_id?: string | null;
  turn_status?: string | null;
  turn_started_at?: string | null;
  updated_at: string;
  now?: string | null;
  attention?: { kind: "approval" | "question" | "recovery" | "proposal"; action_id?: string | null; summary: string; requested_at?: string | null } | null;
  goal?: { objective: string; status: string; current_step: number; plan_length: number; revision?: number | null } | null;
  started_by?: string | null;
  proposed_by?: string | null;
  session_revision?: number | null;
}

interface WireBoard {
  counts: Record<ButlerBoardState, number>;
  items: WireItem[];
  next_offset?: number | null;
  truncated: boolean;
}

function mapItem(item: WireItem): ButlerBoardItem {
  return {
    sessionId: item.session_id,
    title: item.title,
    project: item.project,
    state: item.state,
    backend: item.backend,
    providerProfileId: item.provider_profile_id ?? undefined,
    harnessProfileId: item.harness_profile_id ?? undefined,
    model: item.model ?? undefined,
    parentSessionId: item.parent_session_id ?? undefined,
    isSubagent: item.is_subagent,
    turnId: item.turn_id ?? undefined,
    turnStatus: item.turn_status ?? undefined,
    turnStartedAt: item.turn_started_at ?? undefined,
    updatedAt: item.updated_at,
    now: item.now ?? undefined,
    attention: item.attention ? {
      kind: item.attention.kind,
      actionId: item.attention.action_id ?? undefined,
      summary: item.attention.summary,
      requestedAt: item.attention.requested_at ?? undefined,
    } : undefined,
    goal: item.goal ? {
      objective: item.goal.objective,
      status: item.goal.status,
      currentStep: item.goal.current_step,
      planLength: item.goal.plan_length,
      revision: item.goal.revision ?? undefined,
    } : undefined,
    startedBy: item.started_by === "butler" ? "butler" : undefined,
    proposedBy: item.proposed_by === "butler" ? "butler" : undefined,
    sessionRevision: item.session_revision ?? undefined,
  };
}

function boardQuery(options: { q?: string; state?: "active" | "idle"; offset?: number; limit?: number }): string {
  const query = new URLSearchParams();
  if (options.q?.trim()) query.set("q", options.q.trim());
  if (options.state === "idle") query.set("state", "idle");
  if (options.offset) query.set("offset", String(options.offset));
  if (options.limit) query.set("limit", String(options.limit));
  return query.size ? `?${query}` : "";
}

/** The board for a poll: undefined when Core answered 304 for ``etag``. */
export async function getButlerBoardIfChanged(
  api: ApiClient,
  options: { q?: string; state?: "active" | "idle"; offset?: number; limit?: number },
  etag: string | undefined,
  signal?: AbortSignal,
): Promise<{ board: ButlerBoard; etag?: string } | undefined> {
  const answer = await api.requestIfChanged<WireBoard>(`butler/board${boardQuery(options)}`, etag, { signal });
  if (!answer) return undefined;
  return {
    board: {
      counts: answer.value.counts,
      items: answer.value.items.map(mapItem),
      nextOffset: answer.value.next_offset ?? undefined,
      truncated: answer.value.truncated,
    },
    etag: answer.etag,
  };
}

export type ButlerBoardGrouping = "attention" | "project";

export interface ButlerBoardGroup {
  key: string;
  label: string;
  items: ButlerBoardItem[];
  counts: Record<ButlerBoardState, number>;
}

const STATE_LABEL: Record<Exclude<ButlerBoardState, "idle">, string> = { waiting: "Needs you", working: "Working", failed: "Failed" };
const STATE_ORDER: ButlerBoardState[] = ["waiting", "working", "failed", "idle"];

function emptyCounts(): Record<ButlerBoardState, number> {
  return { waiting: 0, working: 0, failed: 0, idle: 0 };
}

/**
 * Group board cards. Chats needing the operator always come first: by
 * attention they form the first group; by project, projects with a chat
 * needing the operator lead, and each project lists those chats first.
 * Subagent chats fold into their parent unless ``showSubagents`` is set.
 */
export function groupButlerBoard(items: ButlerBoardItem[], grouping: ButlerBoardGrouping, showSubagents: boolean): ButlerBoardGroup[] {
  const visible = showSubagents ? items : items.filter((item) => !item.isSubagent || item.state === "waiting");
  if (grouping === "attention") {
    return (["waiting", "working", "failed"] as const)
      .map((state) => {
        const groupItems = visible.filter((item) => item.state === state);
        const counts = emptyCounts();
        counts[state] = groupItems.length;
        return { key: state, label: STATE_LABEL[state], items: groupItems, counts };
      })
      .filter((group) => group.items.length > 0);
  }
  const byProject = new Map<string, ButlerBoardGroup>();
  for (const item of visible) {
    const group = byProject.get(item.project.id) ?? { key: item.project.id, label: item.project.name || "Untitled project", items: [], counts: emptyCounts() };
    group.items.push(item);
    group.counts[item.state] += 1;
    byProject.set(item.project.id, group);
  }
  const rank = (item: ButlerBoardItem) => STATE_ORDER.indexOf(item.state);
  return [...byProject.values()]
    .map((group) => ({ ...group, items: [...group.items].sort((a, b) => rank(a) - rank(b)) }))
    .sort((a, b) => (b.counts.waiting - a.counts.waiting) || (b.counts.working - a.counts.working) || a.label.localeCompare(b.label));
}

/** Compact elapsed time for a card: "45s", "12m", "3h", "2d". */
export function boardElapsed(since: string | undefined, now: number): string | undefined {
  if (!since) return undefined;
  const start = Date.parse(since);
  if (!Number.isFinite(start)) return undefined;
  const seconds = Math.max(0, Math.floor((now - start) / 1000));
  if (seconds < 60) return `${seconds}s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m`;
  if (seconds < 86_400) return `${Math.floor(seconds / 3600)}h`;
  return `${Math.floor(seconds / 86_400)}d`;
}

export interface ButlerThread {
  sessionId: string;
  title: string;
  project: { id: string; name: string };
  state: "working" | "waiting" | "idle";
  turnId?: string;
  updatedAt: string;
}

/** Butler mode threads in every project, for a poll: undefined on 304. */
export async function listButlerThreadsIfChanged(api: ApiClient, etag: string | undefined, signal?: AbortSignal): Promise<{ threads: ButlerThread[]; etag?: string } | undefined> {
  const answer = await api.requestIfChanged<{ items: Array<{ session_id: string; title: string; project: { id: string; name: string }; state: ButlerThread["state"]; turn_id?: string | null; updated_at: string }> }>("butler/threads", etag, { signal });
  if (!answer) return undefined;
  return {
    threads: answer.value.items.map((item) => ({
      sessionId: item.session_id,
      title: item.title,
      project: item.project,
      state: item.state,
      turnId: item.turn_id ?? undefined,
      updatedAt: item.updated_at,
    })),
    etag: answer.etag,
  };
}

/** Start a chat Butler proposed: its draft goal starts with the revision Core last showed. */
export async function startProposedGoalChat(api: ApiClient, sessionId: string): Promise<void> {
  const goal = await api.getChatGoal(sessionId);
  await api.writeChatGoal(sessionId, { expectedRevision: goal.revision, action: "start" });
}

/** Cancel a chat Butler proposed: Core removes the draft chat and its goal. */
export async function cancelProposedGoalChat(api: ApiClient, sessionId: string, sessionRevision: number | undefined): Promise<void> {
  await api.request<void>(`chat-sessions/${encodeURIComponent(sessionId)}`, {
    method: "DELETE",
    headers: sessionRevision !== undefined ? { "If-Match": String(sessionRevision) } : undefined,
  });
}

/** A goal chat Butler proposed or started, from its tool receipt. */
export interface GoalChatProposal {
  kind: "goal_chat_proposal" | "goal_chat_started";
  sessionId: string;
  projectId: string;
  title: string;
  objective: string;
  completionCriteria: string[];
  runtime?: { backend?: string; profileId?: string; model?: string; reasoningEffort?: string };
  limits?: { timeBudgetSeconds?: number | null; tokenBudget?: number | null; stepBudget?: number | null; allowSubagents?: boolean; maxActiveSubagents?: number | null };
  tools: string[];
  reportsTo?: { sessionId: string; title: string; kind: "butler" | "chat" };
  startError?: string;
}

export function goalChatProposalFromReceipt(receipt: Record<string, unknown> | undefined | null): GoalChatProposal | undefined {
  const action = receipt?.client_action as Record<string, any> | undefined;
  if (!action || (action.kind !== "goal_chat_proposal" && action.kind !== "goal_chat_started") || typeof action.sessionId !== "string") return undefined;
  const criteria = action.completionCriteria;
  return {
    kind: action.kind,
    sessionId: action.sessionId,
    projectId: String(action.projectId ?? ""),
    title: String(action.title ?? "Goal chat"),
    objective: String(action.objective ?? ""),
    completionCriteria: Array.isArray(criteria) ? criteria.map(String) : criteria ? [String(criteria)] : [],
    runtime: action.runtime ?? undefined,
    limits: action.limits ?? undefined,
    tools: Array.isArray(action.tools) ? action.tools.map((tool: { name?: unknown; id?: unknown }) => String(tool?.name ?? tool?.id ?? "")).filter(Boolean) : [],
    reportsTo: action.reportsTo ?? undefined,
    startError: typeof receipt?.start_error === "string" ? receipt.start_error : undefined,
  };
}
