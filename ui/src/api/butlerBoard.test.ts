import { describe, expect, it, vi } from "vitest";
import { ALL_PROJECTS, boardElapsed, boardSectionOpen, boardStatusLabel, getButlerBoardIfChanged, groupButlerBoard, listButlerThreadsIfChanged, parseBoardSectionState, toggleBoardSection, type ButlerBoardItem } from "./butlerBoard";
import type { ApiClient } from "./client";

function item(overrides: Partial<ButlerBoardItem>): ButlerBoardItem {
  return {
    sessionId: overrides.title ?? "chat",
    title: "chat",
    project: { id: "p1", name: "Research" },
    section: "active",
    state: "working",
    backend: "provider",
    isSubagent: false,
    updatedAt: "2026-11-20T10:00:00Z",
    ...overrides,
  };
}

const bounty = { id: "p2", name: "Bounty" };

describe("Butler board grouping", () => {
  const items = [
    item({ title: "Fuzz", state: "working", project: bounty }),
    item({ title: "Symbols", state: "failed", project: bounty }),
    item({ title: "Install", state: "waiting" }),
    item({ title: "Paused triage", section: "idle", state: "idle", goal: { objective: "x", status: "paused", currentStep: 0, planLength: 1 } }),
    item({ title: "Proposal", section: "idle", state: "waiting", project: bounty, attention: { kind: "proposal", summary: "Butler proposed this goal chat" } }),
  ];
  const projects = [
    { ...bounty, counts: { active: 2, idle: 1, completed: 4 } },
    { id: "p1", name: "Research", counts: { active: 1, idle: 1, completed: 0 } },
  ];

  it("groups by project into Active, Idle and Completed with Core's counts", () => {
    const done = [item({ title: "Old run", section: "completed", state: "idle", project: bounty })];
    const groups = groupButlerBoard(items, "project", { projects, completed: { p2: done } });
    // Both have one chat needing the operator; Bounty has more active, so it leads.
    expect(groups.map((group) => [group.label, group.total])).toEqual([["Bounty", 7], ["Research", 2]]);
    expect(groups[0].sections.map((entry) => [entry.section, entry.count, entry.items.map((card) => card.title)])).toEqual([
      ["active", 2, ["Fuzz", "Symbols"]],
      ["idle", 1, ["Proposal"]],
      ["completed", 4, ["Old run"]],
    ]);
  });

  it("lists chats needing the operator first in Active", () => {
    const [group] = groupButlerBoard(items, "attention", { projects });
    expect(group.key).toBe(ALL_PROJECTS);
    expect(group.label).toBeUndefined();
    expect(group.sections.map((entry) => [entry.section, entry.count])).toEqual([["active", 3], ["idle", 2], ["completed", 4]]);
    expect(group.sections[0].items.map((card) => card.title)).toEqual(["Install", "Fuzz", "Symbols"]);
  });

  it("counts matched cards while the search box is ahead of Core", () => {
    const groups = groupButlerBoard(items.filter((card) => card.project.id === "p2"), "project", { projects: [], coreCounts: false });
    expect(groups.map((group) => [group.label, group.sections.map((entry) => entry.count)])).toEqual([["Bounty", [2, 1, 0]]]);
    expect(groupButlerBoard([], "attention", { projects: [] })).toEqual([]);
  });

  it("names each card's status within its section", () => {
    expect(boardStatusLabel(item({ state: "waiting" }))).toBe("Needs you");
    expect(boardStatusLabel(item({ state: "failed" }))).toBe("Failed");
    expect(boardStatusLabel(item({ state: "idle" }))).toBe("Running");
    expect(boardStatusLabel(items[3])).toBe("Paused");
    expect(boardStatusLabel(items[4])).toBe("Proposed");
    expect(boardStatusLabel(item({ section: "idle", state: "idle", goal: { objective: "x", status: "blocked", currentStep: 0, planLength: 0 } }))).toBe("Blocked");
    expect(boardStatusLabel(item({ section: "completed", state: "idle", goal: { objective: "x", status: "cancelled", currentStep: 0, planLength: 0 } }))).toBe("Cancelled");
    expect(boardStatusLabel(item({ section: "completed", state: "idle", goal: { objective: "x", status: "completed", currentStep: 0, planLength: 0 } }))).toBe("Completed");
  });

  it("starts Completed collapsed and remembers toggled sections", () => {
    expect(boardSectionOpen({}, "p1", "active")).toBe(true);
    expect(boardSectionOpen({}, "p1", "idle")).toBe(true);
    expect(boardSectionOpen({}, "p1", "completed")).toBe(false);
    const opened = toggleBoardSection({}, "p1", "completed");
    expect(boardSectionOpen(opened, "p1", "completed")).toBe(true);
    expect(boardSectionOpen(opened, "p2", "completed")).toBe(false);
    const closed = toggleBoardSection(opened, "p1", "active");
    expect(parseBoardSectionState(JSON.stringify(closed))).toEqual({ "p1:completed": true, "p1:active": false });
    expect(parseBoardSectionState("not json")).toEqual({});
    expect(parseBoardSectionState('{"p1:idle": "yes", "p1:active": false}')).toEqual({ "p1:active": false });
    const many = Array.from({ length: 5 }, (_, index) => index).reduce((state, index) => toggleBoardSection(state, `p${index}`, "idle", 3), {});
    expect(Object.keys(many)).toEqual(["p2:idle", "p3:idle", "p4:idle"]);
  });

  it("formats elapsed time compactly and ignores unknown times", () => {
    const now = Date.parse("2026-11-20T12:00:00Z");
    expect(boardElapsed("2026-11-20T11:59:15Z", now)).toBe("45s");
    expect(boardElapsed("2026-11-20T11:48:00Z", now)).toBe("12m");
    expect(boardElapsed("2026-11-20T09:00:00Z", now)).toBe("3h");
    expect(boardElapsed("2026-11-18T12:00:00Z", now)).toBe("2d");
    expect(boardElapsed(undefined, now)).toBeUndefined();
    expect(boardElapsed("not a date", now)).toBeUndefined();
  });
});

