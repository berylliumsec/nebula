import {describe, expect, it, vi} from "vitest";
import {ApiClient, chatRequestBody} from "./client";
import type {ChatStreamEvent} from "./types";

describe("chat task planning transport", () => {
  it("sends the explicit planning choice and reads durable task activity", async () => {
    const requestBody = chatRequestBody({
      providerId: "provider", engagementId: "project", model: "model",
      messages: [{role: "user", content: "Inspect files"}], planningEnabled: true,
    }, true);
    expect(requestBody.planning_enabled).toBe(true);
    expect(chatRequestBody({providerId: "provider", messages: [{role: "user", content: "Hello"}]}, true)).not.toHaveProperty("planning_enabled");
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(new Response(JSON.stringify({plans: [{id: "plan", turn_id: "turn", phase: "discovery"}], directives: []}), {status: 200}));
    const client = new ApiClient({baseUrl: "http://127.0.0.1:8765", fetch: fetchMock});
    await expect(client.getChatTaskPlans("session")).resolves.toMatchObject({plans: [{id: "plan", phase: "discovery"}]});
    expect(String(fetchMock.mock.calls[0][0])).toBe("http://127.0.0.1:8765/api/v1/chat/sessions/session/task-plans");
  });

  it("preserves ordered plan phases from Core's stream", async () => {
    const plan = {id: "plan", session_id: "chat", turn_id: "turn", schema_version: 1, plan_version: 1, phase: "discovery", steps: [], tool_names: [], native_categories: [], files: [], no_file_work: false, unresolved_dependencies: [], revision: 1};
    const frames = [
      {type: "started", turn_id: "turn", session_id: "chat", provider_id: "provider", model: "model", sequence: 1, epoch: "runtime-a"},
      {type: "task_plan", plan, turn_id: "turn", sequence: 2, epoch: "runtime-a"},
      {type: "task_plan", plan: {...plan, phase: "execution", revision: 2}, turn_id: "turn", sequence: 3, epoch: "runtime-a"},
      {type: "done", turn_id: "turn", session_id: "chat", provider_id: "provider", backend: "provider", model: "model", message: {id: "answer", role: "assistant", content: "Done"}, usage: {input_tokens: 1, output_tokens: 1, total_tokens: 2}, citations: [], sequence: 4, epoch: "runtime-a"},
    ];
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(new Response(frames.map(frame => `data: ${JSON.stringify(frame)}\n\n`).join(""), {status: 200, headers: {"content-type": "text/event-stream"}}));
    const client = new ApiClient({baseUrl: "http://127.0.0.1:8765", fetch: fetchMock});
    const events: ChatStreamEvent[] = [];
    await client.streamChat({backend: "provider", providerId: "provider", model: "model", messages: [{role: "user", content: "Inspect"}]}, event => events.push(event));
    expect(events.filter(event => event.type === "task_plan").map(event => event.plan.phase)).toEqual(["discovery", "execution"]);
  });
});
