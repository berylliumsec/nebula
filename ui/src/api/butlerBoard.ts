import type { ApiClient } from "./client";
import { parseButlerCaseStatus, type ButlerCaseStatus } from "../butlerStage";
import { parseChatWorkStage } from "../pages/workStages";
import type { ChatWorkStage } from "./types";

export type ButlerBoardState = "waiting" | "working" | "failed" | "idle";

/** The board's sections, by goal status: running; paused, blocked or proposed; completed or cancelled. */
export type ButlerBoardSection = "active" | "idle" | "completed";
export const BOARD_SECTIONS: readonly ButlerBoardSection[] = ["active", "idle", "completed"];
export const SECTION_LABEL: Record<ButlerBoardSection, string> = { active: "Active", idle: "Idle", completed: "Completed" };
/** What the board shows: Core's sections plus Held, the chats set aside by a chat hold. */
export type BoardViewSection = ButlerBoardSection | "held";
export const VIEW_SECTION_LABEL: Record<BoardViewSection, string> = { ...SECTION_LABEL, held: "Held" };

/** The chat hold that set a card's chat aside. */
export interface BoardChatHold {
  id: string;
  batchId?: string;
  reason: string;
  createdAt: string;
}

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
  goal?: { objective: string; status: string; currentStep: number; planLength: number; revision?: number; modelRefusal?: BoardModelRefusal };
  /** "butler" when Butler started this chat on its own authority. */
  startedBy?: "butler";
  /** "butler" when Butler proposed this chat; a draft until the operator starts it. */
  proposedBy?: "butler";
  sessionRevision?: number;
  /** Operator-set: this chat (or its project) is taken first wherever work waits. */
  priority?: "high";
  /** Set when a chat hold covers this chat; it shows under Held. */
  hold?: BoardChatHold;
  /** Butler's newest open (or freshly resolved) recovery case about this chat. */
  butlerCase?: { id: string; status: ButlerCaseStatus; sessionId?: string; updatedAt?: string };
  /** The project Work stage of the Work item assigned to this chat. */
  workStage?: ChatWorkStage;
}

/** Model refusals Core recorded on a goal; ``paused`` while one holds the goal for the operator's review. */
export interface BoardModelRefusal {
  paused: boolean;
  count: number;
  reason?: string;
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
  /** Searched goal chats per Work stage label, in stage order; the stage filter does not narrow these. */
  stages: BoardStageCount[];
  /** Searched goal chats with no Work stage, per section. */
  noStage: ButlerBoardCounts;
  /** Open operator action requests ("Needs you:" Work items), most urgent first; only on a poll. */
  operatorActions: OperatorActionRequest[];
}

export type OperatorActionUrgency = "low" | "normal" | "high" | "blocking";

/** Something only the operator can do (restart a host, plug in a device…), as Butler recorded it. */
export interface OperatorActionRequest {
  /** The Work item that holds the request. */
  id: string;
  project: { id: string; name: string };
  title: string;
  /** False for an older plain "Needs you:" Work item without Butler's typed record. */
  typed: boolean;
  action: string;
  actionLabel: string;
  deviceOrHost: string;
  reason: string;
  urgency: OperatorActionUrgency;
  status: "open" | "closed" | "cancelled";
  goal?: { sessionId: string; title: string; status?: string };
  evidence: string[];
  check?: { kind: string; params: Record<string, unknown> };
  /** Butler's newest evaluation of the auto-check; ``passed`` is null when it could not tell. */
  lastCheck?: { passed: boolean | null; detail: string; checkedAt: string };
  createdAt: string;
}

interface WireOperatorAction {
  id: string;
  project: { id: string; name: string };
  title: string;
  typed: boolean;
  action: string;
  action_label: string;
  device_or_host: string;
  reason: string;
  urgency: string;
  status: string;
  goal?: { session_id: string; title?: string | null; status?: string | null } | null;
  evidence?: string[] | null;
  check?: { kind: string; params?: Record<string, unknown> | null } | null;
  last_check?: { passed: boolean | null; detail?: string | null; checked_at: string } | null;
  created_at: string;
}

