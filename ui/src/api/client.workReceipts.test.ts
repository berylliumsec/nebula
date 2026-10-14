import { expect, it, vi } from "vitest";
import { ApiClient } from "./client";
import type { ChatStreamEvent } from "./types";

it("passes only successful Nebula work-start structured results to the control receipt UI", async () => {
  const id = "70d5a305-6f30-4270-b27d-2322d7385fbf";
  const frame = (event: Record<string, unknown>) => `event: ${event.type}\ndata: ${JSON.stringify(event)}\n\n`;
  const tool = (toolName: string, result: Record<string, unknown>, itemId: string) => ({
    type: "tool_completed", harness_turn_id: "turn", item_id: itemId,
    server_id: "nebula", tool_name: toolName, payload: { result },
  });
  const frames = [
    tool("nebula.work.start_chat", { structuredContent: { session_id: id } }, "valid"),
    tool("other.tool", { structuredContent: { session_id: id } }, "other"),
    tool("nebula.work.start_chat", { isError: true, structuredContent: { session_id: id } }, "failed"),
    { type: "done", session_id: "control", harness_profile_id: "harness", model: "model",
      message: { id: "answer", role: "assistant", content: "Done" } },
  ];
  const body = new TextEncoder().encode(frames.map(frame).join(""));
  const stream = new ReadableStream<Uint8Array>({ start(controller) { controller.enqueue(body); controller.close(); } });
  const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(new Response(stream, {
    status: 200, headers: { "content-type": "text/event-stream" },
  }));
  const client = new ApiClient({ baseUrl: "http://127.0.0.1:8765", token: "test-token", fetch: fetchMock });
  const events: ChatStreamEvent[] = [];
  await client.streamChat({ backend: "harness", harnessProfileId: "harness", engagementId: "project",
    model: "model", messages: [{ role: "user", content: "Start it" }] }, event => events.push(event));
  expect(events.filter((event) => event.type === "tool_completed").map((event) => event.receipt)).toEqual([
    { session_id: id }, undefined, undefined,
  ]);
});
