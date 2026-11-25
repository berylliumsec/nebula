import { describe, expect, it, vi } from "vitest";
import { ALL_PROJECTS, NO_STAGE_FILTER, parseStageFilter, stageFilterActive, stageFilterOptions, stageFilterSummary, toggleStageFilter, boardElapsed, holdBoardChats, isHoldable, isResumableIdle, listResumableIdle, resumeIdleGoals, boardSectionOpen, boardStatusLabel, getButlerBoardIfChanged, groupButlerBoard, listButlerThreadsIfChanged, operatorActionCheckLabel, resolveOperatorAction, OPERATOR_ACTION_CANCEL_SUMMARY, OPERATOR_ACTION_DONE_SUMMARY, parseBoardSectionState, toggleBoardSection, type ButlerBoardItem } from "./butlerBoard";
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
          butler_case: { id: "case-1", status: "needs_attention", session_id: "case-chat", updated_at: "2026-11-20T10:02:00Z" },
          work_stage: { key: "validation", label: "Validation", item_id: "item-1", item_title: "Validate crash", item_status: "blocked" },
        }, {
          session_id: "s2", title: "Other", project: { id: "p1", name: "Research" }, section: "active", state: "working",
          backend: "provider", is_subagent: false, updated_at: "2026-11-20T10:01:00Z",
          butler_case: { id: "case-2", status: "unknown-status" },
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
      butlerCase: { id: "case-1", status: "needs_attention", sessionId: "case-chat" },
      workStage: { key: "validation", label: "Validation", itemId: "item-1", itemStatus: "blocked" },
    });
    expect(answer?.board.items[1].workStage).toBeUndefined();
    // A status this UI does not know shows no stage rather than a wrong one.
    expect(answer?.board.items[1].butlerCase).toBeUndefined();
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

describe("resuming idle goals", () => {
  it("counts only paused or blocked idle goals as resumable", () => {
    expect(isResumableIdle(item({ section: "idle", goal: { objective: "x", status: "paused", currentStep: 0, planLength: 1 } }))).toBe(true);
    expect(isResumableIdle(item({ section: "idle", goal: { objective: "x", status: "blocked", currentStep: 0, planLength: 1 } }))).toBe(true);
    expect(isResumableIdle(item({ section: "idle", goal: { objective: "x", status: "draft", currentStep: 0, planLength: 1 } }))).toBe(false);
    expect(isResumableIdle(item({ section: "idle", attention: { kind: "proposal", summary: "" }, goal: { objective: "x", status: "paused", currentStep: 0, planLength: 1 } }))).toBe(false);
    expect(isResumableIdle(item({ section: "active", goal: { objective: "x", status: "running", currentStep: 0, planLength: 1 } }))).toBe(false);
  });

  it("pages through Core's whole idle section", async () => {
    const wire = (id: string, status: string) => ({ session_id: id, title: id, project: { id: "p1", name: "R" }, section: "idle", state: "idle", backend: "provider", is_subagent: false, updated_at: "2026-11-20T10:00:00Z", goal: { objective: "x", status, current_step: 0, plan_length: 1, revision: 2 } });
    const requestIfChanged = vi.fn()
      .mockResolvedValueOnce({ value: { counts: { active: 0, idle: 3, completed: 0 }, projects: [], items: [wire("a", "paused"), wire("b", "draft")], next_offset: 200, truncated: false } })
      .mockResolvedValueOnce({ value: { counts: { active: 0, idle: 3, completed: 0 }, projects: [], items: [wire("c", "blocked"), wire("a", "paused")], next_offset: null, truncated: false } });
    const found = await listResumableIdle({ requestIfChanged } as unknown as ApiClient, { q: "fuzz", projectId: "p1" });
    expect(found.map((entry) => entry.sessionId)).toEqual(["a", "c"]);
    expect(requestIfChanged.mock.calls.map(([path]) => path)).toEqual([
      "butler/board?q=fuzz&section=idle&project_id=p1&limit=200",
      "butler/board?q=fuzz&section=idle&project_id=p1&offset=200&limit=200",
    ]);
  });

  it("re-reads a goal whose revision moved and reports Core's refusals", async () => {
    const writeChatGoal = vi.fn(async (sessionId: string, body: { expectedRevision: number }) => {
      if (sessionId === "moved" && body.expectedRevision === 1) throw Object.assign(new Error("goal changed on another device; reload before retrying"), { status: 409 });
      if (sessionId === "held") throw Object.assign(new Error("A project hold paused this goal"), { status: 409 });
    });
    const getChatGoal = vi.fn(async () => ({ revision: 5 }));
    const api = { writeChatGoal, getChatGoal } as unknown as ApiClient;
    const goal = (revision?: number) => ({ objective: "x", status: "paused", currentStep: 0, planLength: 1, revision });
    const progress: number[] = [];
    const outcomes = await resumeIdleGoals(api, [{ sessionId: "moved", goal: goal(1) }, { sessionId: "held", goal: goal(3) }, { sessionId: "fresh", goal: goal(undefined) }], (done) => progress.push(done), 1);
    expect(outcomes).toEqual([{ sessionId: "moved" }, { sessionId: "held", error: "A project hold paused this goal" }, { sessionId: "fresh" }]);
    expect(writeChatGoal).toHaveBeenCalledWith("moved", { expectedRevision: 5, action: "resume" });
    expect(writeChatGoal).toHaveBeenCalledWith("fresh", { expectedRevision: 5, action: "resume" });
    expect(writeChatGoal.mock.calls.filter(([id]) => id === "held")).toHaveLength(1);
    expect(progress).toEqual([1, 2, 3]);
  });
});


