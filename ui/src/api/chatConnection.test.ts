import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiClient } from "./client";
import type { ChatCompletionRequest, ChatStreamEvent } from "./types";

const request: ChatCompletionRequest = {backend: "harness", harnessProfileId: "grok", model: "fixture", messages: [{role: "user", content: "One request"}]};
const frame = (value: unknown) => new TextEncoder().encode(`data: ${JSON.stringify(value)}\n\n`);
const started = {type: "started", turn_id: "chat-turn", session_id: "chat", harness_turn_id: "harness-turn", harness_profile_id: "grok", model: "fixture"};
const done = {type: "done", turn_id: "chat-turn", session_id: "chat", harness_turn_id: "harness-turn", harness_profile_id: "grok", backend: "harness", model: "fixture", message: {role: "assistant", content: "First tail", id: "answer"}, usage: {}};
function stream(values: unknown[], broken = false) {
  let index = 0;
  return new Response(new ReadableStream<Uint8Array>({pull(controller) {
    if (index < values.length) controller.enqueue(frame(values[index++]));
    else if (broken) controller.error(new TypeError("Network lost"));
    else controller.close();
  }}));
}
class Socket extends EventTarget {
  static instances: Socket[] = [];
  constructor(public url: URL, public protocols: string[]) { super(); Socket.instances.push(this); }
  close = vi.fn();
  sendFrame(value: unknown) { this.dispatchEvent(new MessageEvent("message", {data: JSON.stringify(value)})); }
  closed(code = 1006) { this.dispatchEvent(Object.assign(new Event("close"), {code, reason: ""})); }
}

beforeEach(() => { vi.useFakeTimers(); Socket.instances = []; vi.stubGlobal("WebSocket", Socket); });
afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals(); });

describe("chat viewer reconnection", () => {
  it("reattaches a broken accepted stream with GET and skips duplicated sequences", async () => {
    const delta = {type: "message_delta", sequence: 2, delta: "First ", model: "fixture"};
    const fetch = vi.fn<typeof globalThis.fetch>().mockResolvedValueOnce(stream([started, delta], true))
      .mockResolvedValueOnce(stream([delta, {...delta, sequence: 3, delta: "tail"}, done]));
    const client = new ApiClient({baseUrl: "http://localhost:8765", fetch});
    const events: ChatStreamEvent[] = [];
    const pending = client.streamChat(request, event => events.push(event));
    await vi.advanceTimersByTimeAsync(1000);
    expect((await pending)?.message.content).toBe("First tail");
    expect(fetch).toHaveBeenCalledTimes(2);
    expect(fetch.mock.calls[0][1]?.method).toBe("POST");
    expect(fetch.mock.calls[1][0]).toContain("/chat/turns/chat-turn/events?after=2");
    expect(fetch.mock.calls[1][1]).toMatchObject({method: "GET", body: undefined});
    expect(events.flatMap(event => event.type === "message_delta" && "delta" in event ? [event.delta] : [])).toEqual(["First ", "tail"]);
    expect(events.filter(event => event.type === "connection").map(event => event.state)).toEqual(["reconnecting", "connected"]);
  });

  it("does not resubmit a request whose acceptance was never received", async () => {
    const fetch = vi.fn<typeof globalThis.fetch>().mockRejectedValue(new TypeError("offline"));
    const client = new ApiClient({baseUrl: "http://localhost:8765", fetch});
    await expect(client.streamChat(request, () => {})).rejects.toThrow("before acceptance was confirmed");
    expect(fetch).toHaveBeenCalledOnce();
  });

  it("stops recovery on detach and does not retry execution errors", async () => {
    const fetch = vi.fn<typeof globalThis.fetch>().mockResolvedValueOnce(stream([started], true));
    const client = new ApiClient({baseUrl: "http://localhost:8765", fetch});
    const controller = new AbortController();
    const pending = client.streamChat(request, event => { if (event.type === "connection") controller.abort(); }, controller.signal);
    await expect(pending).rejects.toMatchObject({name: "AbortError"});
    await vi.advanceTimersByTimeAsync(60_000);
    expect(fetch).toHaveBeenCalledOnce();
    fetch.mockResolvedValueOnce(stream([started, {type: "error", detail: "Harness interrupted"}]));
    await expect(client.streamChat(request, () => {})).rejects.toThrow("Harness interrupted");
    expect(fetch).toHaveBeenCalledTimes(2);
  });

  it("follows restored provider work without calling POST resume", async () => {
    const fetch = vi.fn<typeof globalThis.fetch>().mockResolvedValue(stream([done]));
    const client = new ApiClient({baseUrl: "http://localhost:8765", fetch});
    await client.followChatTurn("chat-turn", request, () => {});
    expect(fetch.mock.calls[0][0]).toContain("/events?after=0");
    expect(fetch.mock.calls[0][1]?.method).toBe("GET");
  });

  it("reconnects a closed socket from its last delivered event and ignores the old socket", async () => {
    const client = new ApiClient({baseUrl: "http://localhost:8765"});
    const receive = vi.fn(), complete = vi.fn(), failure = vi.fn(), connection = vi.fn();
    const detach = client.followHarnessTurnEvents("turn", 4, receive, complete, failure, connection);
    const first = Socket.instances[0];
    first.sendFrame({kind: "event", event: {type: "output_delta", sequence: 5, delta: "one"}});
    first.closed();
    await vi.advanceTimersByTimeAsync(1000);
    const second = Socket.instances[1];
    expect(second.url.searchParams.get("after")).toBe("5");
    first.sendFrame({kind: "event", event: {type: "output_delta", sequence: 6, delta: "stale"}});
    second.sendFrame({kind: "event", event: {type: "output_delta", sequence: 5, delta: "duplicate"}});
    second.sendFrame({kind: "event", event: {type: "output_delta", sequence: 6, delta: "two"}});
    second.sendFrame({kind: "complete"});
    second.closed(1000);
    await vi.advanceTimersByTimeAsync(60_000);
    expect(receive.mock.calls.map(([event]) => event.delta)).toEqual(["one", "two"]);
    expect(complete).toHaveBeenCalledOnce();
    expect(failure).not.toHaveBeenCalled();
    expect(Socket.instances).toHaveLength(2);
    detach();
  });

  it("detects silent sockets, wakes recovery online, and cancels retry on detach", async () => {
    const client = new ApiClient({baseUrl: "http://localhost:8765"});
    const detach = client.followHarnessTurnEvents("turn", 0, () => {});
    await vi.advanceTimersByTimeAsync(45_000);
    globalThis.dispatchEvent(new Event("online"));
    await vi.advanceTimersByTimeAsync(0);
    expect(Socket.instances).toHaveLength(2);
    Socket.instances[1].closed();
    detach();
    await vi.advanceTimersByTimeAsync(60_000);
    expect(Socket.instances).toHaveLength(2);
  });

  it("does not reconnect rejected authentication", async () => {
    const client = new ApiClient({baseUrl: "http://localhost:8765"});
    const failure = vi.fn();
    client.followHarnessTurnEvents("turn", 0, () => {}, undefined, failure);
    Socket.instances[0].closed(4401);
    await vi.advanceTimersByTimeAsync(60_000);
    expect(failure).toHaveBeenCalledOnce();
    expect(Socket.instances).toHaveLength(1);
  });
});
