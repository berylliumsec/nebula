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

it("maps an unbound harness preflight without inventing a provider ID", async () => {
  const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(new Response(JSON.stringify({
    session_id: "harness-chat", session_revision: 3,
    current_provider_id: null, current_model: "harness-model",
    target_provider_id: "local", target_model: "provider-model", compatible: true,
    requires_compaction_confirmation: false, estimated_active_input_tokens: 100,
  }), {status: 200, headers: {"content-type": "application/json"}}));
  const client = new ApiClient({baseUrl: "http://127.0.0.1:8765", token: "test-token", fetch: fetchMock});
  const preflight = await client.preflightChatRuntimeSwitch("harness-chat", {
    providerId: "local", model: "provider-model", toolsEnabled: false, expectedSessionRevision: 3,
  });
  expect(preflight.currentProviderId).toBeUndefined();
  expect(preflight.compatible).toBe(true);
  expect(JSON.parse(String(fetchMock.mock.calls[0][1]?.body))).toMatchObject({
    provider_id: "local", model: "provider-model", expected_session_revision: 3,
  });
});

it("reserves one project Butler conversation before a Voice or text turn", async () => {
  const session = {
    id: "control", engagement_id: "project", title: "Nebula assistant", backend: "harness",
    harness_profile_id: "codex", model: "model-a", metadata: {control_assistant: true},
    revision: 1, created_at: "2026-10-15T00:00:00Z", updated_at: "2026-10-15T00:00:00Z",
  };
  const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(new Response(JSON.stringify(session), {
    status: 200, headers: {"content-type": "application/json"},
  }));
  const client = new ApiClient({baseUrl: "http://127.0.0.1:8765", token: "test-token", fetch: fetchMock});
  const saved = await client.getOrCreateControlAssistantSession({
    engagementId: "project", backend: "harness", harnessProfileId: "codex", model: "model-a",
    harnessReasoningEffort: "high", allowCloudToolResults: true,
  });
  expect(saved).toMatchObject({id: "control", engagementId: "project", isControlAssistant: true});
  expect(fetchMock.mock.calls[0][0]).toBe("http://127.0.0.1:8765/api/v1/chat/control-assistant/session");
  expect(fetchMock.mock.calls[0][1]?.method).toBe("POST");
  expect(JSON.parse(String(fetchMock.mock.calls[0][1]?.body))).toMatchObject({
    engagement_id: "project", backend: "harness", harness_profile_id: "codex", model: "model-a",
    harness_reasoning_effort: "high", allow_cloud_tool_results: true,
  });
});