describe("Butler board reads", () => {
  it("maps Core's board and passes the poll's ETag", async () => {
    const requestIfChanged = vi.fn().mockResolvedValue({
      etag: '"b2"',
      value: {
        counts: { active: 1, idle: 2, completed: 9 },
        states: { waiting: 1, working: 0, failed: 0 },
        projects: [{ id: "p1", name: "Research", counts: { active: 1, idle: 2, completed: 9 } }],
        truncated: false,
        next_offset: null,
        items: [{
          session_id: "s1", title: "Kernel fuzz", project: { id: "p1", name: "Research" }, section: "active", state: "waiting",
          backend: "provider", provider_profile_id: "prov", model: "m", is_subagent: false, turn_id: "t1",
          turn_status: "waiting_approval", turn_started_at: "2026-11-20T10:00:00Z", updated_at: "2026-11-20T10:01:00Z",
          now: "Approve run_command: make install",
          attention: { kind: "approval", action_id: "a1", summary: "Approve run_command: make install", requested_at: "2026-11-20T10:01:00Z" },
          goal: { objective: "Find a crash", status: "running", current_step: 2, plan_length: 6 },
        }],
      },
    });
    const answer = await getButlerBoardIfChanged({ requestIfChanged } as unknown as ApiClient, { q: " fuzz " }, '"b1"');
    expect(requestIfChanged).toHaveBeenCalledWith("butler/board?q=fuzz", '"b1"', { signal: undefined });
    expect(answer?.etag).toBe('"b2"');
    expect(answer?.board.counts.completed).toBe(9);
    expect(answer?.board.states?.waiting).toBe(1);
    expect(answer?.board.projects[0].counts.idle).toBe(2);
    expect(answer?.board.items[0]).toMatchObject({
      sessionId: "s1", section: "active", state: "waiting", turnId: "t1",
      attention: { kind: "approval", actionId: "a1" },
      goal: { currentStep: 2, planLength: 6 },
    });
  });

  it("pages a section in one project and treats 304 as unchanged", async () => {
    const requestIfChanged = vi.fn().mockResolvedValue(undefined);
    const api = { requestIfChanged } as unknown as ApiClient;
    expect(await getButlerBoardIfChanged(api, { section: "completed", projectId: "p 1", offset: 50, limit: 50 }, '"b"')).toBeUndefined();
    expect(requestIfChanged).toHaveBeenCalledWith("butler/board?section=completed&project_id=p+1&offset=50&limit=50", '"b"', { signal: undefined });
    expect(await listButlerThreadsIfChanged(api, '"t"')).toBeUndefined();
    expect(requestIfChanged).toHaveBeenLastCalledWith("butler/threads", '"t"', { signal: undefined });
  });
});
