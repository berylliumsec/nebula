import type { WorkStage } from "../api/types";

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
