import { describe, expect, it } from "vitest";
import type { AgentRunSummary, RunEvent, RunEventKind } from "../api/types";
import { evolveRunFromEvent } from "./WorkspaceContext";

function event(kind: RunEventKind, sequence: number, payload: Record<string, unknown> = {}): RunEvent {
  return { id: `event-${sequence}`, sequence, kind, runId: "run-1", occurredAt: `2026-07-12T12:0${sequence}:00Z`, summary: kind, payload };
}

describe("evolveRunFromEvent", () => {
  it("tracks mission lifecycle, planned/completed tasks, and final cost", () => {
    let run: AgentRunSummary = { id: "run-1", engagementId: "engagement-1", title: "Review", status: "queued", updatedAt: "2026-07-12T12:00:00Z", completedTasks: 0, totalTasks: 0 };
    run = evolveRunFromEvent(run, event("run.started", 1));
    expect(run).toMatchObject({ status: "planning", startedAt: "2026-07-12T12:01:00Z" });
    run = evolveRunFromEvent(run, event("run.planned", 2, { tasks: [{ id: "one" }, { id: "two" }] }));
    expect(run).toMatchObject({ status: "running", totalTasks: 2 });
    run = evolveRunFromEvent(run, event("task.completed", 3, { task_id: "one" }));
    expect(run.completedTasks).toBe(1);
    run = evolveRunFromEvent(run, event("run.completed", 4, { cost_usd: 1.25 }));
    expect(run).toMatchObject({ status: "complete", completedTasks: 2, spentUsd: 1.25 });
  });

  it("tracks approval, cancellation, and failure states without changing other runs", () => {
    const run: AgentRunSummary = { id: "run-1", engagementId: "engagement-1", title: "Review", status: "running", updatedAt: "2026-07-12T12:00:00Z", completedTasks: 0, totalTasks: 1 };
    expect(evolveRunFromEvent(run, event("run.waiting_approval", 1)).status).toBe("waiting_approval");
    expect(evolveRunFromEvent(run, event("run.stop_requested", 2)).status).toBe("cancelling");
    expect(evolveRunFromEvent(run, event("run.cancelled", 3)).status).toBe("cancelled");
    expect(evolveRunFromEvent(run, event("run.failed", 4)).status).toBe("failed");
    expect(evolveRunFromEvent(run, { ...event("run.failed", 5), runId: "other" })).toBe(run);
  });
});