describe("Butler board holds and priority", () => {
  const hold = { id: "h1", batchId: "b1", reason: "Priorities changed", createdAt: "2026-11-20T10:00:00Z" };

  it("moves held chats to a Held section and leads each section with high priority", () => {
    const [group] = groupButlerBoard([
      item({ title: "Routine", state: "waiting" }),
      item({ title: "Urgent", state: "working", priority: "high" }),
      item({ title: "Set aside", section: "idle", state: "idle", goal: { objective: "x", status: "paused", currentStep: 0, planLength: 1 }, hold }),
      item({ title: "Paused", section: "idle", state: "idle", goal: { objective: "x", status: "paused", currentStep: 0, planLength: 1 } }),
    ], "project", { projects: [{ id: "p1", name: "Research", counts: { active: 2, idle: 2, completed: 0 } }] });
    expect(group.sections.map((entry) => [entry.section, entry.items.map((value) => value.title), entry.count])).toEqual([
      ["active", ["Urgent", "Routine"], 2],
      ["held", ["Set aside"], 1],
      ["idle", ["Paused"], 1],
      ["completed", [], 0],
    ]);
  });

  it("holds and resumes only what fits: no proposals, nothing already held", () => {
    expect(isHoldable(item({}))).toBe(true);
    expect(isHoldable(item({ hold }))).toBe(false);
    expect(isHoldable(item({ section: "idle", attention: { kind: "proposal", summary: "x" } }))).toBe(false);
    expect(isHoldable(item({ section: "completed" }))).toBe(false);
    expect(isResumableIdle(item({ section: "idle", goal: { objective: "x", status: "paused", currentStep: 0, planLength: 1 }, hold }))).toBe(false);
  });

  it("places one hold batch per project", async () => {
    const holdChats = vi.fn(async (projectId: string, choice: { sessionIds?: string[] }) => ({ batchId: `b-${projectId}`, sessionIds: choice.sessionIds ?? [] }));
    const api = { projectHolds: { holdChats } } as unknown as ApiClient;
    const result = await holdBoardChats(api, [item({ title: "a" }), item({ title: "b", project: bounty }), item({ title: "c" })]);
    expect(holdChats.mock.calls.map(([projectId, choice]) => [projectId, choice.sessionIds])).toEqual([["p1", ["a", "c"]], ["p2", ["b"]]]);
    expect(result.placed.map((entry) => entry.batchId).sort()).toEqual(["b-p1", "b-p2"]);
    expect(result.failed).toEqual([]);
  });
});

