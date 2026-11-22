import { describe, expect, it } from "vitest";
import { NO_STAGE_KEY, OTHER_STAGE_KEY, effectiveStages, hasStageGap, stageCells, stageColumns, stageKeyFor, stageRollup, type StagedItem } from "./workStages";

const stages = [
  { key: "draft", label: "Draft", flagWhenEmpty: true },
  { key: "review", label: "Review", flagWhenEmpty: true },
  { key: "ship", label: "Ship", flagWhenEmpty: false },
];
const item = (id: string, status: StagedItem["status"], stage?: string | null): StagedItem => ({ id, status, stage });

describe("work stages", () => {
  it("counts open items per stage and flags only untouched flagged stages", () => {
    const cells = stageCells(stages, [
      item("1", "in_progress", "draft"),
      item("2", "blocked", "draft"),
      item("3", "done", "review"),
      item("4", "ready", null),
    ]);
    expect(cells.map((cell) => [cell.key, cell.open, cell.state, cell.detail])).toEqual([
      ["draft", 2, "blocked", "1 blocked"],
      ["review", 0, "normal", "1 done"],
      ["ship", 0, "empty", ""],
    ]);
    expect(hasStageGap(cells)).toBe(false);
    const empty = stageCells(stages, []);
    expect(empty.map((cell) => cell.state)).toEqual(["gap", "gap", "empty"]);
    expect(empty[0].detail).toBe("None yet");
    expect(hasStageGap(empty)).toBe(true);
  });

  it("shows an Other stage cell for unlisted keys with open items", () => {
    const cells = stageCells(stages, [item("1", "review", "retired"), item("2", "ready", "draft")]);
    expect(cells.at(-1)).toMatchObject({ key: OTHER_STAGE_KEY, label: "Other stage", open: 1, state: "active", detail: "1 in review" });
    expect(cells[0].detail).toBe("1 item not started");
    expect(stageCells(stages, [item("1", "done", "retired")]).some((cell) => cell.key === OTHER_STAGE_KEY)).toBe(false);
  });

  it("builds board columns with No stage and Other stage only when used", () => {
    const columns = stageColumns(stages, [item("1", "ready", "draft"), item("2", "done", "draft"), item("3", "ready")]);
    expect(columns.map((column) => [column.key, column.open.map((entry) => entry.id), column.done])).toEqual([
      ["draft", ["1"], 1], ["review", [], 0], ["ship", [], 0], [NO_STAGE_KEY, ["3"], 0],
    ]);
  });

  it("derives unique stage keys from labels", () => {
    expect(stageKeyFor("Code Review", [])).toBe("code-review");
    expect(stageKeyFor("Code review", ["code-review"])).toBe("code-review-2");
    expect(stageKeyFor("   ", [])).toBe("stage");
    expect(stageKeyFor("Écrire", [])).toBe("ecrire");
  });

  it("uses a project's own stages, else its nearest ancestor's, and survives a parent cycle", () => {
    const own = [{ key: "build", label: "Build", flagWhenEmpty: false }];
    const projects = [
      { id: "root", name: "Root", workStages: stages },
      { id: "team", name: "Team", parentEngagementId: "root" },
      { id: "task", name: "Task", parentEngagementId: "team" },
      { id: "own", name: "Own", parentEngagementId: "root", workStages: own },
      { id: "a", name: "A", parentEngagementId: "b" },
      { id: "b", name: "B", parentEngagementId: "a" },
    ];
    const byId = new Map(projects.map((project) => [project.id, project]));
    expect(effectiveStages(byId.get("task"), byId)).toEqual({ stages, from: byId.get("root") });
    expect(effectiveStages(byId.get("root"), byId)).toEqual({ stages, from: undefined });
    expect(effectiveStages(byId.get("own"), byId)).toEqual({ stages: own, from: undefined });
    expect(effectiveStages(byId.get("a"), byId)).toEqual({ stages: [] });
    expect(effectiveStages(undefined, byId)).toEqual({ stages: [] });
  });

  it("counts subprojects, not items, per stage across a parent's subprojects", () => {
    const at = (id: string, project: string, status: StagedItem["status"], stage: string | null) => ({ ...item(id, status, stage), engagement_id: project });
    const cells = stageRollup(stages, ["a", "b", "c"], [
      at("1", "a", "in_progress", "draft"), at("2", "a", "blocked", "draft"), at("3", "b", "done", "draft"),
      at("4", "b", "ready", "ship"), at("5", "outside", "in_progress", "review"), at("6", "c", "in_progress", null),
    ]);
    expect(cells.map((cell) => [cell.key, [...cell.reached].sort(), cell.state, cell.detail])).toEqual([
      ["draft", ["a", "b"], "blocked", "1 blocked"],
      ["review", [], "gap", ""],
      ["ship", ["b"], "normal", "1 not started"],
    ]);
    expect(cells[0].total).toBe(3);
    expect(stageRollup(stages, ["b"], [at("3", "b", "done", "draft")])[0]).toMatchObject({ state: "normal", detail: "All done" });
  });
});
