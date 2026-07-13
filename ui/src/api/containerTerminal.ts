import type { ContainerTerminalSession } from "./types";
import { websocketAuthProtocol } from "./events";

export type ContainerTerminalSocketState =
  | "connecting"
  | "ready"
  | "closing"
  | "closed"
  | "error";

export interface ContainerTerminalExit {
  exitCode?: number;
  outcome: string;
}

export interface ContainerTerminalSocketOptions {
  apiBaseUrl: string;
  token?: string;
  session: ContainerTerminalSession;
  websocketFactory?: (url: string, protocols: string[]) => WebSocket;
  onState?: (state: ContainerTerminalSocketState) => void;
  onOutput: (data: Uint8Array) => void;
  onReady?: (limits: { maxDurationSeconds: number; idleTimeoutSeconds: number }) => void;
  onExit?: (result: ContainerTerminalExit) => void;
  onError?: (code: string, detail: string) => void;
}

const TERMINAL_PROTOCOL = "nebula.container-terminal.v1";

function socketUrl(apiBaseUrl: string, path: string): string {
  const url = new URL(path, apiBaseUrl);
  url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
  return url.toString();
}

function decodeBase64(value: string): Uint8Array {
  const decoded = globalThis.atob(value);
  const result = new Uint8Array(decoded.length);
  for (let index = 0; index < decoded.length; index += 1) {
    result[index] = decoded.charCodeAt(index);
  }
  return result;
}

export class ContainerTerminalSocket {
  private socket?: WebSocket;
  private exitReceived = false;

  constructor(private readonly options: ContainerTerminalSocketOptions) {}

  connect(): void {
    if (this.socket) return;
    this.setState("connecting");
    const protocols = [TERMINAL_PROTOCOL];
    if (this.options.token) protocols.push(websocketAuthProtocol(this.options.token));
    protocols.push(`nebula.ticket.${this.options.session.websocketTicket}`);
    const factory = this.options.websocketFactory ?? ((url, offered) => new WebSocket(url, offered));
    const socket = factory(
      socketUrl(this.options.apiBaseUrl, this.options.session.websocketPath),
      protocols,
    );
    this.socket = socket;
    socket.addEventListener("message", (event) => this.receive(event));
    socket.addEventListener("error", () => {
      this.setState("error");
      this.options.onError?.("connection_error", "Container terminal connection failed.");
    });
    socket.addEventListener("close", () => {
      this.socket = undefined;
      this.setState("closed");
      if (!this.exitReceived) {
        this.exitReceived = true;
        this.options.onExit?.({ outcome: "disconnected" });
      }
    });
  }

  sendInput(data: string): void {
    this.send({ type: "input", data });
  }

  resize(columns: number, rows: number): void {
    this.send({ type: "resize", columns, rows });
  }

  requestClose(): void {
    if (!this.socket || this.socket.readyState !== 1) return;
    this.setState("closing");
    this.send({ type: "close" });
  }

  dispose(): void {
    const socket = this.socket;
    this.socket = undefined;
    if (!socket) return;
    if (socket.readyState === 1) {
      socket.send(JSON.stringify({ type: "close" }));
      socket.close(1000, "terminal view closed");
    } else if (socket.readyState === 0) {
      socket.close();
    }
  }

  private send(frame: Record<string, unknown>): void {
    if (!this.socket || this.socket.readyState !== 1) return;
    this.socket.send(JSON.stringify(frame));
  }

  private receive(event: MessageEvent): void {
    if (typeof event.data !== "string") {
      this.options.onError?.("invalid_frame", "Core sent a non-text terminal frame.");
      return;
    }
    let frame: unknown;
    try {
      frame = JSON.parse(event.data);
    } catch {
      this.options.onError?.("invalid_frame", "Core sent malformed terminal data.");
      return;
    }
    if (!frame || typeof frame !== "object" || !("type" in frame)) {
      this.options.onError?.("invalid_frame", "Core sent an invalid terminal frame.");
      return;
    }
    const value = frame as Record<string, unknown>;
    if (value.type === "ready") {
      this.setState("ready");
      this.options.onReady?.({
        maxDurationSeconds: typeof value.max_duration_seconds === "number" ? value.max_duration_seconds : 0,
        idleTimeoutSeconds: typeof value.idle_timeout_seconds === "number" ? value.idle_timeout_seconds : 0,
      });
      return;
    }
    if (value.type === "output") {
      if (value.encoding !== "base64" || typeof value.data !== "string") {
        this.options.onError?.("invalid_frame", "Core sent invalid terminal output.");
        return;
      }
      try {
        this.options.onOutput(decodeBase64(value.data));
      } catch {
        this.options.onError?.("invalid_frame", "Core sent malformed base64 terminal output.");
      }
      return;
    }
    if (value.type === "error") {
      this.options.onError?.(
        typeof value.code === "string" ? value.code : "terminal_error",
        typeof value.detail === "string" ? value.detail : "Container terminal failed.",
      );
      return;
    }
    if (value.type === "exit") {
      this.exitReceived = true;
      this.setState("closed");
      this.options.onExit?.({
        exitCode: typeof value.exit_code === "number" ? value.exit_code : undefined,
        outcome: typeof value.outcome === "string" ? value.outcome : "completed",
      });
    }
  }

  private setState(state: ContainerTerminalSocketState): void {
    this.options.onState?.(state);
  }
}
