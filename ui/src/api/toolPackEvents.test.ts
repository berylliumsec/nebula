import { afterEach, describe, expect, it, vi } from "vitest";
import { parseToolPackProgressFrame, ToolPackEventStream, type ToolPackProgressEvent } from "./toolPackEvents";

class MockToolPackWebSocket extends EventTarget {
  static instances: MockToolPackWebSocket[] = [];
  readonly url: string;
  readonly protocols: string[];

  constructor(url: string | URL, protocols: string | string[]) {
    super();
    this.url = String(url);
    this.protocols = typeof protocols === "string" ? [protocols] : protocols;
    MockToolPackWebSocket.instances.push(this);
  }

  close(code = 1000): void {
    this.dispatchEvent(new CloseEvent("close", { code }));
  }
}

function progress(sequence: number, phase: ToolPackProgressEvent["phase"] = "pulling") {
  return {
    kind: "event",
    event: {
      sequence,
      occurred_at: "2026-07-12T19:00:00Z",
      operation_id: "operation-1",
      operation: "install_catalog",
      phase,
      installation_id: "installation-1",
      pack_identity: "berylliumsec/network@1.0.0",
      manifest_digest: "a".repeat(64),
      result_status: phase === "ready" ? "ready" : null,
      internal_detail: "must not cross the UI boundary",
    },
  };
}

describe("ToolPackEventStream", () => {
  afterEach(() => {
    MockToolPackWebSocket.instances = [];
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it("authenticates with subprotocols, sanitizes events, and keeps the token out of the URL", () => {
    vi.stubGlobal("WebSocket", MockToolPackWebSocket);
    const received: ToolPackProgressEvent[] = [];
    const stream = new ToolPackEventStream({
      apiBaseUrl: "https://nebula.test/api/v1",
      token: "secret-token",
      afterSequence: 4,
      onEvent: (event) => received.push(event),
    });

    stream.connect();
    const socket = MockToolPackWebSocket.instances[0];
    expect(socket.url).toBe("wss://nebula.test/api/v1/tool-packs/events/ws?after_sequence=4");
    expect(socket.url).not.toContain("secret-token");
    expect(socket.protocols[0]).toBe("nebula.tool-packs.v1");
    expect(socket.protocols[1]).toMatch(/^nebula\.auth\./);

    socket.dispatchEvent(new MessageEvent("message", { data: JSON.stringify(progress(5)) }));
    socket.dispatchEvent(new MessageEvent("message", { data: JSON.stringify(progress(5, "ready")) }));
    expect(received).toEqual([{
      sequence: 5,
      occurredAt: "2026-07-12T19:00:00Z",
      operationId: "operation-1",
      operation: "install_catalog",
      phase: "pulling",
      installationId: "installation-1",
      packIdentity: "berylliumsec/network@1.0.0",
      manifestDigest: "a".repeat(64),
      resultStatus: undefined,
    }]);
    expect(stream.lastSequence).toBe(5);
    stream.disconnect();
  });

  it("reconnects with the last sequence so Core can replay without duplicates", () => {
    vi.useFakeTimers();
    vi.stubGlobal("WebSocket", MockToolPackWebSocket);
    const states: string[] = [];
    const stream = new ToolPackEventStream({
      apiBaseUrl: "http://127.0.0.1:8765/api/v1",
      afterSequence: 10,
      maxReconnectDelayMs: 500,
      onEvent: vi.fn(),
      onStateChange: (state) => states.push(state),
    });
    stream.connect();
    const first = MockToolPackWebSocket.instances[0];
    first.dispatchEvent(new MessageEvent("message", { data: JSON.stringify(progress(11, "verifying")) }));
    first.dispatchEvent(new CloseEvent("close", { code: 1006 }));
    expect(states).toContain("reconnecting");
    vi.advanceTimersByTime(500);
    expect(MockToolPackWebSocket.instances).toHaveLength(2);
    expect(MockToolPackWebSocket.instances[1].url).toContain("after_sequence=11");
    stream.disconnect();
  });

  it("reports replay gaps and treats auth/platform close codes as unavailable", () => {
    vi.stubGlobal("WebSocket", MockToolPackWebSocket);
    const onReplayGap = vi.fn();
    const onStateChange = vi.fn();
    const stream = new ToolPackEventStream({
      apiBaseUrl: "http://127.0.0.1:8765/api/v1",
      onEvent: vi.fn(),
      onReplayGap,
      onStateChange,
    });
    stream.connect();
    const socket = MockToolPackWebSocket.instances[0];
    socket.dispatchEvent(new MessageEvent("message", { data: JSON.stringify({ kind: "replay_gap", oldest_sequence: 50 }) }));
    expect(onReplayGap).toHaveBeenCalledOnce();
    socket.dispatchEvent(new CloseEvent("close", { code: 4501 }));
    expect(onStateChange).toHaveBeenLastCalledWith("unsupported");
  });

  it("rejects malformed or out-of-contract progress frames", () => {
    expect(parseToolPackProgressFrame(progress(1))).toBeDefined();
    expect(parseToolPackProgressFrame({ ...progress(1), event: { ...progress(1).event, operation: "shell" } })).toBeUndefined();
    expect(parseToolPackProgressFrame({ ...progress(1), event: { ...progress(1).event, manifest_digest: "not-a-digest" } })).toBeUndefined();
    expect(parseToolPackProgressFrame({ ...progress(1), event: { ...progress(1).event, pack_identity: "pack\nspoof" } })?.packIdentity).toBeUndefined();
    expect(parseToolPackProgressFrame({ kind: "heartbeat", after_sequence: 4 })).toBeUndefined();
  });
});
