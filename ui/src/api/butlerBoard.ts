import type { ApiClient } from "./client";

export type ButlerBoardState = "waiting" | "working" | "failed" | "idle";

/** The board's sections, by goal status: running; paused, blocked or proposed; completed or cancelled. */
export type ButlerBoardSection = "active" | "idle" | "completed";
export const BOARD_SECTIONS: readonly ButlerBoardSection[] = ["active", "idle", "completed"];
export const SECTION_LABEL: Record<ButlerBoardSection, string> = { active: "Active", idle: "Idle", completed: "Completed" };

export interface ButlerBoardItem {
  sessionId: string;
  title: string;
  project: { id: string; name: string };
  section: ButlerBoardSection;
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

export type ButlerBoardCounts = Record<ButlerBoardSection, number>;

export interface ButlerBoard {
  /** Exact goal chats per section, across every project. */
  counts: ButlerBoardCounts;
  /** Active chats by turn state (and Butler's proposals as waiting); only on a poll. */
  states?: Record<Exclude<ButlerBoardState, "idle">, number>;
  /** Every project with a goal chat, with exact section counts. */
  projects: Array<{ id: string; name: string; counts: ButlerBoardCounts }>;
  items: ButlerBoardItem[];
  nextOffset?: number;
  /** A section had more chats than Core returned; the counts stay exact. */
  truncated: boolean;
}

interface WireItem {
  session_id: string;
  title: string;
  project: { id: string; name: string };
  section?: ButlerBoardSection | null;
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
  counts: ButlerBoardCounts;
  states?: Record<Exclude<ButlerBoardState, "idle">, number> | null;
  projects?: Array<{ id: string; name: string; counts: ButlerBoardCounts }> | null;
  items: WireItem[];
  next_offset?: number | null;
  truncated: boolean;
}

/** The section a goal status belongs to. */
export function sectionOfGoalStatus(status: string | undefined): ButlerBoardSection {
  if (status === "running") return "active";
  if (status === "completed" || status === "cancelled") return "completed";
  return "idle";
}

function mapItem(item: WireItem): ButlerBoardItem {
  return {
    sessionId: item.session_id,
    title: item.title,
    project: item.project,
    section: item.section ?? sectionOfGoalStatus(item.goal?.status),
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

export interface ButlerBoardQuery {
  q?: string;
  /** One section's page; without it Core answers the poll (Active and Idle). */
  section?: ButlerBoardSection;
  projectId?: string;
  offset?: number;
  limit?: number;
}

function boardQuery(options: ButlerBoardQuery): string {
  const query = new URLSearchParams();
  if (options.q?.trim()) query.set("q", options.q.trim());
  if (options.section) query.set("section", options.section);
  if (options.projectId) query.set("project_id", options.projectId);
  if (options.offset) query.set("offset", String(options.offset));
  if (options.limit) query.set("limit", String(options.limit));
  return query.size ? `?${query}` : "";
}

/** The board for a poll: undefined when Core answered 304 for ``etag``. */
export async function getButlerBoardIfChanged(
  api: ApiClient,
  options: ButlerBoardQuery,
  etag: string | undefined,
  signal?: AbortSignal,
): Promise<{ board: ButlerBoard; etag?: string } | undefined> {
  const answer = await api.requestIfChanged<WireBoard>(`butler/board${boardQuery(options)}`, etag, { signal });
  if (!answer) return undefined;
  return {
    board: {
      counts: answer.value.counts,
      states: answer.value.states ?? undefined,
      projects: answer.value.projects ?? [],
      items: answer.value.items.map(mapItem),
      nextOffset: answer.value.next_offset ?? undefined,
      truncated: answer.value.truncated,
    },
    etag: answer.etag,
  };
}

/** "project" groups by project (the default); "attention" is the Status grouping: the three sections. */
export type ButlerBoardGrouping = "attention" | "project";

export interface ButlerBoardSectionGroup {
  section: ButlerBoardSection;
  items: ButlerBoardItem[];
  /** Core's exact count while the search matches Core's, else the matched cards. */
  count: number;
}

export interface ButlerBoardGroup {
  key: string;
  /** The project's name; none for the Status grouping. */
  label?: string;
  total: number;
  sections: ButlerBoardSectionGroup[];
}

/** The key that groups Completed pages and remembered sections for the Status grouping. */
export const ALL_PROJECTS = "all";

const STATE_ORDER: ButlerBoardState[] = ["waiting", "working", "failed", "idle"];

function emptyCounts(): ButlerBoardCounts {
  return { active: 0, idle: 0, completed: 0 };
}

/**
 * Group board cards into Active, Idle and Completed. By project, each
 * project holds the three sections, projects with a chat needing the operator
 * first; the Status grouping is the three sections across projects. Active
 * lists chats needing the operator first. Completed cards come from pages
 * loaded on request (``completed`` by project ID, or ``ALL_PROJECTS``).
 * ``projects`` carries Core's exact counts; with ``coreCounts`` false (the
 * search box is ahead of Core's search) the counts are the matched cards.
 */
export function groupButlerBoard(
  items: readonly ButlerBoardItem[],
  grouping: ButlerBoardGrouping,
  options: { projects?: ButlerBoard["projects"]; completed?: Record<string, readonly ButlerBoardItem[]>; coreCounts?: boolean } = {},
): ButlerBoardGroup[] {
  const projects = options.projects ?? [];
  const completed = options.completed ?? {};
  const coreCounts = options.coreCounts ?? true;
  const rank = (item: ButlerBoardItem) => STATE_ORDER.indexOf(item.state);
  const build = (key: string, label: string | undefined, listed: readonly ButlerBoardItem[], done: readonly ButlerBoardItem[], counts: ButlerBoardCounts | undefined): ButlerBoardGroup => {
    const sections = BOARD_SECTIONS.map((section) => {
      const sectionItems = section === "completed" ? [...done] : listed.filter((item) => item.section === section);
      if (section === "active") sectionItems.sort((a, b) => rank(a) - rank(b));
      return { section, items: sectionItems, count: coreCounts && counts ? counts[section] : sectionItems.length };
    });
    return { key, label, sections, total: sections.reduce((sum, entry) => sum + entry.count, 0) };
  };
  if (grouping === "attention") {
    const totals = projects.reduce((sum, project) => {
      for (const section of BOARD_SECTIONS) sum[section] += project.counts[section];
      return sum;
    }, emptyCounts());
    const group = build(ALL_PROJECTS, undefined, items, completed[ALL_PROJECTS] ?? [], totals);
    return group.total > 0 ? [group] : [];
  }
  const names = new Map<string, string>();
  for (const project of projects) names.set(project.id, project.name);
  for (const item of items) if (!names.has(item.project.id)) names.set(item.project.id, item.project.name);
  const counts = new Map(projects.map((project) => [project.id, project.counts]));
  const waiting = (group: ButlerBoardGroup) => group.sections[0].items.filter((item) => item.state === "waiting").length
    + group.sections[1].items.filter((item) => item.state === "waiting").length;
  return [...names.entries()]
    .map(([id, name]) => build(id, name || "Untitled project", items.filter((item) => item.project.id === id), completed[id] ?? [], counts.get(id)))
    .filter((group) => group.total > 0)
    .sort((a, b) => (waiting(b) - waiting(a)) || (b.sections[0].count - a.sections[0].count) || (a.label ?? "").localeCompare(b.label ?? ""));
}

/** What a card's status says, after its section: "Needs you", "Paused", "Cancelled"… */
export function boardStatusLabel(item: Pick<ButlerBoardItem, "section" | "state" | "goal" | "attention">): string {
  if (item.section === "active") return ({ waiting: "Needs you", working: "Working", failed: "Failed", idle: "Running" } as const)[item.state];
  if (item.section === "completed") return item.goal?.status === "cancelled" ? "Cancelled" : "Completed";
  if (item.attention?.kind === "proposal") return "Proposed";
  if (item.state === "waiting") return "Needs you";
  return item.goal?.status === "blocked" ? "Blocked" : item.goal?.status === "draft" ? "Proposed" : "Paused";
}

/** Which sections are open, as this device remembers them: ``"<group>:<section>"`` → open. */
export type BoardSectionOpenState = Record<string, boolean>;

/** Completed starts collapsed; Active and Idle start open. */
export function boardSectionOpen(state: BoardSectionOpenState, groupKey: string, section: ButlerBoardSection): boolean {
  return state[`${groupKey}:${section}`] ?? section !== "completed";
}

/** Parse the remembered open sections; anything unreadable is forgotten. */
export function parseBoardSectionState(stored: string | null): BoardSectionOpenState {
  if (!stored) return {};
  try {
    const value = JSON.parse(stored) as unknown;
    if (!value || typeof value !== "object" || Array.isArray(value)) return {};
    return Object.fromEntries(Object.entries(value).filter((entry): entry is [string, boolean] => typeof entry[1] === "boolean"));
  } catch {
    return {}; // diagnostic-expected: a damaged preference falls back to the default sections
  }
}

/** Toggle one section, keeping at most ``limit`` remembered choices (the newest). */
export function toggleBoardSection(state: BoardSectionOpenState, groupKey: string, section: ButlerBoardSection, limit = 200): BoardSectionOpenState {
  const key = `${groupKey}:${section}`;
  const open = !boardSectionOpen(state, groupKey, section);
  const rest = Object.entries(state).filter(([entry]) => entry !== key);
  return Object.fromEntries([...rest.slice(Math.max(0, rest.length - limit + 1)), [key, open]]);
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

/** An idle goal the operator can resume: paused or blocked. Proposed drafts start from their card instead. */
export function isResumableIdle(item: Pick<ButlerBoardItem, "section" | "goal" | "attention">): boolean {
  return item.section === "idle" && item.attention?.kind !== "proposal" && (item.goal?.status === "paused" || item.goal?.status === "blocked");
}

/**
 * Every resumable idle goal chat Core lists, across projects or in one
 * (``projectId``), matching ``q``: not only the cards the poll returned.
 */
export async function listResumableIdle(api: ApiClient, options: { q?: string; projectId?: string } = {}, signal?: AbortSignal): Promise<ButlerBoardItem[]> {
  const found: ButlerBoardItem[] = [];
  let offset: number | undefined = 0;
  // Core pages a section by recent activity; the guard stops a misbehaving cursor.
  for (let pages = 0; offset !== undefined && pages < 500; pages += 1) {
    const answer = await getButlerBoardIfChanged(api, { q: options.q, section: "idle", projectId: options.projectId, offset, limit: 200 }, undefined, signal);
    if (!answer) break;
    found.push(...answer.board.items.filter(isResumableIdle));
    offset = answer.board.nextOffset !== undefined && answer.board.nextOffset > offset ? answer.board.nextOffset : undefined;
  }
  const seen = new Set<string>();
  return found.filter((item) => !seen.has(item.sessionId) && seen.add(item.sessionId));
}

export interface ResumeOutcome {
  sessionId: string;
  /** Core's reason when it did not resume the goal. */
  error?: string;
}

/**
 * Resume goals through Core's per-goal action, a few at a time, so Core
 * applies its usual checks (holds, budgets, recovery) to each one. A goal
 * whose revision moved since the board read is re-read and tried once more.
 */
export async function resumeIdleGoals(
  api: ApiClient,
  items: ReadonlyArray<Pick<ButlerBoardItem, "sessionId" | "goal">>,
  onProgress?: (done: number, outcome: ResumeOutcome) => void,
  concurrency = 4,
): Promise<ResumeOutcome[]> {
  const outcomes: ResumeOutcome[] = [];
  let next = 0;
  const resumeOne = async (item: Pick<ButlerBoardItem, "sessionId" | "goal">): Promise<ResumeOutcome> => {
    let revision = item.goal?.revision;
    for (let attempt = 0; attempt < 2; attempt += 1) {
      try {
        if (revision === undefined) revision = (await api.getChatGoal(item.sessionId)).revision;
        await api.writeChatGoal(item.sessionId, { expectedRevision: revision, action: "resume" });
        return { sessionId: item.sessionId };
      } catch (caught) { // diagnostic-expected: a refused resume returns Core's reason to the board, which logs the batch's refusals
        const status = (caught as { status?: unknown })?.status;
        if (attempt === 0 && status === 409 && /changed on another device/i.test(caught instanceof Error ? caught.message : "")) {
          revision = undefined;
          continue;
        }
        return { sessionId: item.sessionId, error: caught instanceof Error && caught.message ? caught.message : "Core did not resume the goal." };
      }
    }
    return { sessionId: item.sessionId, error: "The goal kept changing; open the chat to resume it." };
  };
  const worker = async () => {
    while (next < items.length) {
      const item = items[next];
      next += 1;
      const outcome = await resumeOne(item);
      outcomes.push(outcome);
      onProgress?.(outcomes.length, outcome);
    }
  };
  await Promise.all(Array.from({ length: Math.min(concurrency, items.length) }, worker));
  return outcomes;
}
