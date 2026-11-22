import { describe, expect, it } from "vitest";
import { goalChatProposalFromReceipt } from "./butlerBoard";
import { groupTurnsByStep, mapButlerBrief, turnChips } from "./butlerBrief";
import { fileMarks } from "./fileAttribution";

describe("Butler mode chat brief", () => {
  const wire = {
    session_id: "chat-1", title: "Kernel fuzz", project: { id: "p1", name: "Bounty" }, state: "waiting", backend: "harness",
    model: "gpt", started_at: "2026-11-20T10:00:00Z", updated_at: "2026-11-20T12:00:00Z", turn_count: 212, cost_usd: 3.4, subagent_count: 2,
    parent_session_id: null,
    goal: { objective: "Fuzz IOKit", completion_criteria: "Findings written", status: "active", plan: ["Map", "Write", "Install"], active_plan_index: 2, turns_used: 212, blocked_reason: null },
    attention: { kind: "approval", action_id: "approval-1", summary: "Wants to install", command: "sudo make install" },
    turns: [
      { turn_id: "t210", index: 210, status: "complete", started_at: "x", finished_at: "y", line: "Built the harness.", plan_index: 2, counts: { commands: 3, edits: 1, approvals: 0, tools: 5 }, files: ["fuzz/fuzz.c"] },
      { turn_id: "t5", index: 5, status: "complete", started_at: "x", finished_at: null, line: "Mapped the surface.", plan_index: null, counts: { commands: 0, edits: 0, approvals: 1, tools: 1 }, files: [] },
    ],
    older_before: 5,
    files: [{ path: "fuzz/fuzz.c", last_turn_id: "t210", last_changed_at: "y" }],
  };

  it("maps Core's snake_case brief and keeps optional fields absent rather than null", () => {
    const brief = mapButlerBrief(wire);
    expect(brief.sessionId).toBe("chat-1");
    expect(brief.goal?.activePlanIndex).toBe(2);
    expect(brief.goal?.blockedReason).toBeUndefined();
    expect(brief.attention).toEqual({ kind: "approval", actionId: "approval-1", summary: "Wants to install", command: "sudo make install" });
    expect(brief.parentSessionId).toBeUndefined();
    expect(brief.turns[1].finishedAt).toBeUndefined();
    expect(brief.olderBefore).toBe(5);
  });

  it("groups turns under their plan step and summarizes each turn's tools", () => {
    const brief = mapButlerBrief(wire);
    const groups = groupTurnsByStep(brief.turns);
    expect(groups.get(2)?.map((turn) => turn.turnId)).toEqual(["t210"]);
    expect(groups.get(undefined)?.map((turn) => turn.turnId)).toEqual(["t5"]);
    expect(turnChips(brief.turns[0])).toEqual(["Ran 3 commands", "Edited 1 file", "1 other tool"]);
    expect(turnChips(brief.turns[1])).toEqual(["1 approval", "1 other tool"]);
  });
});

describe("Butler goal-chat proposals", () => {
  it("reads a proposal from Butler's tool receipt", () => {
    const proposal = goalChatProposalFromReceipt({
      state: "proposed",
      operator_confirmation_required: true,
      client_action: {
        kind: "goal_chat_proposal", sessionId: "draft-1", projectId: "p1", title: "Crash dedupe", objective: "Cluster crashes",
        completionCriteria: ["Report lists every crash"], runtime: { backend: "harness", model: "opus", reasoningEffort: "high" },
        limits: { timeBudgetSeconds: 10800, allowSubagents: true, maxActiveSubagents: 2 }, tools: [{ id: "shell", name: "Host shell" }],
        reportsTo: { sessionId: "parent", title: "Fuzz ptp", kind: "chat" },
      },
    });
    expect(proposal).toMatchObject({ kind: "goal_chat_proposal", sessionId: "draft-1", title: "Crash dedupe", tools: ["Host shell"], completionCriteria: ["Report lists every crash"] });
    expect(proposal?.reportsTo?.title).toBe("Fuzz ptp");
  });

  it("ignores other receipts", () => {
    expect(goalChatProposalFromReceipt({ client_action: { kind: "records_refresh" } })).toBeUndefined();
    expect(goalChatProposalFromReceipt(undefined)).toBeUndefined();
  });
});

describe("Code & files attribution marks", () => {
  it("marks each changed file and the folders above it with the most urgent chat state", () => {
    const marks = fileMarks([
      { path: "fuzz/iokit/fuzz.c", sessionId: "a", sessionTitle: "A", changedAt: "x", state: "working" },
      { path: "fuzz/iokit/Makefile", sessionId: "b", sessionTitle: "B", changedAt: "x", state: "waiting" },
      { path: "README.md", sessionId: "c", sessionTitle: "C", changedAt: "x", state: "idle" },
    ]);
    expect(marks["fuzz/iokit/fuzz.c"]).toBe("working");
    expect(marks["fuzz/iokit"]).toBe("waiting");
    expect(marks.fuzz).toBe("waiting");
    expect(marks["README.md"]).toBe("idle");
  });
});
