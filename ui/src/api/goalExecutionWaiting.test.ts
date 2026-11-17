import { expect, it, vi } from "vitest";
import { ApiClient } from "./client";

function goalResponse(execution: unknown) {
  return new Response(JSON.stringify({
    id: "goal-1", engagement_id: "project", session_id: "session",
    objective: "Continue research", completion_criteria: ["Record evidence"],
    plan: [], current_step: 4, status: "running", elapsed_seconds: 0,
    children_started: 0, usage: {input_tokens: 0, output_tokens: 0, total_tokens: 0},
    linked_turn_ids: [], completion_evidence: [], skill_snapshots: [], revision: 17,
    metadata: {}, execution,
  }), {status: 200});
}

it("maps why Core says a running goal is not executing", async () => {
  const fetchMock = vi.fn<typeof fetch>()
    .mockResolvedValueOnce(goalResponse({reason: "held", detail: "Project execution is held: recovery is unconfirmed"}))
    .mockResolvedValueOnce(goalResponse(null))
    .mockResolvedValueOnce(goalResponse({reason: "unknown", detail: "ignored"}));
  const client = new ApiClient({baseUrl: "http://127.0.0.1:8765", fetch: fetchMock});
  await expect(client.getChatGoal("session")).resolves.toMatchObject({
    status: "running",
    executionWaiting: {reason: "held", detail: "Project execution is held: recovery is unconfirmed"},
  });
  expect((await client.getChatGoal("session")).executionWaiting).toBeUndefined();
  expect((await client.getChatGoal("session")).executionWaiting).toBeUndefined();
});