describe("Butler board stage filter", () => {
  it("sends chosen stages and No stage to Core and maps Core's stage counts", async () => {
    const requestIfChanged = vi.fn().mockResolvedValue({
      etag: '"s"',
      value: {
        counts: { active: 1, idle: 0, completed: 0 }, projects: [], items: [], truncated: false,
        stages: [{ label: "Recon", counts: { active: 3, idle: 1, completed: 0 } }],
        no_stage: { active: 0, idle: 2, completed: 5 },
      },
    });
    const answer = await getButlerBoardIfChanged({ requestIfChanged } as unknown as ApiClient, { q: "fuzz", stages: { labels: ["Recon", "Lanes"], noStage: true } }, undefined);
    expect(requestIfChanged).toHaveBeenCalledWith("butler/board?q=fuzz&stage=Recon&stage=Lanes&no_stage=true", undefined, { signal: undefined });
    expect(answer?.board.stages).toEqual([{ label: "Recon", counts: { active: 3, idle: 1, completed: 0 } }]);
    expect(answer?.board.noStage).toEqual({ active: 0, idle: 2, completed: 5 });
  });

  it("defaults stage counts for an older Core", async () => {
    const requestIfChanged = vi.fn().mockResolvedValue({ value: { counts: { active: 0, idle: 0, completed: 0 }, items: [], truncated: false } });
    const answer = await getButlerBoardIfChanged({ requestIfChanged } as unknown as ApiClient, {}, undefined);
    expect(answer?.board.stages).toEqual([]);
    expect(answer?.board.noStage).toEqual({ active: 0, idle: 0, completed: 0 });
  });

  it("toggles labels case-insensitively, summarizes, and remembers only valid choices", () => {
    let filter = toggleStageFilter(NO_STAGE_FILTER, "Recon");
    filter = toggleStageFilter(filter, "Lanes");
    expect(stageFilterSummary(filter)).toBe("Recon + 1");
    filter = toggleStageFilter(filter, "recon");
    expect(filter.labels).toEqual(["Lanes"]);
    filter = toggleStageFilter(filter, null);
    expect(stageFilterActive(filter)).toBe(true);
    expect(stageFilterSummary(filter)).toBe("Lanes + 1");
    expect(stageFilterSummary(NO_STAGE_FILTER)).toBe("All");
    expect(stageFilterSummary({ labels: [], noStage: true })).toBe("No stage");
    expect(parseStageFilter(JSON.stringify(filter))).toEqual(filter);
    expect(parseStageFilter('{"labels":["Recon"," recon ",7,""],"noStage":"yes"}')).toEqual({ labels: ["Recon"], noStage: false });
    expect(parseStageFilter("not json")).toEqual(NO_STAGE_FILTER);
  });

  it("lists Core's stages in order, keeps a chosen stage Core no longer reports, then No stage", () => {
    const options = stageFilterOptions({
      stages: [{ label: "Recon", counts: { active: 2, idle: 1, completed: 0 } }, { label: "Lanes", counts: { active: 0, idle: 1, completed: 1 } }],
      noStage: { active: 1, idle: 0, completed: 4 },
    }, { labels: ["lanes", "Shipping"], noStage: false });
    expect(options).toEqual([
      { label: "Recon", name: "Recon", count: 3, selected: false },
      { label: "Lanes", name: "Lanes", count: 2, selected: true },
      { label: "Shipping", name: "Shipping", count: 0, selected: true },
      { label: null, name: "No stage", count: 5, selected: false },
    ]);
  });
});

