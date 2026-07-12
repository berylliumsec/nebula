export type TerminalConnectionState = "connecting" | "connected" | "disconnected" | "error";

export interface TerminalConnectOptions {
  sessionId: string;
  columns: number;
  rows: number;
  signal: AbortSignal;
  onData: (data: string) => void;
  onStateChange: (state: TerminalConnectionState, message?: string) => void;
}

export interface TerminalTransport {
  connect(options: TerminalConnectOptions): void;
  send(data: string): void;
  resize(columns: number, rows: number): void;
  disconnect(): void;
}

function encodeProtocolToken(token: string): string {
  const bytes = new TextEncoder().encode(token);
  let binary = "";
  bytes.forEach((byte) => (binary += String.fromCharCode(byte)));
  return `nebula.auth.${btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "")}`;
}

/** Browser/Tauri boundary for a human-controlled PTY session. */
export class ApiTerminalTransport implements TerminalTransport {
  private socket?: WebSocket;

  constructor(private readonly apiBaseUrl: string, private readonly token?: string) {}

  connect(options: TerminalConnectOptions): void {
    options.onStateChange("connecting");
    const url = new URL(
      `${this.apiBaseUrl.replace(/\/$/, "")}/sessions/${encodeURIComponent(options.sessionId)}/terminal/ws`,
    );
    url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
    url.searchParams.set("columns", String(options.columns));
    url.searchParams.set("rows", String(options.rows));
    const protocols = ["nebula.terminal.v1"];
    if (this.token) protocols.push(encodeProtocolToken(this.token));
    const socket = new WebSocket(url, protocols);
    this.socket = socket;

    socket.addEventListener("open", () => options.onStateChange("connected"));
    socket.addEventListener("message", (event) => options.onData(String(event.data)));
    socket.addEventListener("error", () => options.onStateChange("error", "Terminal transport failed"));
    socket.addEventListener("close", () => options.onStateChange("disconnected"));
    options.signal.addEventListener("abort", () => this.disconnect(), { once: true });
  }

  send(data: string): void {
    if (this.socket?.readyState === WebSocket.OPEN) {
      this.socket.send(JSON.stringify({ type: "input", data }));
    }
  }

  resize(columns: number, rows: number): void {
    if (this.socket?.readyState === WebSocket.OPEN) {
      this.socket.send(JSON.stringify({ type: "resize", columns, rows }));
    }
  }

  disconnect(): void {
    this.socket?.close(1000, "Terminal closed");
    this.socket = undefined;
  }
}

/** Safe placeholder: it never executes commands or falls back to a host shell. */
export class UnavailableTerminalTransport implements TerminalTransport {
  connect(options: TerminalConnectOptions): void {
    options.onData(
      "\r\n\x1b[1;38;5;111mNebula isolated terminal\x1b[0m\r\n" +
        "Connect Nebula Core and a certified PTY runner to begin a human session.\r\n" +
        "Agent tool execution uses a separate policy-controlled sandbox.\r\n\r\n",
    );
    options.onStateChange("disconnected", "Runner unavailable");
  }

  send(): void {}
  resize(): void {}
  disconnect(): void {}
}