/** One Work stage label across projects, with its goal chats per section. */
export interface BoardStageCount {
  label: string;
  counts: ButlerBoardCounts;
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
  goal?: { objective: string; status: string; current_step: number; plan_length: number; revision?: number | null; model_refusal?: { paused: boolean; count: number; reason?: string | null } | null } | null;
  started_by?: string | null;
  proposed_by?: string | null;
  session_revision?: number | null;
  priority?: string | null;
  hold?: { id: string; batch_id?: string | null; reason: string; created_at: string } | null;
  butler_case?: { id: string; status: string; session_id?: string | null; updated_at?: string | null } | null;
  work_stage?: unknown;
}

interface WireBoard {
  counts: ButlerBoardCounts;
  states?: Record<Exclude<ButlerBoardState, "idle">, number> | null;
  projects?: Array<{ id: string; name: string; counts: ButlerBoardCounts }> | null;
  items: WireItem[];
  next_offset?: number | null;
  truncated: boolean;
  stages?: Array<{ label: string; counts: ButlerBoardCounts }> | null;
  no_stage?: ButlerBoardCounts | null;
  operator_actions?: WireOperatorAction[] | null;
}

const URGENCIES: readonly OperatorActionUrgency[] = ["blocking", "high", "normal", "low"];

function mapOperatorAction(wire: WireOperatorAction): OperatorActionRequest {
  const urgency = URGENCIES.find((value) => value === wire.urgency) ?? "normal";
  const status = wire.status === "closed" || wire.status === "cancelled" ? wire.status : "open";
  return {
    id: wire.id,
    project: wire.project,
    title: wire.title,
    typed: wire.typed,
    action: wire.action,
    actionLabel: wire.action_label,
    deviceOrHost: wire.device_or_host,
    reason: wire.reason,
    urgency,
    status,
    goal: wire.goal ? { sessionId: wire.goal.session_id, title: wire.goal.title ?? "", status: wire.goal.status ?? undefined } : undefined,
    evidence: wire.evidence ?? [],
    check: wire.check ? { kind: wire.check.kind, params: wire.check.params ?? {} } : undefined,
    lastCheck: wire.last_check ? { passed: wire.last_check.passed, detail: wire.last_check.detail ?? "", checkedAt: wire.last_check.checked_at } : undefined,
    createdAt: wire.created_at,
  };
}

/** Most urgent first; Core already orders them, this keeps an edited list in order. */
export function sortOperatorActions(requests: readonly OperatorActionRequest[]): OperatorActionRequest[] {
  return [...requests].sort((a, b) => (URGENCIES.indexOf(a.urgency) - URGENCIES.indexOf(b.urgency)) || a.createdAt.localeCompare(b.createdAt));
}

/** What a request's auto-check says now. */
export function operatorActionCheckLabel(request: Pick<OperatorActionRequest, "check" | "lastCheck">): string {
  if (!request.check) return "No auto-check: mark it done when finished";
  const last = request.lastCheck;
  if (!last) return `Auto-check ${request.check.kind}: not run yet`;
  const detail = last.detail ? ` (${last.detail})` : "";
  if (last.passed === true) return `Auto-check ${request.check.kind} passes${detail}; closing`;
  if (last.passed === false) return `Auto-check ${request.check.kind}: not yet${detail}`;
  return `Auto-check ${request.check.kind}: unknown${detail}`;
}

/** Summaries Core reads back: a check-in starting with the cancel text cancels the request. */
export const OPERATOR_ACTION_DONE_SUMMARY = "Operator marked this request done from the Butler board.";
export const OPERATOR_ACTION_CANCEL_SUMMARY = "Operator cancelled this request from the Butler board.";

/**
 * Mark a request done or cancel it: a Work check-in that closes its Work
 * item. Butler's next check records the outcome and resumes a goal that was
 * waiting on a done request.
 */
