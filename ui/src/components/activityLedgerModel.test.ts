import { describe, expect, it } from "vitest";
import type { AgentRunSummary, RunEvent } from "../api/types";
import type { HarnessActivityItem } from "../pages/harnessActivity";
import {
  activityLedgerFromHarness,
  activityLedgerFromMission,
  activityLedgerFromNative,
  missionLedgerEntries,
  normalizeActivityStatus,
} from "./activityLedgerModel";

function harnessItem(overrides: Partial<HarnessActivityItem>): HarnessActivityItem {
  return {
    assistantId: "assistant-1",
    key: "turn-1:item-1",
    type: "item_upsert",
    status: "running",
    title: "item upsert",
    sequence: 1,
    streams: {},
    payload: {},
    artifactIds: [],
    ...overrides,
  };
}

function run(overrides: Partial<AgentRunSummary> = {}): AgentRunSummary {
  return {
    id: "run-1",
    engagementId: "project-1",
    title: "Perimeter review",
    status: "running",
    updatedAt: "2026-08-26T12:00:00Z",
    completedTasks: 1,
    totalTasks: 3,
    ...overrides,
  };
}

describe("activity ledger presentation model", () => {
  it("normalizes lifecycle statuses and keeps attention distinct", () => {
    expect(normalizeActivityStatus("streaming")).toBe("active");
    expect(normalizeActivityStatus("waiting_approval")).toBe("attention");
    expect(normalizeActivityStatus("interrupted")).toBe("failed");
    expect(normalizeActivityStatus("completed")).toBe("complete");
  });

  it("uses explicit plan phases and meaningful current work without protocol labels", () => {
    const items = [
      harnessItem({
        key: "plan",
        kind: "plan",
        title: "Plan",
        plan: [
          { id: "inspect", title: "Inspect scope", status: "completed" },
          { id: "verify", title: "Verify findings", status: "in_progress" },
        ],
      }),
      harnessItem({
        key: "tool",
        kind: "tool",
        title: "item upsert",
        streams: { commentary: "Saving the verified finding." },
        sequence: 2,
      }),
    ];

    const model = activityLedgerFromHarness("Work summary", "streaming", items);
    expect(model.phases.map((phase) => phase.label)).toEqual(["Inspect scope", "Verify findings"]);
    expect(model.currentAction).toBe("Saving the verified finding.");
    expect(model.currentAction).not.toContain("item upsert");
    expect(model.actionCount).toBe(1);
  });

  it("deduplicates replayed mission harness lifecycle updates", () => {
    const events: RunEvent[] = [
      {
        id: "started",
        sequence: 1,
        kind: "harness.tool_started",
        occurredAt: "2026-08-26T12:00:00Z",
        summary: "mission · Harness tool started",
        payload: { item_id: "tool-1", item_kind: "tool", item_status: "running", title: "Lookup advisory", payload: { query: "CVE-1" } },
      },
      {
        id: "completed",
        sequence: 2,
        kind: "harness.tool_completed",
        occurredAt: "2026-08-26T12:00:02Z",
        summary: "Advisory lookup completed.",
        payload: { item_id: "tool-1", item_kind: "tool", item_status: "completed", title: "Lookup advisory", artifact_ids: ["artifact-1"], payload: { result: "matched" } },
      },
    ];

    const entries = missionLedgerEntries(events);
    expect(entries).toHaveLength(1);
    expect(entries[0]).toMatchObject({ label: "Lookup advisory", status: "complete", sequence: 2 });
    expect(entries[0].payload).toEqual({ query: "CVE-1", result: "matched" });
    const model = activityLedgerFromMission(run(), events);
    expect(model.actionCount).toBe(1);
    expect(model.artifactCount).toBe(1);
  });

  it("keeps failed and waiting work individually visible", () => {
    const model = activityLedgerFromHarness("Work summary", "streaming", [
      harnessItem({ key: "approval", kind: "tool", title: "Run active scan", status: "waiting_approval" }),
      harnessItem({ key: "failure", kind: "command", title: "Verify output", status: "failed", sequence: 2 }),
    ]);
    expect(model.status).toBe("attention");
    expect(model.attentionCount).toBe(2);
    expect(model.entries.filter((entry) => entry.status === "attention" || entry.status === "failed")).toHaveLength(2);
  });

  it("creates the same compact receipt for native-provider tools", () => {
    const model = activityLedgerFromNative("Work summary", "complete", [{
      assistantId: "assistant-1",
      toolCallId: "tool-1",
      capability: "Search evidence",
      status: "complete",
      summary: "Found two relevant artifacts.",
      evidenceIds: ["evidence-1"],
      artifacts: [],
    }]);
    expect(model).toMatchObject({ status: "complete", actionCount: 1, artifactCount: 1 });
    expect(model.entries[0].label).toBe("Search evidence");
  });

  it("uses saved mission stages as the visible live phases", () => {
    const model = activityLedgerFromMission(run({
      stages: [
        { title: "Discover", objective: "Discover services" },
        { title: "Verify", objective: "Verify observations" },
      ],
    }), [{
      id: "stage-1-completed",
      sequence: 1,
      kind: "stage.completed",
      occurredAt: "2026-08-26T12:00:00Z",
      summary: "Discovery completed.",
      payload: { stage_id: "stage-1" },
    }]);
    expect(model.phases.map((phase) => [phase.label, phase.status])).toEqual([
      ["Discover", "complete"],
      ["Verify", "active"],
    ]);
  });
});
