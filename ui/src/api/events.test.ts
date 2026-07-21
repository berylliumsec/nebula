import { afterEach, describe, expect, it, vi } from "vitest";
import type { RunEvent } from "./types";
import { NebulaEventStream } from "./events";

class MockWebSocket extends EventTarget {
  static instance?: MockWebSocket;
  readonly url: string;
  readonly protocols: string[];

  constructor(url: string | URL, protocols: string | string[]) {
    super();
    this.url = String(url);
    this.protocols = typeof protocols === "string" ? [protocols] : protocols;
    MockWebSocket.instance = this;
  }

  close(): void {
    this.dispatchEvent(new CloseEvent("close", { code: 1000 }));
  }
}

describe("NebulaEventStream", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("authenticates out of band and advances only monotonically", () => {
    vi.stubGlobal("WebSocket", MockWebSocket);
    const received: RunEvent[] = [];
    const stream = new NebulaEventStream({
      apiBaseUrl: "https://nebula.test/api/v1",
      token: "secret-token",
      cursor: { after: 41, engagementId: "engagement-1", runId: "run-1" },
      onEvent: (event) => received.push(event),
    });

    stream.connect();
    const socket = MockWebSocket.instance!;
    expect(socket.url).toContain("wss://nebula.test/api/v1/runs/run-1/events/ws?after=41&engagement_id=engagement-1");
    expect(socket.url).not.toContain("secret-token");
    expect(socket.protocols[0]).toBe("nebula.events.v1");
    expect(socket.protocols[1]).toMatch(/^nebula\.auth\./);

    const event: RunEvent = {
      sequence: 42,
      id: "event-42",
      kind: "system.notice",
      occurredAt: "2026-07-12T19:00:00Z",
      summary: "Ready",
      payload: {},
    };
    socket.dispatchEvent(new MessageEvent("message", { data: JSON.stringify(event) }));
    socket.dispatchEvent(new MessageEvent("message", { data: JSON.stringify(event) }));
    expect(received).toHaveLength(1);
    expect(stream.lastSequence).toBe(42);
    stream.disconnect();
  });

  it("supports run-scoped Core replay frames", () => {
    vi.stubGlobal("WebSocket", MockWebSocket);
    const received: RunEvent[] = [];
    const stream = new NebulaEventStream({
      apiBaseUrl: "http://127.0.0.1:8765/api/v1",
      cursor: { after: 7, runId: "run one" },
      onEvent: (event) => received.push(event),
    });
    stream.connect();
    const socket = MockWebSocket.instance!;
    expect(socket.url).toContain("/runs/run%20one/events/ws?after=7");
    socket.dispatchEvent(new MessageEvent("message", {
      data: JSON.stringify({
        kind: "event",
        event: {
          id: "event-8",
          run_id: "run one",
          sequence: 8,
          event_type: "tool.completed",
          actor_id: "agent-2",
          occurred_at: "2026-07-12T19:00:00Z",
          payload: { summary: "Tool finished" },
        },
      }),
    }));
    expect(received[0]).toMatchObject({
      sequence: 8,
      kind: "tool.completed",
      runId: "run one",
      actor: "agent-2",
      summary: "Tool finished",
    });
    stream.disconnect();
  });

  it("marks a stream unsupported until a run is selected", () => {
    vi.stubGlobal("WebSocket", MockWebSocket);
    MockWebSocket.instance = undefined;
    const onStateChange = vi.fn();
    const stream = new NebulaEventStream({
      apiBaseUrl: "http://127.0.0.1:8765/api/v1",
      onEvent: vi.fn(),
      onStateChange,
    });
    stream.connect();
    expect(onStateChange).toHaveBeenLastCalledWith("unsupported");
    expect(MockWebSocket.instance).toBeUndefined();
  });
});
