import { expect, it, vi } from "vitest";
import { ApiClient } from "./client";

it("maps both saved goal handoff links from Core", async () => {
  const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(new Response(JSON.stringify({
    id: "goal-1", engagement_id: "project", session_id: "session",
    objective: "Continue research", completion_criteria: ["Record evidence"],
    plan: [], current_step: 0, status: "paused", elapsed_seconds: 0,
    children_started: 0, usage: {input_tokens: 0, output_tokens: 0, total_tokens: 0},
    linked_turn_ids: [], completion_evidence: [], skill_snapshots: [], revision: 3,
    metadata: {
      handoff_source_session_id: "old-chat",
      handoff_target_session_id: "new-chat",
      handoff_reason: "Unsupported planning adapter",
    },
  }), {status: 200}));
  const client = new ApiClient({baseUrl: "http://127.0.0.1:8765", fetch: fetchMock});
  await expect(client.getChatGoal("session")).resolves.toMatchObject({
    handoffSourceSessionId: "old-chat",
    handoffTargetSessionId: "new-chat",
    handoffReason: "Unsupported planning adapter",
  });
});
