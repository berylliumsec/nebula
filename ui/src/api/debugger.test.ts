import { describe, expect, it, vi } from "vitest";
import type { DebugSessionStart } from "./types";
import { DebugTransport } from "./debugger";

class MockSocket extends EventTarget {
  readyState: number = WebSocket.CONNECTING;
  sent: string[] = [];

  send(value: string) { this.sent.push(value); }
  open() { this.readyState = WebSocket.OPEN; this.dispatchEvent(new Event("open")); }
  message(value: unknown) { this.dispatchEvent(new MessageEvent("message", { data: JSON.stringify(value) })); }
  close() { this.readyState = WebSocket.CLOSED; this.dispatchEvent(new CloseEvent("close")); }
}

const session: DebugSessionStart = {
  sessionId: "debug-1",
  websocketPath: "/api/v1/debug-sessions/debug-1/ws",
  websocketTicket: "ticket-1",
  protocol: "nebula.debug.v1",
  path: "probe.py",
  sourceSha256: "0".repeat(64),
  imageDigest: `sha256:${"1".repeat(64)}`,
  workspaceAccess: "read-only",
  network: "none",
  expiresAt: "2026-08-24T12:00:00Z",
};

describe("DebugTransport", () => {
  it("uses ticket/auth subprotocols and correlates DAP responses and events", async () => {
    const socket = new MockSocket();
    const onEvent = vi.fn();
    const onOutput = vi.fn();
    let protocols: string[] = [];
    const transport = new DebugTransport({
      apiBaseUrl: "http://127.0.0.1:1420/api/v1",
      token: "secret",
      session,
      websocketFactory: (_url, offered) => {
        protocols = offered;
        return socket as unknown as WebSocket;
      },
      onEvent,
      onAdapterOutput: onOutput,
    });
    const connected = transport.connect();
    socket.open();
    await connected;
    expect(protocols[0]).toBe("nebula.debug.v1");
    expect(protocols).toContain("nebula.ticket.ticket-1");
    expect(protocols.some((item) => item.startsWith("nebula.auth."))).toBe(true);

    const pending = transport.request("threads");
    const request = JSON.parse(socket.sent[0]) as { seq: number };
    socket.message({ kind: "dap", message: { seq: 2, type: "response", request_seq: request.seq, success: true, command: "threads", body: { threads: [] } } });
    await expect(pending).resolves.toMatchObject({ success: true, command: "threads" });

    socket.message({ kind: "dap", message: { seq: 3, type: "event", event: "stopped", body: { reason: "breakpoint", threadId: 1 } } });
    socket.message({ kind: "adapterOutput", output: "adapter detail" });
    expect(onEvent).toHaveBeenCalledWith(expect.objectContaining({ event: "stopped" }));
    expect(onOutput).toHaveBeenCalledWith("adapter detail");
  });
});
