import type { ChatWorkStage, WorkStage } from "../api/types";

/** The Work item fields stage views read. */
export interface StagedItem {
  id: string;
  status: "backlog" | "ready" | "in_progress" | "blocked" | "review" | "done";
  stage?: string | null;
}

export type StageCellState = "active" | "blocked" | "gap" | "empty" | "normal";

/** One stage of one project on All projects: open count, a short detail, its state. */
export interface StageCell {
  key: string;
  label: string;
  open: number;
  done: number;
  state: StageCellState;
  detail: string;
}

/** Items whose stage key the project no longer lists. */
export const OTHER_STAGE_KEY = "__other";
/** Items with no stage. */
export const NO_STAGE_KEY = "__none";

const plural = (count: number, one: string, many = `${one}s`) => `${count.toLocaleString()} ${count === 1 ? one : many}`;

function bucketOf(item: StagedItem, keys: Set<string>): string {
  if (!item.stage) return NO_STAGE_KEY;
  return keys.has(item.stage) ? item.stage : OTHER_STAGE_KEY;
}

function cell(key: string, label: string, stageItems: StagedItem[], flagWhenEmpty: boolean): StageCell {
  const open = stageItems.filter((item) => item.status !== "done");
  const done = stageItems.length - open.length;
  const blocked = open.filter((item) => item.status === "blocked").length;
  const review = open.filter((item) => item.status === "review").length;
  const inProgress = open.filter((item) => item.status === "in_progress").length;
  if (!open.length) {
    // A flagged stage is a gap only when nothing has ever reached it.
    if (!done) return { key, label, open: 0, done: 0, state: flagWhenEmpty ? "gap" : "empty", detail: flagWhenEmpty ? "None yet" : "" };
    return { key, label, open: 0, done, state: "normal", detail: `${done.toLocaleString()} done` };
  }
  const detail = blocked ? `${blocked.toLocaleString()} blocked`
    : review ? `${review.toLocaleString()} in review`
      : inProgress ? `${inProgress.toLocaleString()} in progress`
        : `${plural(open.length, "item")} not started`;
  return { key, label, open: open.length, done, state: blocked ? "blocked" : inProgress || review ? "active" : "normal", detail };
}

/** One cell per defined stage, plus Other stage when items use an unlisted key. */
export function stageCells(stages: WorkStage[], items: StagedItem[]): StageCell[] {
  const keys = new Set(stages.map((stage) => stage.key));
  const byKey = new Map<string, StagedItem[]>();
  for (const item of items) {
    const bucket = bucketOf(item, keys);
    byKey.set(bucket, [...(byKey.get(bucket) ?? []), item]);
  }
  const cells = stages.map((stage) => cell(stage.key, stage.label, byKey.get(stage.key) ?? [], stage.flagWhenEmpty));
  const other = byKey.get(OTHER_STAGE_KEY) ?? [];
  if (other.some((item) => item.status !== "done")) cells.push(cell(OTHER_STAGE_KEY, "Other stage", other, false));
  return cells;
}

export interface StageColumn<T extends StagedItem> {
  key: string;
  label: string;
  open: T[];
  done: number;
}

/** Board columns when grouped by stage: defined stages, then No stage and Other stage when used. */
export function stageColumns<T extends StagedItem>(stages: WorkStage[], items: T[]): StageColumn<T>[] {
  const keys = new Set(stages.map((stage) => stage.key));
  const byKey = new Map<string, T[]>();
  for (const item of items) {
    const bucket = bucketOf(item, keys);
    byKey.set(bucket, [...(byKey.get(bucket) ?? []), item]);
  }
  const column = (key: string, label: string): StageColumn<T> => {
    const all = byKey.get(key) ?? [];
    const open = all.filter((item) => item.status !== "done");
    return { key, label, open, done: all.length - open.length };
  };
  const columns = stages.map((stage) => column(stage.key, stage.label));
  for (const [key, label] of [[NO_STAGE_KEY, "No stage"], [OTHER_STAGE_KEY, "Other stage"]] as const) {
    if (byKey.get(key)?.length) columns.push(column(key, label));
  }
  return columns;
}

/** Projects with a flagged stage that nothing has reached. */
export function hasStageGap(cells: StageCell[]): boolean {
  return cells.some((entry) => entry.state === "gap");
}

/** A stable key for a new stage, derived from its label and unique in the project. */
export function stageKeyFor(label: string, taken: Iterable<string>): string {
  const used = new Set(taken);
  const base = label.normalize("NFKD").replace(/[\u0300-\u036f]/g, "").toLocaleLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 56) || "stage";
  let key = base;
  for (let suffix = 2; used.has(key); suffix += 1) key = `${base}-${suffix}`;
  return key;
}

