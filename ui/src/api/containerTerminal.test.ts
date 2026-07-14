import { afterEach, describe, expect, it, vi } from "vitest";
import { ContainerTerminalSocket } from "./containerTerminal";

class MockTerminalWebSocket extends EventTarget {
  static instance?: MockTerminalWebSocket;
  static instances: MockTerminalWebSocket[] = [];
  readonly url: string;
  readonly protocols: string[];
  readyState = 1;
  sent: string[] = [];

  constructor(url: string, protocols: string[]) {
    super();
    this.url = url;
    this.protocols = protocols;
    MockTerminalWebSocket.instance = this;
    MockTerminalWebSocket.instances.push(this);
  }

  send(value: string): void {
    this.sent.push(value);
  }

  close(code = 1000, reason = ""): void {
    this.readyState = 3;
    this.dispatchEvent(new CloseEvent("close", { code, reason }));
  }
}

describe("ContainerTerminalSocket", () => {
  afterEach(() => {
    MockTerminalWebSocket.instance = undefined;
    MockTerminalWebSocket.instances = [];
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  it("keeps auth and the one-use ticket out of the URL and decodes raw output", () => {
    const states: string[] = [];
    const output: Uint8Array[] = [];
    const exits: Array<{ outcome: string; exitCode?: number }> = [];
    const socket = new ContainerTerminalSocket({
      apiBaseUrl: "https://nebula.test/api/v1",
      token: "secret-token",
      session: {
        sessionId: "terminal-1",
        websocketTicket: "one-use-ticket",
        ticketExpiresAt: "2026-07-13T18:00:00Z",
        websocketPath: "/api/v1/container-terminals/terminal-1/ws",
        reconnectGraceSeconds: 600,
        replayMaxBytes: 1_048_576,
        lastSequence: 0,
      },
      websocketFactory: (url, protocols) => new MockTerminalWebSocket(url, protocols) as unknown as WebSocket,
      onState: (state) => states.push(state),
      onOutput: (data) => output.push(data),
      onExit: (result) => exits.push(result),
    });

    socket.connect();
    const transport = MockTerminalWebSocket.instance!;
    expect(transport.url).toBe("wss://nebula.test/api/v1/container-terminals/terminal-1/ws?after_sequence=0");
    expect(transport.url).not.toContain("secret-token");
    expect(transport.url).not.toContain("one-use-ticket");
    expect(transport.protocols[0]).toBe("nebula.container-terminal.v1");
    expect(transport.protocols[1]).toMatch(/^nebula\.auth\./);
    expect(transport.protocols[2]).toBe("nebula.ticket.one-use-ticket");

    transport.dispatchEvent(new MessageEvent("message", { data: JSON.stringify({ type: "ready", max_duration_seconds: 86_400, idle_timeout_seconds: 1_800, reconnect_ticket: "reconnect-ticket", reconnect_grace_seconds: 600, replay_max_bytes: 1_048_576 }) }));
    transport.dispatchEvent(new MessageEvent("message", { data: JSON.stringify({ type: "output", sequence: 1, encoding: "base64", data: "AP9PSw==" }) }));
    expect(states).toContain("ready");
    expect([...output[0]]).toEqual([0, 255, 79, 75]);

    socket.sendInput("whoami\r");
    socket.resize(120, 40);
    expect(transport.sent.map((value) => JSON.parse(value))).toEqual([
      { type: "input", data: "whoami\r" },
      { type: "resize", columns: 120, rows: 40 },
    ]);

    transport.dispatchEvent(new MessageEvent("message", { data: JSON.stringify({ type: "exit", exit_code: 7, outcome: "completed" }) }));
    expect(exits).toEqual([{ exitCode: 7, outcome: "completed" }]);
    transport.close();
    expect(exits).toHaveLength(1);
  });

  it("reports malformed output and requests a server-side close", () => {
    const errors: string[] = [];
    const socket = new ContainerTerminalSocket({
      apiBaseUrl: "http://127.0.0.1:8765/api/v1",
      session: {
        sessionId: "terminal-2",
        websocketTicket: "ticket-2",
        ticketExpiresAt: "2026-07-13T18:00:00Z",
        websocketPath: "/api/v1/container-terminals/terminal-2/ws",
        reconnectGraceSeconds: 600,
        replayMaxBytes: 1_048_576,
        lastSequence: 0,
      },
      websocketFactory: (url, protocols) => new MockTerminalWebSocket(url, protocols) as unknown as WebSocket,
      onOutput: vi.fn(),
      onError: (_code, detail) => errors.push(detail),
    });
    socket.connect();
    const transport = MockTerminalWebSocket.instance!;
    transport.dispatchEvent(new MessageEvent("message", { data: "not-json" }));
    expect(errors[0]).toMatch(/malformed/i);
    socket.requestClose();
    expect(JSON.parse(transport.sent[0])).toEqual({ type: "close" });
  });

  it("retries a recovered ticket until the previous route attachment detaches", async () => {
    vi.useFakeTimers();
    const states: string[] = [];
    const errors: string[] = [];
    const exits: string[] = [];
    const socket = new ContainerTerminalSocket({
      apiBaseUrl: "http://127.0.0.1:8765/api/v1",
      session: {
        sessionId: "terminal-recovered",
        websocketTicket: "fresh-recovery-ticket",
        ticketExpiresAt: "2026-07-13T18:00:00Z",
        websocketPath: "/api/v1/container-terminals/terminal-recovered/ws",
        reconnectGraceSeconds: 600,
        replayMaxBytes: 1_048_576,
        lastSequence: 0,
      },
      websocketFactory: (url, protocols) => new MockTerminalWebSocket(url, protocols) as unknown as WebSocket,
      onState: (state) => states.push(state),
      onOutput: vi.fn(),
      onError: (_code, detail) => errors.push(detail),
      onExit: ({ outcome }) => exits.push(outcome),
    });

    socket.connect();
    const blocked = MockTerminalWebSocket.instance!;
    blocked.close(4409, "terminal already has an active WebSocket attachment");
    expect(states.at(-1)).toBe("reconnecting");
    expect(errors).toEqual([]);
    expect(exits).toEqual([]);

    await vi.advanceTimersByTimeAsync(250);
    const retried = MockTerminalWebSocket.instance!;
    expect(retried).not.toBe(blocked);
    expect(retried.protocols.at(-1)).toBe("nebula.ticket.fresh-recovery-ticket");
    retried.dispatchEvent(new MessageEvent("message", { data: JSON.stringify({
      type: "ready",
      reconnect_ticket: "rotated-after-attach",
      reconnect_grace_seconds: 600,
      replay_max_bytes: 1_048_576,
    }) }));
    expect(states.at(-1)).toBe("ready");
    expect(MockTerminalWebSocket.instances).toHaveLength(2);
    expect(exits).toEqual([]);
    socket.dispose();
  });

  it("reconnects with a rotated ticket, requests missed output, and ignores duplicates", async () => {
    vi.useFakeTimers();
    const errors: Array<{ code: string; detail: string }> = [];
    const exits: Array<{ outcome: string }> = [];
    const states: string[] = [];
    const output: number[][] = [];
    const session = {
      sessionId: "terminal-3",
      websocketTicket: "ticket-3",
      ticketExpiresAt: "2026-07-13T18:00:00Z",
      websocketPath: "/api/v1/container-terminals/terminal-3/ws",
      reconnectGraceSeconds: 600,
      replayMaxBytes: 1_048_576,
      lastSequence: 0,
    };
    const socket = new ContainerTerminalSocket({
      apiBaseUrl: "http://127.0.0.1:8765/api/v1",
      session,
      websocketFactory: (url, protocols) => new MockTerminalWebSocket(url, protocols) as unknown as WebSocket,
      onState: (state) => states.push(state),
      onOutput: (data) => output.push([...data]),
      onError: (code, detail) => errors.push({ code, detail }),
      onExit: (result) => exits.push(result),
    });

    socket.connect();
    const first = MockTerminalWebSocket.instance!;
    first.dispatchEvent(new MessageEvent("message", { data: JSON.stringify({ type: "ready", reconnect_ticket: "reconnect-1", reconnect_grace_seconds: 600, replay_max_bytes: 1_048_576 }) }));
    first.dispatchEvent(new MessageEvent("message", { data: JSON.stringify({ type: "output", sequence: 1, encoding: "base64", data: "QQ==" }) }));
    first.close(1006);
    expect(states.at(-1)).toBe("reconnecting");
    expect(exits).toEqual([]);
    expect(errors).toEqual([]);

    await vi.advanceTimersByTimeAsync(250);
    const second = MockTerminalWebSocket.instance!;
    expect(second).not.toBe(first);
    expect(second.url).toBe("ws://127.0.0.1:8765/api/v1/container-terminals/terminal-3/ws?after_sequence=1");
    expect(second.protocols.at(-1)).toBe("nebula.ticket.reconnect-1");
    second.dispatchEvent(new MessageEvent("message", { data: JSON.stringify({ type: "ready", reconnect_ticket: "reconnect-2", reconnect_grace_seconds: 600, replay_max_bytes: 1_048_576 }) }));
    second.dispatchEvent(new MessageEvent("message", { data: JSON.stringify({ type: "output", sequence: 1, encoding: "base64", data: "QQ==" }) }));
    second.dispatchEvent(new MessageEvent("message", { data: JSON.stringify({ type: "output", sequence: 2, encoding: "base64", data: "Qg==" }) }));
    expect(output).toEqual([[65], [66]]);
    expect(session.websocketTicket).toBe("reconnect-2");
    expect(session.lastSequence).toBe(2);

    socket.dispose();
    expect(second.sent).toEqual([]);
    expect(exits).toEqual([]);
  });
});
