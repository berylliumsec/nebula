import { afterEach, describe, expect, it, vi } from "vitest";
import { ContainerTerminalSocket } from "./containerTerminal";

class MockTerminalWebSocket extends EventTarget {
  static instance?: MockTerminalWebSocket;
  readonly url: string;
  readonly protocols: string[];
  readyState = 1;
  sent: string[] = [];

  constructor(url: string, protocols: string[]) {
    super();
    this.url = url;
    this.protocols = protocols;
    MockTerminalWebSocket.instance = this;
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
      },
      websocketFactory: (url, protocols) => new MockTerminalWebSocket(url, protocols) as unknown as WebSocket,
      onState: (state) => states.push(state),
      onOutput: (data) => output.push(data),
      onExit: (result) => exits.push(result),
    });

    socket.connect();
    const transport = MockTerminalWebSocket.instance!;
    expect(transport.url).toBe("wss://nebula.test/api/v1/container-terminals/terminal-1/ws");
    expect(transport.url).not.toContain("secret-token");
    expect(transport.url).not.toContain("one-use-ticket");
    expect(transport.protocols[0]).toBe("nebula.container-terminal.v1");
    expect(transport.protocols[1]).toMatch(/^nebula\.auth\./);
    expect(transport.protocols[2]).toBe("nebula.ticket.one-use-ticket");

    transport.dispatchEvent(new MessageEvent("message", { data: JSON.stringify({ type: "ready", max_duration_seconds: 1800, idle_timeout_seconds: 900 }) }));
    transport.dispatchEvent(new MessageEvent("message", { data: JSON.stringify({ type: "output", encoding: "base64", data: "AP9PSw==" }) }));
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

  it("reports the WebSocket close reason instead of an opaque error event", () => {
    const errors: Array<{ code: string; detail: string }> = [];
    const exits: Array<{ outcome: string }> = [];
    const socket = new ContainerTerminalSocket({
      apiBaseUrl: "http://127.0.0.1:8765/api/v1",
      session: {
        sessionId: "terminal-3",
        websocketTicket: "ticket-3",
        ticketExpiresAt: "2026-07-13T18:00:00Z",
        websocketPath: "/api/v1/container-terminals/terminal-3/ws",
      },
      websocketFactory: (url, protocols) => new MockTerminalWebSocket(url, protocols) as unknown as WebSocket,
      onOutput: vi.fn(),
      onError: (code, detail) => errors.push({ code, detail }),
      onExit: (result) => exits.push(result),
    });

    socket.connect();
    const transport = MockTerminalWebSocket.instance!;
    transport.dispatchEvent(new Event("error"));
    expect(errors).toEqual([]);
    transport.close(4401, "terminal ticket has already been used");

    expect(errors).toEqual([{
      code: "connection_error",
      detail: "Terminal connection failed: terminal ticket has already been used (WebSocket close code 4401).",
    }]);
    expect(exits).toEqual([{ outcome: "disconnected" }]);
  });
});
