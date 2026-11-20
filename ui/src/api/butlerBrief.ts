import type { ApiClient } from "./client";
import type { ButlerBoardState } from "./butlerBoard";

export interface ButlerBriefTurn {
  turnId: string;
  index: number;
  status: string;
  startedAt?: string;
  finishedAt?: string;
  /** One line Core derived from the turn: its first answer sentence, its tool calls, or its failure. */
  line: string;
  planIndex?: number;
  counts: { commands: number; edits: number; approvals: number; tools: number };
  files: string[];
}

export interface ButlerBrief {
  sessionId: string;
  title: string;
  project: { id: string; name: string };
  state: ButlerBoardState;
  backend: "provider" | "harness";
  model?: string;
  startedAt?: string;
  updatedAt: string;
  turnCount: number;
  costUsd?: number;
  subagentCount: number;
  parentSessionId?: string;
  goal?: {
    objective: string;
    completionCriteria?: string;
    status: string;
    plan: string[];
    activePlanIndex?: number;
    turnsUsed: number;
    blockedReason?: string;
  };
  attention?: { kind: "approval" | "question" | "recovery" | "proposal"; actionId?: string; summary: string; command?: string };
  /** Oldest first. */
  turns: ButlerBriefTurn[];
  /** Pass as ``before`` to read older turns; absent when there are none. */
  olderBefore?: number;
  files: Array<{ path: string; lastTurnId?: string; lastChangedAt?: string }>;
}

type Wire = Record<string, any>;

const pick = (value: Wire, snake: string, camel: string): any => value[snake] ?? value[camel];
const optional = <T,>(value: T | null | undefined): T | undefined => value ?? undefined;

function mapTurn(turn: Wire): ButlerBriefTurn {
  const counts = turn.counts ?? {};
  return {
    turnId: pick(turn, "turn_id", "turnId"),
    index: turn.index,
    status: turn.status,
    startedAt: optional(pick(turn, "started_at", "startedAt")),
    finishedAt: optional(pick(turn, "finished_at", "finishedAt")),
    line: turn.line ?? "",
    planIndex: optional(pick(turn, "plan_index", "planIndex")),
    counts: { commands: counts.commands ?? 0, edits: counts.edits ?? 0, approvals: counts.approvals ?? 0, tools: counts.tools ?? 0 },
    files: turn.files ?? [],
  };
}

/** Core's wire keys are mapped once here, whichever case the route uses. */
export function mapButlerBrief(value: Wire): ButlerBrief {
  const goal = value.goal as Wire | null | undefined;
  const attention = value.attention as Wire | null | undefined;
  return {
    sessionId: pick(value, "session_id", "sessionId"),
    title: value.title,
    project: value.project,
    state: value.state,
    backend: value.backend,
    model: optional(value.model),
    startedAt: optional(pick(value, "started_at", "startedAt")),
    updatedAt: pick(value, "updated_at", "updatedAt"),
    turnCount: pick(value, "turn_count", "turnCount") ?? 0,
    costUsd: optional(pick(value, "cost_usd", "costUsd")),
    subagentCount: pick(value, "subagent_count", "subagentCount") ?? 0,
    parentSessionId: optional(pick(value, "parent_session_id", "parentSessionId")),
    goal: goal ? {
      objective: goal.objective,
      completionCriteria: optional(pick(goal, "completion_criteria", "completionCriteria")),
      status: goal.status,
      plan: goal.plan ?? [],
      activePlanIndex: optional(pick(goal, "active_plan_index", "activePlanIndex")),
      turnsUsed: pick(goal, "turns_used", "turnsUsed") ?? 0,
      blockedReason: optional(pick(goal, "blocked_reason", "blockedReason")),
    } : undefined,
    attention: attention ? {
      kind: attention.kind,
      actionId: optional(pick(attention, "action_id", "actionId")),
      summary: attention.summary,
      command: optional(attention.command),
    } : undefined,
    turns: ((value.turns ?? []) as Wire[]).map(mapTurn),
    olderBefore: optional(pick(value, "older_before", "olderBefore")),
    files: ((value.files ?? []) as Wire[]).map((file) => ({ path: file.path, lastTurnId: optional(pick(file, "last_turn_id", "lastTurnId")), lastChangedAt: optional(pick(file, "last_changed_at", "lastChangedAt")) })),
  };
}

/** A chat's brief for a poll: undefined when Core answered 304 for ``etag``. */
export async function getButlerBriefIfChanged(api: ApiClient, sessionId: string, etag: string | undefined, signal?: AbortSignal): Promise<{ brief: ButlerBrief; etag?: string } | undefined> {
  const answer = await api.requestIfChanged<Wire>(`butler/sessions/${encodeURIComponent(sessionId)}/brief`, etag, { signal });
  if (!answer) return undefined;
  return { brief: mapButlerBrief(answer.value), etag: answer.etag };
}

/** Older turns, oldest first. */
export async function getButlerBriefTurnsBefore(api: ApiClient, sessionId: string, before: number, signal?: AbortSignal): Promise<{ turns: ButlerBriefTurn[]; olderBefore?: number }> {
  const value = mapButlerBrief(await api.request<Wire>(`butler/sessions/${encodeURIComponent(sessionId)}/brief?before=${before}`, { signal }));
  return { turns: value.turns, olderBefore: value.olderBefore };
}

/** Turns grouped under the plan step they ran in; turns with no step go under ``undefined``. */
export function groupTurnsByStep(turns: ButlerBriefTurn[]): Map<number | undefined, ButlerBriefTurn[]> {
  const groups = new Map<number | undefined, ButlerBriefTurn[]>();
  for (const turn of turns) {
    const list = groups.get(turn.planIndex) ?? [];
    list.push(turn);
    groups.set(turn.planIndex, list);
  }
  return groups;
}

/** "Ran 3 commands · edited 2 files · 1 approval" for a turn's chips. */
export function turnChips(turn: ButlerBriefTurn): string[] {
  const chips: string[] = [];
  const plural = (count: number, one: string, many: string) => `${count} ${count === 1 ? one : many}`;
  if (turn.counts.commands) chips.push(`Ran ${plural(turn.counts.commands, "command", "commands")}`);
  if (turn.counts.edits) chips.push(`Edited ${plural(turn.counts.edits, "file", "files")}`);
  if (turn.counts.approvals) chips.push(plural(turn.counts.approvals, "approval", "approvals"));
  const other = turn.counts.tools - turn.counts.commands - turn.counts.edits;
  if (other > 0) chips.push(plural(other, "other tool", "other tools"));
  return chips;
}
