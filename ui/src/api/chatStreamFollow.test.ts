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
const queued = (position: number, sequence: number) => ({type: "queued", turn_id: "turn", queued_at: "2026-09-24T12:00:00Z", queue_position: position, capacity_lane: "direct", detail: "Waiting for Core capacity", sequence, epoch: "runtime-a"});
/** A stream the test feeds frame by frame, as Core writes them over time. */
function controlled() {
  let target!: ReadableStreamDefaultController<Uint8Array>;
  const body = new ReadableStream<Uint8Array>({start(controller) { target = controller; }});
  return {
    response: new Response(body, {status: 200, headers: {"content-type": "text/event-stream"}}),
    push: (value: unknown) => target.enqueue(encode(value)),
    end: () => target.close(),
  };
}

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
    // The resumed runtime queued and started at once: no wait is reported.
    expect(seen(replay)).toEqual(["restarted", "started", "delta", "delta", "done"]);
    expect(replay.flatMap(event => event.type === "delta" ? [event.delta] : [])).toEqual(["saved", " new"]);
  });

  it("reports a capacity wait once it lasts, then each new position and the admission", async () => {
    const frames = controlled();
    const fetch = vi.fn<typeof globalThis.fetch>().mockResolvedValueOnce(frames.response);
    const client = new ApiClient({baseUrl: "http://core/api/v1", fetch});
    const events: ChatStreamEvent[] = [];
    const pending = client.streamChat(request, event => events.push(event));

    // Exactly what Core sends while a turn waits for provider capacity.
    frames.push(queued(3, 1));
    await vi.advanceTimersByTimeAsync(100);
    frames.push(queued(2, 2));
    await vi.advanceTimersByTimeAsync(100);
    expect(seen(events)).toEqual([]);
    await vi.advanceTimersByTimeAsync(300);
    // The wait keeps its start; the report carries the newest position.
    expect(events).toEqual([{type: "queued", turnId: "turn", queuedAt: "2026-09-24T12:00:00Z", queuePosition: 2, capacityLane: "direct", detail: "Waiting for Core capacity"}]);
    frames.push(queued(1, 3));
    await vi.advanceTimersByTimeAsync(0);
    expect(events.flatMap(event => event.type === "queued" ? [event.queuePosition] : [])).toEqual([2, 1]);
    frames.push({type: "admitted", turn_id: "turn", admitted_at: "2026-09-24T12:00:09Z", capacity_lane: "direct", sequence: 4, epoch: "runtime-a"});
    frames.push({...started("runtime-a"), sequence: 5});
    frames.push({...done, sequence: 6, epoch: "runtime-a"});
    frames.end();
    await pending;

    expect(seen(events)).toEqual(["queued", "queued", "admitted", "started", "done"]);
    expect(events[2]).toEqual({type: "admitted", turnId: "turn", admittedAt: "2026-09-24T12:00:09Z", capacityLane: "direct"});
  });

  it("never reports the moment a free slot takes to admit a turn", async () => {
    const frames = controlled();
    const fetch = vi.fn<typeof globalThis.fetch>().mockResolvedValueOnce(frames.response);
    const client = new ApiClient({baseUrl: "http://core/api/v1", fetch});
    const events: ChatStreamEvent[] = [];
    const pending = client.streamChat(request, event => events.push(event));

    frames.push(queued(1, 1));
    await vi.advanceTimersByTimeAsync(40);
    frames.push({type: "admitted", turn_id: "turn", admitted_at: "2026-09-24T12:00:00Z", capacity_lane: "direct", sequence: 2, epoch: "runtime-a"});
    await vi.advanceTimersByTimeAsync(5_000);
    frames.push({...started("runtime-a"), sequence: 3});
    frames.push({...done, sequence: 4, epoch: "runtime-a"});
    frames.end();
    await pending;

    expect(seen(events)).toEqual(["admitted", "started", "done"]);
  });

  it("follows a queued background turn whose position Core has not assigned yet", async () => {
    const frames = controlled();
    const fetch = vi.fn<typeof globalThis.fetch>().mockResolvedValueOnce(frames.response);
    const client = new ApiClient({baseUrl: "http://core/api/v1", fetch});
    const events: ChatStreamEvent[] = [];
    const pending = client.followChatTurn("turn", request, event => events.push(event));
    frames.push({type: "queued", turn_id: "turn", queued_at: "2026-09-24T12:00:00Z", queue_position: null, capacity_lane: "background", detail: "Waiting for Core capacity", sequence: 1, epoch: "runtime-a"});
    await vi.advanceTimersByTimeAsync(500);
    frames.push({...done, sequence: 2, epoch: "runtime-a"});
    frames.end();
    await pending;
    expect(seen(events)).toEqual(["connection:connected", "queued", "done"]);
    expect(events[1]).toMatchObject({type: "queued", turnId: "turn", capacityLane: "background"});
    expect(events[1]).not.toHaveProperty("queuePosition", expect.anything());
  });

  it("drops a held wait when the viewer detaches", async () => {
    const frames = controlled();
    const fetch = vi.fn<typeof globalThis.fetch>().mockResolvedValueOnce(frames.response);
    const client = new ApiClient({baseUrl: "http://core/api/v1", fetch});
    const events: ChatStreamEvent[] = [];
    const viewer = new AbortController();
    const pending = client.streamChat(request, event => events.push(event), viewer.signal);
    frames.push(queued(4, 1));
    await vi.advanceTimersByTimeAsync(100);
    viewer.abort();
    frames.push(queued(3, 2));
    await expect(pending).rejects.toThrow();
    await vi.advanceTimersByTimeAsync(5_000);
    expect(events).toEqual([]);
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