/** The fields stage inheritance reads from a project. */
export interface StageOwner {
  id: string;
  name: string;
  parentEngagementId?: string | null;
  workStages?: WorkStage[];
}

/** A project's own stages, or else its nearest ancestor's; `from` names that ancestor. */
export function effectiveStages<T extends StageOwner>(project: T | undefined, byId: ReadonlyMap<string, T>): { stages: WorkStage[]; from?: T } {
  const seen = new Set<string>();
  for (let current = project; current && !seen.has(current.id); current = current.parentEngagementId ? byId.get(current.parentEngagementId) : undefined) {
    seen.add(current.id);
    if (current.workStages?.length) return { stages: current.workStages, from: current === project ? undefined : current };
  }
  return { stages: [] };
}

/** One stage across a parent's subprojects: which reached it and how their work there stands. */
export interface StageRollupCell {
  key: string;
  label: string;
  total: number;
  /** Subprojects with any item in this stage. */
  reached: Set<string>;
  open: number;
  active: number;
  blocked: number;
  state: StageCellState;
  detail: string;
}

/** Per stage, how many of the given subprojects reached it; counts are subprojects, not items. */
export function stageRollup<T extends StagedItem & { engagement_id: string }>(stages: WorkStage[], projectIds: readonly string[], items: readonly T[]): StageRollupCell[] {
  const members = new Set(projectIds);
  const byStage = new Map<string, Map<string, T[]>>();
  for (const item of items) {
    if (!item.stage || !members.has(item.engagement_id)) continue;
    const projects = byStage.get(item.stage) ?? new Map<string, T[]>();
    projects.set(item.engagement_id, [...(projects.get(item.engagement_id) ?? []), item]);
    byStage.set(item.stage, projects);
  }
  return stages.map((stage) => {
    const projects = byStage.get(stage.key) ?? new Map<string, T[]>();
    let open = 0, active = 0, blocked = 0;
    for (const stageItems of projects.values()) {
      const pending = stageItems.filter((item) => item.status !== "done");
      if (pending.length) open += 1;
      if (pending.some((item) => item.status === "blocked")) blocked += 1;
      else if (pending.some((item) => item.status === "in_progress" || item.status === "review")) active += 1;
    }
    const reached = new Set(projects.keys());
    const state: StageCellState = !reached.size ? (stage.flagWhenEmpty ? "gap" : "empty") : blocked ? "blocked" : active ? "active" : "normal";
    const detail = !reached.size ? "" : blocked ? `${blocked.toLocaleString()} blocked`
      : active ? `${active.toLocaleString()} active`
        : open ? `${open.toLocaleString()} not started` : "All done";
    return { key: stage.key, label: stage.label, total: members.size, reached, open, active, blocked, state, detail };
  });
}

const ITEM_STATUSES: readonly ChatWorkStage["itemStatus"][] = ["backlog", "ready", "in_progress", "blocked", "review", "done"];

/** Core's ``work_stage`` on a chat or board item, or undefined when absent or malformed. */
export function parseChatWorkStage(value: unknown): ChatWorkStage | undefined {
  if (!value || typeof value !== "object") return undefined;
  const wire = value as Record<string, unknown>;
  const status = wire.item_status as ChatWorkStage["itemStatus"];
  if (typeof wire.key !== "string" || typeof wire.label !== "string" || !wire.label.trim()
    || typeof wire.item_id !== "string" || !ITEM_STATUSES.includes(status)) return undefined;
  return { key: wire.key, label: wire.label, itemId: wire.item_id, itemTitle: typeof wire.item_title === "string" ? wire.item_title : "", itemStatus: status };
}

/** The Projects page's stage-tile state for one item, so its dot colour reads the same here. */
export function chatStageState(status: ChatWorkStage["itemStatus"]): Extract<StageCellState, "active" | "blocked" | "normal"> {
  if (status === "blocked") return "blocked";
  return status === "in_progress" || status === "review" ? "active" : "normal";
}

const ITEM_STATUS_TEXT: Record<ChatWorkStage["itemStatus"], string> = {
  backlog: "not started", ready: "not started", in_progress: "in progress", blocked: "blocked", review: "in review", done: "done",
};

/** "Work stage Validation, blocked": what a screen reader hears beside the chat's own name. */
export function chatStageName(stage: ChatWorkStage): string {
  return `Work stage ${stage.label}, ${ITEM_STATUS_TEXT[stage.itemStatus]}`;
}

/** The tooltip also names the Work item: "Work stage Validation: Validate candidate crash, blocked". */
export function chatStageDescription(stage: ChatWorkStage): string {
  return `Work stage ${stage.label}: ${stage.itemTitle ? `${stage.itemTitle}, ` : ""}${ITEM_STATUS_TEXT[stage.itemStatus]}`;
}
