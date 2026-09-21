import { expect, it, vi } from "vitest";
import { ApiClient } from "./client";

it("saves a subagent choice on an existing conversation and clears its limit", async () => {
  const wire = {
    id: "session-1", engagement_id: "engagement-1", title: "Chat", backend: "provider",
    provider_profile_id: "provider", model: "model-a", revision: 4,
    created_at: "2026-09-21T10:00:00Z", updated_at: "2026-09-21T10:00:00Z",
    metadata: { allow_subagents: true, max_active_subagents: null },
  };
  const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(new Response(JSON.stringify(wire), { status: 200 }));
  const client = new ApiClient({ baseUrl: "http://127.0.0.1:8765", fetch: fetchMock });

  const updated = await client.updateChatSessionAssistantSettings("session-1", {
    allowSubagents: true, maxActiveSubagents: null, expectedRevision: 3,
  });

  expect(updated).toMatchObject({ allowSubagents: true, subagentLimit: undefined });
  expect(JSON.parse(String(fetchMock.mock.calls[0][1]?.body))).toMatchObject({
    allow_subagents: true, max_active_subagents: null, expected_revision: 3,
  });
});
