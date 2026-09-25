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

  it("advances Now to newer completed work instead of pinning stale streaming context", () => {
    const model = activityLedgerFromHarness("Work summary", "streaming", [
      harnessItem({
        key: "goal",
        kind: "goal",
        title: "AirPlay research",
        summary: "Revisit the active goal.",
        sequence: 1,
        goal: {
          objective: "Revisit AirPlay research.",
          currentStep: "Revisit the active goal.",
          status: "running",
          childAgents: 0,
        },
      }),
      harnessItem({ key: "narrative", kind: "reasoning", title: "Revisit the active goal", status: "streaming", sequence: 2 }),
      harnessItem({ key: "tool", kind: "tool", title: "Refresh AirPlay sources", status: "completed", sequence: 3 }),
    ]);

    expect(model.currentAction).toBe("Refresh AirPlay sources");
  });

  it("keeps unresolved operator attention ahead of newer routine activity", () => {
    const model = activityLedgerFromHarness("Work summary", "streaming", [
      harnessItem({ key: "approval", kind: "tool", title: "Approve network access", status: "waiting_approval", sequence: 1 }),
      harnessItem({ key: "tool", kind: "tool", title: "Refresh local index", status: "completed", sequence: 2 }),
    ]);

    expect(model.currentAction).toBe("Approve network access");
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

  it("names an older saved subagent wait as delegated work", () => {
    const model = activityLedgerFromNative("Work summary", "streaming", [{
      assistantId: "assistant-1",
      toolCallId: "wait-1",
      capability: "wait_subagents",
      displayName: "Wait for subagents",
      status: "waiting_callback",
      summary: "Waiting for 2 subagents to report.",
      evidenceIds: [],
      artifacts: [],
    }]);
    expect(model.entries[0]).toMatchObject({
      label: "Collect delegated reports",
      phase: "delegation",
      summary: "Waiting for delegated work.",
    });
  });

  it("names a running SSH call and prioritizes it over an earlier failure", () => {
    const model = activityLedgerFromNative("Work summary", "streaming", [
      { assistantId: "assistant-1", toolCallId: "failed-load", capability: "tool_catalog.load", status: "failed", summary: "Invalid input.", evidenceIds: [], artifacts: [] },
      { assistantId: "assistant-1", toolCallId: "running-ssh", capability: "ssh.simulation-host.run_command", displayName: "Command runtime", status: "running", arguments: { command: "inspect corpus_index.json" }, evidenceIds: [], artifacts: [] },
    ]);
    expect(model.status).toBe("active");
    expect(model.currentAction).toBe("Command on simulation-host");
    expect(model.entries[0].label).toBe("Command on simulation-host");
    expect(model.entries[0].sourceTool?.arguments).toEqual({ command: "inspect corpus_index.json" });
    expect(model.entries[1].status).toBe("failed");
  });

  it("names a brokered MCP call by its server and tool, not its routing digest", () => {
    const model = activityLedgerFromNative("Work summary", "complete", [{
      assistantId: "assistant-1",
      toolCallId: "tool-1",
      capability: "mcp.9a4c1f0b77de.create_issue",
      displayName: "GitHub · create_issue",
      status: "complete",
      evidenceIds: [],
      artifacts: [],
    }]);
    expect(model.entries[0].label).toBe("GitHub · create_issue");
  });

  it("still drops the digest for MCP calls recorded before Core sent a readable name", () => {
    const model = activityLedgerFromNative("Work summary", "complete", [{
      assistantId: "assistant-1",
      toolCallId: "tool-1",
      capability: "mcp.9a4c1f0b77de.create_issue",
      status: "complete",
      evidenceIds: [],
      artifacts: [],
    }]);
    expect(model.entries[0].label).toBe("create_issue");
  });

  it("names the MCP server a harness tool call reached", () => {
    const model = activityLedgerFromHarness("Work summary", "complete", [
      harnessItem({ key: "mcp", kind: "tool", title: "read_file", serverId: "workspace", status: "completed" }),
      harnessItem({ key: "own", kind: "tool", title: "Grep", serverId: "claude", status: "completed", sequence: 2 }),
    ]);
    expect(model.entries.map((entry) => entry.label)).toEqual(["Grep", "workspace · read_file"]);
  });

  it("prefers the readable name Core sends with a brokered call on a harness turn", () => {
    const model = activityLedgerFromHarness("Work summary", "complete", [
      harnessItem({
        key: "brokered",
        kind: "tool",
        title: "mcp.9a4c1f0b77de.create_issue",
        status: "completed",
        payload: { display_name: "GitHub · create_issue" },
      }),
    ]);
    expect(model.entries[0].label).toBe("GitHub · create_issue");
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
