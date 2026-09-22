import { expect, it, vi } from "vitest";
import { ApiClient, chatRequestBody } from "./client";

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

it("sends a harness choice still being verified for Core to remember, only when there is one", () => {
  const messages = [{ role: "user" as const, content: "Split it" }];
  expect(chatRequestBody({
    backend: "harness",
    harnessProfileId: "codex",
    pendingProviderSubagent: { providerId: "openrouter", model: "deepseek/deepseek-v3.2", maxActive: 2 },
    messages,
  }, true)).toMatchObject({
    pending_provider_subagent: { provider_profile_id: "openrouter", model: "deepseek/deepseek-v3.2", max_active: 2 },
  });
  const unlimited = chatRequestBody({
    backend: "harness",
    harnessProfileId: "codex",
    pendingProviderSubagent: { providerId: "openrouter", model: "deepseek/deepseek-v3.2" },
    messages,
  }, true);
  expect(unlimited.pending_provider_subagent).toEqual({ provider_profile_id: "openrouter", model: "deepseek/deepseek-v3.2" });
  // An older remote Core forbids unknown request fields, so none is left out.
  expect(chatRequestBody({ backend: "harness", harnessProfileId: "codex", messages }, true)).not.toHaveProperty("pending_provider_subagent");
});

it("creates a goal conversation with the composer's subagent choice and effort", async () => {
  const session = {
    id: "goal-chat", engagement_id: "engagement-1", title: "Split the review", backend: "provider",
    provider_profile_id: "provider", model: "model-a", revision: 1,
    created_at: "2026-09-21T10:00:00Z", updated_at: "2026-09-21T10:00:00Z",
    metadata: { allow_subagents: true, max_active_subagents: 2, reasoning_effort: "high" },
  };
  const goal = {
    id: "goal-1", engagement_id: "engagement-1", session_id: "goal-chat", objective: "Split the review",
    completion_criteria: ["Every area is reviewed"], plan: [], current_step: 0, status: "draft", revision: 1,
  };
  const fetchMock = vi.fn<typeof fetch>().mockImplementation(async () => new Response(JSON.stringify({ session, goal }), { status: 201 }));
  const client = new ApiClient({ baseUrl: "http://127.0.0.1:8765", fetch: fetchMock });
  const draft = {
    engagementId: "engagement-1", providerId: "provider", model: "model-a", toolsEnabled: false,
    mcpServerIds: [], hookIds: [], objective: "Split the review", completionCriteria: ["Every area is reviewed"],
  };

  const created = await client.createChatGoalConversation({ ...draft, allowSubagents: true, maxActiveSubagents: 2, reasoningEffort: "high" });

  expect(created.session).toMatchObject({ allowSubagents: true, subagentLimit: 2, reasoningEffort: "high" });
  expect(JSON.parse(String(fetchMock.mock.calls[0][1]?.body))).toMatchObject({
    allow_subagents: true, max_active_subagents: 2, reasoning_effort: "high",
  });
  await client.createChatGoalConversation(draft);
  const plain = JSON.parse(String(fetchMock.mock.calls[1][1]?.body));
  expect(plain).not.toHaveProperty("allow_subagents");
  expect(plain).not.toHaveProperty("max_active_subagents");
  expect(plain).not.toHaveProperty("reasoning_effort");
});
