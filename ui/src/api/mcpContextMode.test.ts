import {expect, it, vi} from "vitest";
import {ApiClient, chatRequestBody} from "./client";
import {emptyAssistantDefaults} from "./assistantDefaults";
import {settingCatalog, settingCatalogText} from "../settingsCatalog";

const settings = {
  backend: "provider", profile_id: "local", model: "model-a", mcp_server_ids: ["notes"],
  hook_ids: [], tools_enabled: false, include_knowledge: false,
};
const response = (value: unknown) => new Response(JSON.stringify(value), {
  status: 200, headers: {"content-type": "application/json"},
});
const clientWith = (fetchMock: typeof fetch) => new ApiClient({
  baseUrl: "http://127.0.0.1:8765", token: "synthetic", fetch: fetchMock,
});

it("defaults new UI conversations to on-demand and sends only provider modes", () => {
  const defaults = emptyAssistantDefaults();
  expect(defaults.mcpContextMode).toBe("on_demand");
  const body = {providerId: "local", messages: [{role: "user" as const, content: "Hi"}], mcpContextMode: defaults.mcpContextMode};
  expect(chatRequestBody(body, false)).toMatchObject({mcp_context_mode: "on_demand"});
  expect(chatRequestBody({...body, backend: "harness"}, false)).not.toHaveProperty("mcp_context_mode");
});

it("reads legacy chats as always-loaded and patches an observed next-turn mode", async () => {
  const fetchMock = vi.fn<typeof fetch>()
    .mockResolvedValueOnce(response({session_id: "chat", revision: 1, settings}))
    .mockResolvedValueOnce(response({session_id: "chat", revision: 2, settings: {...settings, mcp_context_mode: "on_demand"}}));
  const client = clientWith(fetchMock);
  expect((await client.getChatAssistantConfig("chat")).settings.mcpContextMode).toBe("always_loaded");
  expect((await client.updateChatAssistantConfig("chat", 1, {mcpContextMode: "on_demand"})).settings.mcpContextMode).toBe("on_demand");
  expect(JSON.parse(String(fetchMock.mock.calls[1][1]?.body))).toEqual({
    expected_revision: 1, settings: {mcp_context_mode: "on_demand"},
  });
});

it("persists project defaults and carries explicit modes into new goal conversations", async () => {
  const session = {
    id: "goal-chat", engagement_id: "project", title: "Synthetic goal", backend: "provider",
    provider_profile_id: "local", metadata: {}, revision: 1,
  };
  const fetchMock = vi.fn<typeof fetch>()
    .mockResolvedValueOnce(response({...emptyAssistantDefaults(), mcp_context_mode: "always_loaded", mcp_server_ids: ["notes"]}))
    .mockResolvedValueOnce(response({session, goal: {id: "goal", objective: "Read notes", completion_criteria: [], metadata: {}}}));
  const client = clientWith(fetchMock);
  const saved = await client.updateEngagementAssistantDefaults("project", {mcpContextMode: "always_loaded"});
  expect(saved.mcpContextMode).toBe("always_loaded");
  await client.createChatGoalConversation({
    engagementId: "project", model: "model-a", toolsEnabled: false,
    mcpServerIds: ["notes"], mcpContextMode: "on_demand", hookIds: [],
    objective: "Read notes", completionCriteria: ["Read"],
  });
  expect(JSON.parse(String(fetchMock.mock.calls[0][1]?.body))).toMatchObject({changes: {mcp_context_mode: "always_loaded"}});
  expect(JSON.parse(String(fetchMock.mock.calls[1][1]?.body))).toMatchObject({mcp_context_mode: "on_demand"});
});

it("makes MCP schema modes discoverable to the settings assistant", () => {
  for (const query of ["MCP tool context", "load schemas on demand", "always loaded"]) {
    expect(settingCatalog.some(item => item.id === "settings.mcp" && settingCatalogText(item).toLowerCase().includes(query.toLowerCase()))).toBe(true);
  }
});
