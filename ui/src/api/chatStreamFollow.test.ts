import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiClient } from "./client";
import type { ChatCompletionRequest, ChatStreamEvent } from "./types";

vi.mock("../diagnostics", () => ({
  logCaughtDiagnostic: vi.fn(),
  logDiagnostic: vi.fn(),
  newOperationId: () => "operation",
  rememberDiagnosticErrorPresentation: vi.fn(),
}));

const request: ChatCompletionRequest = {backend: "provider", providerId: "provider", model: "model", messages: [{role: "user", content: "Run it"}]};
const encode = (value: unknown) => new TextEncoder().encode(`data: ${JSON.stringify(value)}\n\n`);
function stream(values: unknown[], broken = false) {
  let index = 0;
  return new Response(new ReadableStream<Uint8Array>({pull(controller) {
    if (index < values.length) controller.enqueue(encode(values[index++]));
    else if (broken) controller.error(new TypeError("Network lost"));
    else controller.close();
  }}), {status: 200, headers: {"content-type": "text/event-stream"}});
}
const started = (epoch: string) => ({type: "started", turn_id: "turn", session_id: "chat", provider_id: "provider", model: "model", sequence: 1, epoch});
const done = {type: "done", turn_id: "turn", session_id: "chat", provider_id: "provider", backend: "provider", model: "model", message: {id: "answer", role: "assistant", content: "saved new"}, usage: {input_tokens: 1, output_tokens: 1, total_tokens: 2}, citations: []};
const seen = (events: ChatStreamEvent[]) => events.map(event => event.type === "connection" ? `connection:${event.state}` : event.type);

beforeEach(() => { vi.useFakeTimers(); });
afterEach(() => { vi.useRealTimers(); });

describe("provider stream follow", () => {
  it("ends cleanly when the turn parks for a callback or a subagent wait", async () => {
    // Exactly what Core sends before a tool turn parks on WAITING_CALLBACK.
    const fetch = vi.fn<typeof globalThis.fetch>().mockResolvedValueOnce(stream([
      started("runtime-a"),
      {type: "tool_started", turn_id: "turn", tool_call_id: "wait", capability: "wait_subagents", sequence: 2, epoch: "runtime-a"},
      {type: "callback_required", turn_id: "turn", tool_call_id: "wait", wait_kind: "subagents", subagent_ids: ["child-a", "child-b"], summary: "Waiting for 2 subagents to report.", sequence: 3, epoch: "runtime-a"},
    ]));
    const client = new ApiClient({baseUrl: "http://core/api/v1", fetch});
    const events: ChatStreamEvent[] = [];

    await expect(client.streamChat(request, event => events.push(event))).resolves.toBeUndefined();

    expect(seen(events)).toEqual(["started", "tool_started", "callback_required"]);
    expect(events.at(-1)).toMatchObject({type: "callback_required", waitKind: "subagents", subagentIds: ["child-a", "child-b"], summary: "Waiting for 2 subagents to report."});
    // No reconnect, so no follow request that Core would refuse with a 409.
    expect(fetch).toHaveBeenCalledOnce();
  });

  it("names a command callback wait", async () => {
    const fetch = vi.fn<typeof globalThis.fetch>().mockResolvedValueOnce(stream([
      started("runtime-a"),
      {type: "callback_required", turn_id: "turn", tool_call_id: "command", process_id: "process", results_url: "http://core/results", summary: "Waiting for the command to POST results.", sequence: 2, epoch: "runtime-a"},
    ]));
    const client = new ApiClient({baseUrl: "http://core/api/v1", fetch});
    const events: ChatStreamEvent[] = [];
    await client.streamChat(request, event => events.push(event));
    expect(events.at(-1)).toMatchObject({type: "callback_required", waitKind: "process", processId: "process", resultsUrl: "http://core/results"});
    expect(fetch).toHaveBeenCalledOnce();
  });

  it("replays a turn Core resumed in a new runtime instead of skipping it", async () => {
    // The old runtime sent 40 frames, then Core restarted.
    const before = [started("runtime-a"), ...Array.from({length: 39}, (_, index) => ({type: "delta", turn_id: "turn", delta: "x", sequence: index + 2, epoch: "runtime-a"}))];
    // The resumed runtime counts from 1 again and replays the saved text first.
    const after = [
      {type: "queued", turn_id: "turn", sequence: 1, epoch: "runtime-b"},
      {...started("runtime-b"), sequence: 2},
      {type: "delta", turn_id: "turn", delta: "saved", sequence: 3, epoch: "runtime-b"},
      {type: "delta", turn_id: "turn", delta: " new", sequence: 4, epoch: "runtime-b"},
      {...done, sequence: 5, epoch: "runtime-b"},
    ];
    const fetch = vi.fn<typeof globalThis.fetch>()
      .mockResolvedValueOnce(stream(before, true))
      .mockResolvedValueOnce(stream(after));
    const client = new ApiClient({baseUrl: "http://core/api/v1", fetch});
    const events: ChatStreamEvent[] = [];
    const pending = client.streamChat(request, event => events.push(event));
    await vi.advanceTimersByTimeAsync(1_000);

    expect((await pending)?.message.content).toBe("saved new");
    expect(fetch).toHaveBeenCalledTimes(2);
    expect(fetch.mock.calls[1][0]).toBe("http://core/api/v1/chat/turns/turn/events?after=40&epoch=runtime-a");
    const replay = events.slice(events.findIndex(event => event.type === "restarted"));
    expect(seen(replay)).toEqual(["restarted", "started", "delta", "delta", "done"]);
    expect(replay.flatMap(event => event.type === "delta" ? [event.delta] : [])).toEqual(["saved", " new"]);
  });

  it("keeps deduplicating frames of the same runtime across a reconnect", async () => {
    const delta = (sequence: number, text: string) => ({type: "delta", turn_id: "turn", delta: text, sequence, epoch: "runtime-a"});
    const fetch = vi.fn<typeof globalThis.fetch>()
      .mockResolvedValueOnce(stream([started("runtime-a"), delta(2, "first ")], true))
      .mockResolvedValueOnce(stream([delta(2, "first "), delta(3, "tail"), {...done, message: {...done.message, content: "first tail"}}]));
    const client = new ApiClient({baseUrl: "http://core/api/v1", fetch});
    const events: ChatStreamEvent[] = [];
    const pending = client.streamChat(request, event => events.push(event));
    await vi.advanceTimersByTimeAsync(1_000);
    await pending;
    expect(fetch.mock.calls[1][0]).toBe("http://core/api/v1/chat/turns/turn/events?after=2&epoch=runtime-a");
    expect(events.some(event => event.type === "restarted")).toBe(false);
    expect(events.flatMap(event => event.type === "delta" ? [event.delta] : [])).toEqual(["first ", "tail"]);
  });

  it("receives a retained pause frame after the connection drops just before it", async () => {
    const fetch = vi.fn<typeof globalThis.fetch>()
      .mockResolvedValueOnce(stream([started("runtime-a")], true))
      .mockResolvedValueOnce(stream([
        {type: "approval_required", turn_id: "turn", tool_call_id: "call", approval: {id: "approval"}, sequence: 2, epoch: "runtime-a"},
      ]));
    const client = new ApiClient({baseUrl: "http://core/api/v1", fetch});
    const events: ChatStreamEvent[] = [];
    const pending = client.streamChat(request, event => events.push(event));
    await vi.advanceTimersByTimeAsync(1_000);
    await expect(pending).resolves.toBeUndefined();
    expect(seen(events)).toEqual(["started", "connection:reconnecting", "connection:connected", "approval_required"]);
  });
});
