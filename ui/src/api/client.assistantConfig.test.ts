import { expect, it, vi } from "vitest";
import { ApiClient } from "./client";

it("maps saved next-turn settings and sends a revision-guarded settings change", async () => {
  const settings = {
    backend: "harness", profile_id: "codex", model: "model-a", harness_reasoning_effort: "high",
    harness_service_tier: "fast", harness_mode: "plan", harness_cyber_access_program: "standard",
    mcp_server_ids: ["notebook"], hook_ids: [], include_knowledge: false,
    allow_cloud_knowledge: false, tools_enabled: true, allow_subagents: false,
    allow_agent_messaging: true,
  };
  const session = {
    id: "control", engagement_id: "project", title: "Nebula assistant", backend: "harness",
    harness_profile_id: "codex", harness_session_id: "vendor", model: "model-a",
    metadata: {control_assistant: true, assistant_config: settings}, revision: 4,
    created_at: "2026-10-14T00:00:00Z", updated_at: "2026-10-14T00:00:00Z",
  };
  const fetchMock = vi.fn<typeof fetch>()
    .mockResolvedValueOnce(new Response(JSON.stringify(session), {status: 200, headers: {"content-type": "application/json"}}))
    .mockResolvedValueOnce(new Response(JSON.stringify({session_id: "control", revision: 5, settings: {...settings, harness_service_tier: "default"}}), {status: 200, headers: {"content-type": "application/json"}}));
  const client = new ApiClient({baseUrl: "http://127.0.0.1:8765", token: "test-token", fetch: fetchMock});
  const current = await client.getChatSession("control");
  expect(current.isControlAssistant).toBe(true);
  expect(current.title).toBe("Butler");
  expect(current.assistantConfig).toMatchObject({harnessServiceTier: "fast", mcpServerIds: ["notebook"], allowAgentMessaging: true});
  const updated = await client.updateChatAssistantConfig("control", 4, {harnessServiceTier: "default"});
  expect(updated.settings.harnessServiceTier).toBe("default");
  expect(JSON.parse(String(fetchMock.mock.calls[1][1]?.body))).toEqual({
    expected_revision: 4, settings: {harness_service_tier: "default"},
  });
});