describe("operator action requests", () => {
  const wire = (overrides: Record<string, unknown>) => ({
    id: "w1", project: { id: "p1", name: "Research" }, title: "Needs you: Restart host on lab-host-1", typed: true,
    action: "restart_host", action_label: "Restart host", device_or_host: "lab-host-1", reason: "Hung after an update",
    urgency: "normal", status: "open", goal: null, evidence: ["ssh timed out"], check: { kind: "tcp_reachable", params: { port: 22 } },
    last_check: null, created_at: "2026-11-20T10:00:00Z", ...overrides,
  });

  it("maps the poll's requests most urgent first and tolerates an older Core", async () => {
    const requestIfChanged = vi.fn().mockResolvedValue({
      etag: '"o"',
      value: {
        counts: { active: 0, idle: 0, completed: 0 }, items: [], truncated: false,
        operator_actions: [
          wire({ id: "normal" }),
          wire({ id: "blocking", urgency: "blocking", goal: { session_id: "s1", title: "Lab run", status: "blocked" }, last_check: { passed: false, detail: "refused", checked_at: "2026-11-20T10:05:00Z" } }),
          wire({ id: "odd", urgency: "someday", status: "weird", check: null, typed: false }),
        ],
      },
    });
    const answer = await getButlerBoardIfChanged({ requestIfChanged } as unknown as ApiClient, {}, undefined);
    const requests = answer!.board.operatorActions;
    expect(requests.map((request) => request.id)).toEqual(["blocking", "normal", "odd"]);
    expect(requests[0]).toMatchObject({ deviceOrHost: "lab-host-1", actionLabel: "Restart host", goal: { sessionId: "s1", title: "Lab run", status: "blocked" }, lastCheck: { passed: false, detail: "refused" } });
    expect(requests[2]).toMatchObject({ urgency: "normal", status: "open", check: undefined, typed: false });

    const older = vi.fn().mockResolvedValue({ value: { counts: { active: 0, idle: 0, completed: 0 }, items: [], truncated: false } });
    expect((await getButlerBoardIfChanged({ requestIfChanged: older } as unknown as ApiClient, {}, undefined))?.board.operatorActions).toEqual([]);
  });

  it("says what the auto-check shows", () => {
    const check = { kind: "tcp_reachable", params: {} };
    expect(operatorActionCheckLabel({})).toBe("No auto-check: mark it done when finished");
    expect(operatorActionCheckLabel({ check })).toBe("Auto-check tcp_reachable: not run yet");
    expect(operatorActionCheckLabel({ check, lastCheck: { passed: false, detail: "refused", checkedAt: "t" } })).toBe("Auto-check tcp_reachable: not yet (refused)");
    expect(operatorActionCheckLabel({ check, lastCheck: { passed: null, detail: "", checkedAt: "t" } })).toBe("Auto-check tcp_reachable: unknown");
    expect(operatorActionCheckLabel({ check, lastCheck: { passed: true, detail: "open", checkedAt: "t" } })).toBe("Auto-check tcp_reachable passes (open); closing");
  });

  it("marks done or cancels through a Work check-in that closes the item", async () => {
    const request = vi.fn().mockResolvedValue({});
    const api = { request } as unknown as ApiClient;
    await resolveOperatorAction(api, { id: "w 1", project: { id: "p/1", name: "Research" } }, "done");
    await resolveOperatorAction(api, { id: "w2", project: { id: "p1", name: "Research" } }, "cancelled");
    expect(request.mock.calls[0][0]).toBe("engagements/p%2F1/work/w%201/updates");
    expect(JSON.parse(request.mock.calls[0][1].body)).toEqual({ summary: OPERATOR_ACTION_DONE_SUMMARY, status: "done", request_id: "board-operator-action:w 1" });
    expect(JSON.parse(request.mock.calls[1][1].body)).toMatchObject({ summary: OPERATOR_ACTION_CANCEL_SUMMARY, status: "done" });
    // Core reads a check-in starting with this text as a cancel.
    expect(OPERATOR_ACTION_CANCEL_SUMMARY.startsWith("Operator cancelled this request")).toBe(true);
  });
});