export async function resolveOperatorAction(api: ApiClient, request: Pick<OperatorActionRequest, "id" | "project">, outcome: "done" | "cancelled"): Promise<void> {
  await api.request(`engagements/${encodeURIComponent(request.project.id)}/work/${encodeURIComponent(request.id)}/updates`, {
    method: "POST",
    body: JSON.stringify({
      summary: outcome === "cancelled" ? OPERATOR_ACTION_CANCEL_SUMMARY : OPERATOR_ACTION_DONE_SUMMARY,
      status: "done",
      request_id: `board-operator-action:${request.id}`,
    }),
  });
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
      modelRefusal: item.goal.model_refusal ? {
        paused: item.goal.model_refusal.paused,
        count: item.goal.model_refusal.count,
        reason: item.goal.model_refusal.reason ?? undefined,
      } : undefined,
    } : undefined,
    startedBy: item.started_by === "butler" ? "butler" : undefined,
    proposedBy: item.proposed_by === "butler" ? "butler" : undefined,
    sessionRevision: item.session_revision ?? undefined,
    priority: item.priority === "high" ? "high" : undefined,
    hold: item.hold ? {
      id: item.hold.id,
      batchId: item.hold.batch_id ?? undefined,
      reason: item.hold.reason,
      createdAt: item.hold.created_at,
    } : undefined,
    butlerCase: mapButlerCase(item.butler_case),
    workStage: parseChatWorkStage(item.work_stage),
  };
}

function mapButlerCase(value: WireItem["butler_case"]): ButlerBoardItem["butlerCase"] {
  const status = parseButlerCaseStatus(value?.status);
  if (!value || !status) return undefined;
  return { id: value.id, status, sessionId: value.session_id ?? undefined, updatedAt: value.updated_at ?? undefined };
}

export interface ButlerBoardQuery {
  q?: string;
  /** One section's page; without it Core answers the poll (Active and Idle). */
  section?: ButlerBoardSection;
  projectId?: string;
  offset?: number;
  limit?: number;
  /** Only goal chats in these Work stages (labels) or, with ``noStage``, without one. */
  stages?: BoardStageFilter;
}

function boardQuery(options: ButlerBoardQuery): string {
  const query = new URLSearchParams();
  if (options.q?.trim()) query.set("q", options.q.trim());
  if (options.section) query.set("section", options.section);
  if (options.projectId) query.set("project_id", options.projectId);
  if (options.offset) query.set("offset", String(options.offset));
  if (options.limit) query.set("limit", String(options.limit));
  for (const label of options.stages?.labels ?? []) query.append("stage", label);
  if (options.stages?.noStage) query.set("no_stage", "true");
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
      stages: answer.value.stages ?? [],
      noStage: answer.value.no_stage ?? emptyCounts(),
      operatorActions: sortOperatorActions((answer.value.operator_actions ?? []).map(mapOperatorAction)),
    },
    etag: answer.etag,
  };
}

/** "project" groups by project (the default); "attention" is the Status grouping: the three sections. */
export type ButlerBoardGrouping = "attention" | "project";

