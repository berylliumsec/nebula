import { describe, expect, it } from "vitest";
import { NO_STAGE_KEY, OTHER_STAGE_KEY, hasStageGap, stageCells, stageColumns, stageKeyFor, type StagedItem } from "./workStages";

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
});
