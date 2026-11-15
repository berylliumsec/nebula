import { describe, expect, it, vi } from "vitest";
import { ApiClient } from "../api/client";
import { reduceHarnessActivity, subagentStoppable, type HarnessActivityItem } from "./harnessActivity";

// Core's subagent events for one Claude task lifecycle, as
// nebula.v3.harnesses._claude_task_event and the stop route record them.
function task(sequence: number, itemId: string, status: string, payload: Record<string, unknown>) {
  return {
    schema_version: "nebula.harness-activity/v2", id: `event-${sequence}`, sequence, type: "item_upsert",
    vendor: "claude_agent_sdk", harness_turn_id: "turn", item_id: itemId, parent_item_id: `tool-${itemId}`,
    item_kind: "subagent", item_status: status, title: "Scan hosts", artifact_ids: [], payload,
  };
}
const flagged = (itemId: string, stoppable: boolean) => ({ task_id: itemId, task_type: "local_agent", last_tool_name: null, output_file: null, usage: null, patch: null, stoppable });
const CORE_EVENTS = [
  task(1, "t1", "running", flagged("t1", true)), // TaskStartedMessage
  task(2, "t1", "streaming", flagged("t1", true)), // TaskProgressMessage
  task(3, "t2", "queued", { ...flagged("t2", true), patch: { status: "pending" } }), // pending patch
  task(4, "t1", "cancelled", { task_id: "t1" }), // POST .../tasks/t1/stop
  task(5, "t2", "waiting_input", { ...flagged("t2", false), patch: { status: "paused" } }),
  task(6, "t3", "completed", flagged("t3", false)),
];

async function itemsAfter(count: number): Promise<HarnessActivityItem[]> {
  const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(new Response(JSON.stringify({ events: CORE_EVENTS.slice(0, count), next_sequence: count }), { status: 200 }));
  const page = await new ApiClient({ baseUrl: "http://core.test", fetch: fetchMock }).getHarnessTurnEvents("turn");
  return page.events.reduce<HarnessActivityItem[]>((items, event) => reduceHarnessActivity(items, event, "assistant"), []);
}
const find = (items: HarnessActivityItem[], id: string) => items.find((item) => item.itemId === id);

describe("subagent Stop follows Core's stoppable flag", () => {
  it("offers Stop for running, streaming and queued tasks", async () => {
    expect(subagentStoppable(find(await itemsAfter(1), "t1"))).toBe(true);
    const progressed = await itemsAfter(3);
    expect(find(progressed, "t1")?.status).toBe("streaming");
    expect(subagentStoppable(find(progressed, "t1"))).toBe(true);
    expect(find(progressed, "t2")?.status).toBe("queued");
    expect(subagentStoppable(find(progressed, "t2"))).toBe(true);
  });

  it("withdraws Stop once Core records the stop, though the merged payload keeps the old flag", async () => {
    const stopped = find(await itemsAfter(4), "t1");
    expect(stopped?.status).toBe("cancelled");
    expect(stopped?.payload.stoppable).toBe(true);
    expect(subagentStoppable(stopped)).toBe(false);
  });

  it("hides Stop for paused and finished tasks and for non-subagent items", async () => {
    const items = await itemsAfter(6);
    expect(subagentStoppable(find(items, "t2"))).toBe(false);
    expect(subagentStoppable(find(items, "t3"))).toBe(false);
    expect(subagentStoppable(undefined)).toBe(false);
    const running = find(await itemsAfter(1), "t1")!;
    expect(subagentStoppable({ ...running, kind: "tool" })).toBe(false);
    // A Core that sends a running task with stoppable=false is believed.
    expect(subagentStoppable({ ...running, payload: { ...running.payload, stoppable: false } })).toBe(false);
    // An event without the flag (older Core) is stoppable while active.
    expect(subagentStoppable({ ...running, payload: { task_id: "t1" } })).toBe(true);
  });
});