export interface ButlerBoardSectionGroup {
  section: BoardViewSection;
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
  const high = (item: ButlerBoardItem) => (item.priority === "high" ? 0 : 1);
  const build = (key: string, label: string | undefined, listed: readonly ButlerBoardItem[], done: readonly ButlerBoardItem[], counts: ButlerBoardCounts | undefined): ButlerBoardGroup => {
    // Held cards leave Active and Idle for their own section; Core's counts
    // still include them, so those counts drop by the held cards shown.
    const held = listed.filter((item) => item.hold && item.section !== "completed");
    const sections: ButlerBoardGroup["sections"] = BOARD_SECTIONS.map((section) => {
      const sectionItems = section === "completed" ? [...done] : listed.filter((item) => item.section === section && !item.hold);
      sectionItems.sort((a, b) => (high(a) - high(b)) || (section === "active" ? rank(a) - rank(b) : 0));
      const heldHere = held.filter((item) => item.section === section).length;
      const count = coreCounts && counts ? Math.max(0, counts[section] - heldHere) : sectionItems.length;
      return { section, items: sectionItems, count };
    });
    if (held.length) sections.splice(1, 0, { section: "held", items: held.sort((a, b) => high(a) - high(b)), count: held.length });
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
  const waiting = (group: ButlerBoardGroup) => group.sections
    .filter((entry) => entry.section === "active" || entry.section === "idle")
    .reduce((sum, entry) => sum + entry.items.filter((item) => item.state === "waiting").length, 0);
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
  if (item.goal?.modelRefusal?.paused) return "Model refusal";
  return item.goal?.status === "blocked" ? "Blocked" : item.goal?.status === "draft" ? "Proposed" : "Paused";
}

/** Which sections are open, as this device remembers them: ``"<group>:<section>"`` → open. */
export type BoardSectionOpenState = Record<string, boolean>;

/** Completed starts collapsed; Active and Idle start open. */
export function boardSectionOpen(state: BoardSectionOpenState, groupKey: string, section: BoardViewSection): boolean {
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
export function toggleBoardSection(state: BoardSectionOpenState, groupKey: string, section: BoardViewSection, limit = 200): BoardSectionOpenState {
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
export function isResumableIdle(item: Pick<ButlerBoardItem, "section" | "goal" | "attention"> & { hold?: BoardChatHold }): boolean {
  return item.section === "idle" && !item.hold && item.attention?.kind !== "proposal" && (item.goal?.status === "paused" || item.goal?.status === "blocked");
}

/** A chat the operator can set aside with a chat hold: running or idle, not already held, not a proposal. */
export function isHoldable(item: Pick<ButlerBoardItem, "section" | "attention" | "hold">): boolean {
  return !item.hold && (item.section === "active" || item.section === "idle") && item.attention?.kind !== "proposal";
}

/** One project's chat holds placed together: release them together with Undo. */
export interface PlacedHold {
  projectId: string;
  batchId?: string;
  sessionIds: string[];
}

/**
 * Hold chosen chats now, one batch per project. No confirmation: Undo (or
 * Release) resumes exactly what each hold paused.
 */
export async function holdBoardChats(
  api: ApiClient,
  items: ReadonlyArray<Pick<ButlerBoardItem, "sessionId" | "project">>,
): Promise<{ placed: PlacedHold[]; failed: Array<{ projectId: string; error: string }> }> {
  const byProject = new Map<string, string[]>();
  for (const item of items) byProject.set(item.project.id, [...(byProject.get(item.project.id) ?? []), item.sessionId]);
  return placeHolds(api, [...byProject].map(([projectId, sessionIds]) => ({ projectId, sessionIds })));
}

/** Hold every chat with work running now, in each listed project. */
export function holdAllActive(api: ApiClient, projectIds: readonly string[]) {
  return placeHolds(api, projectIds.map((projectId) => ({ projectId, allActive: true })));
}

async function placeHolds(
  api: ApiClient,
  requests: Array<{ projectId: string; sessionIds?: string[]; allActive?: boolean }>,
): Promise<{ placed: PlacedHold[]; failed: Array<{ projectId: string; error: string }> }> {
  const placed: PlacedHold[] = [];
  const failed: Array<{ projectId: string; error: string }> = [];
  await Promise.all(requests.map(async (request) => {
    try {
      const result = await api.projectHolds.holdChats(request.projectId, { sessionIds: request.sessionIds, allActive: request.allActive });
      if (result.sessionIds.length) placed.push({ projectId: request.projectId, ...result });
    } catch (caught) { // diagnostic-expected: the board reports which projects Core refused
      failed.push({ projectId: request.projectId, error: caught instanceof Error && caught.message ? caught.message : "Core did not hold these chats." });
    }
  }));
  return { placed, failed };
}

/** Release whole batches (Undo, Release all) or single chats; Core resumes what each hold paused. */
export async function releaseBoardHolds(
  api: ApiClient,
  releases: ReadonlyArray<{ projectId: string; batchId?: string; sessionIds?: string[] }>,
): Promise<void> {
  await Promise.all(releases.map((release) => api.projectHolds.releaseChats(release.projectId, release)));
}

/**
 * Every resumable idle goal chat Core lists, across projects or in one
 * (``projectId``), matching ``q``: not only the cards the poll returned.
 */
export async function listResumableIdle(api: ApiClient, options: { q?: string; projectId?: string; stages?: BoardStageFilter } = {}, signal?: AbortSignal): Promise<ButlerBoardItem[]> {
  const found: ButlerBoardItem[] = [];
  let offset: number | undefined = 0;
  // Core pages a section by recent activity; the guard stops a misbehaving cursor.
  for (let pages = 0; offset !== undefined && pages < 500; pages += 1) {
    const answer = await getButlerBoardIfChanged(api, { q: options.q, section: "idle", projectId: options.projectId, offset, limit: 200, stages: options.stages }, undefined, signal);
    if (!answer) break;
    // A goal a model refusal paused needs its own review, never a bulk resume.
    found.push(...answer.board.items.filter((item) => isResumableIdle(item) && !item.goal?.modelRefusal?.paused));
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

/**
 * The board's stage filter: goal chats in any of these Work stage labels
 * (across projects, any case) or, with ``noStage``, without a stage. Empty
 * shows every goal chat. This device remembers it, like the grouping.
 */
export interface BoardStageFilter {
  labels: string[];
  noStage: boolean;
}

export const NO_STAGE_FILTER: BoardStageFilter = { labels: [], noStage: false };

export function stageFilterActive(filter: BoardStageFilter): boolean {
  return filter.labels.length > 0 || filter.noStage;
}

/** Parse the remembered filter; anything unreadable shows every stage. */
export function parseStageFilter(stored: string | null): BoardStageFilter {
  if (!stored) return NO_STAGE_FILTER;
  try {
    const value = JSON.parse(stored) as { labels?: unknown; noStage?: unknown };
    const labels = Array.isArray(value?.labels) ? value.labels.filter((label): label is string => typeof label === "string" && label.trim().length > 0).slice(0, 50) : [];
    return { labels: dedupeLabels(labels), noStage: value?.noStage === true };
  } catch {
    return NO_STAGE_FILTER; // diagnostic-expected: a damaged preference falls back to every stage
  }
}

function dedupeLabels(labels: readonly string[]): string[] {
  const seen = new Set<string>();
  return labels.map((label) => label.trim()).filter((label) => !seen.has(label.toLocaleLowerCase()) && seen.add(label.toLocaleLowerCase()));
}

/** Add or remove one stage label (any case); ``null`` toggles No stage. */
export function toggleStageFilter(filter: BoardStageFilter, label: string | null): BoardStageFilter {
  if (label === null) return { ...filter, noStage: !filter.noStage };
  const key = label.toLocaleLowerCase();
  const has = filter.labels.some((entry) => entry.toLocaleLowerCase() === key);
  return { ...filter, labels: has ? filter.labels.filter((entry) => entry.toLocaleLowerCase() !== key) : dedupeLabels([...filter.labels, label]) };
}

/** What the Stage control says: "All", "Recon", "No stage", "Recon + 2". */
export function stageFilterSummary(filter: BoardStageFilter): string {
  const names = [...filter.labels, ...(filter.noStage ? ["No stage"] : [])];
  if (!names.length) return "All";
  return names.length === 1 ? names[0] : `${names[0]} + ${names.length - 1}`;
}

export interface BoardStageOption {
  /** The stage label; ``null`` is No stage. */
  label: string | null;
  name: string;
  /** Searched goal chats in this stage, in every section. */
  count: number;
  selected: boolean;
}

/**
 * The filter's choices: Core's stages in stage order, then No stage. A
 * chosen stage Core no longer reports stays listed with 0, so the operator
 * can see and remove it.
 */
export function stageFilterOptions(board: Pick<ButlerBoard, "stages" | "noStage"> | undefined, filter: BoardStageFilter): BoardStageOption[] {
  const total = (counts: ButlerBoardCounts) => counts.active + counts.idle + counts.completed;
  const chosen = new Set(filter.labels.map((label) => label.toLocaleLowerCase()));
  const listed = (board?.stages ?? []).map((stage) => ({ label: stage.label, name: stage.label, count: total(stage.counts), selected: chosen.has(stage.label.toLocaleLowerCase()) }));
  const known = new Set(listed.map((option) => option.label.toLocaleLowerCase()));
  const missing = filter.labels.filter((label) => !known.has(label.toLocaleLowerCase())).map((label) => ({ label, name: label, count: 0, selected: true }));
  return [...listed, ...missing, { label: null, name: "No stage", count: board ? total(board.noStage) : 0, selected: filter.noStage }];
}
