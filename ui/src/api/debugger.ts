import type { DebugProtocol } from "@vscode/debugprotocol";
import type { DebugSessionStart } from "./types";
import { websocketAuthProtocol } from "./events";

export type DebugTransportState = "connecting" | "ready" | "closed" | "failed";

interface DebugTransportOptions {
  apiBaseUrl: string;
  token?: string;
  session: DebugSessionStart;
  websocketFactory?: (url: string, protocols: string[]) => WebSocket;
  onEvent(event: DebugProtocol.Event): void;
  onAdapterOutput?(output: string): void;
  onState?(state: DebugTransportState, detail?: string): void;
}

function websocketUrl(baseUrl: string, path: string): string {
  const url = new URL(path, baseUrl.endsWith("/") ? baseUrl : `${baseUrl}/`);
  url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
  return url.toString();
}

export class DebugTransport {
  private sequence = 1;
  private socket?: WebSocket;
  private readonly pending = new Map<
    number,
    { resolve(value: DebugProtocol.Response): void; reject(error: Error): void }
  >();

  constructor(private readonly options: DebugTransportOptions) {}

  connect(): Promise<void> {
    if (typeof WebSocket === "undefined" && !this.options.websocketFactory) {
      return Promise.reject(
        new Error("This browser does not support WebSockets."),
      );
    }
    this.options.onState?.("connecting");
    const protocols = [
      this.options.session.protocol,
      `nebula.ticket.${this.options.session.websocketTicket}`,
    ];
    if (this.options.token)
      protocols.push(websocketAuthProtocol(this.options.token));
    const factory =
      this.options.websocketFactory ??
      ((url: string, offered: string[]) => new WebSocket(url, offered));
    const socket = factory(
      websocketUrl(this.options.apiBaseUrl, this.options.session.websocketPath),
      protocols,
    );
    this.socket = socket;
    return new Promise((resolve, reject) => {
      const failed = (detail: string) => {
        const error = new Error(detail);
        this.options.onState?.("failed", detail);
        reject(error);
      };
      socket.addEventListener(
        "open",
        () => {
          this.options.onState?.("ready");
          resolve();
        },
        { once: true },
      );
      socket.addEventListener(
        "error",
        () => failed("The debugger connection could not be opened."),
        { once: true },
      );
      socket.addEventListener("message", (event) =>
        this.onMessage(String(event.data)),
      );
      socket.addEventListener("close", () => {
        const error = new Error("The debugger connection closed.");
        this.pending.forEach(({ reject: rejectPending }) =>
          rejectPending(error),
        );
        this.pending.clear();
        this.options.onState?.("closed", error.message);
      });
    });
  }

  request(command: string, args?: object): Promise<DebugProtocol.Response> {
    if (!this.socket || this.socket.readyState !== WebSocket.OPEN) {
      return Promise.reject(new Error("The debugger is not connected."));
    }
    const request: DebugProtocol.Request = {
      seq: this.sequence++,
      type: "request",
      command,
      arguments: args,
    };
    return new Promise((resolve, reject) => {
      this.pending.set(request.seq, { resolve, reject });
      this.socket?.send(JSON.stringify(request));
    });
  }

  close(): void {
    if (
      this.socket?.readyState === WebSocket.CONNECTING ||
      this.socket?.readyState === WebSocket.OPEN
    ) {
      this.socket.close(1000, "Debugger stopped");
    }
  }

  private onMessage(raw: string): void {
    let frame: {
      kind?: string;
      message?: DebugProtocol.ProtocolMessage;
      output?: string;
      detail?: string;
    };
    try {
      frame = JSON.parse(raw) as typeof frame;
    } catch {
      this.options.onState?.(
        "failed",
        "Core returned an invalid debugger message.",
      );
      return;
    }
    if (frame.kind === "adapterOutput" && typeof frame.output === "string") {
      this.options.onAdapterOutput?.(frame.output);
      return;
    }
    if (frame.kind === "error") {
      this.options.onState?.(
        "failed",
        frame.detail ?? "The debugger rejected a request.",
      );
      return;
    }
    const message = frame.message;
    if (frame.kind !== "dap" || !message || typeof message !== "object") return;
    if (message.type === "response") {
      const response = message as DebugProtocol.Response;
      const pending = this.pending.get(response.request_seq);
      if (!pending) return;
      this.pending.delete(response.request_seq);
      if (response.success) pending.resolve(response);
      else
        pending.reject(
          new Error(response.message || `${response.command} failed.`),
        );
    } else if (message.type === "event") {
      this.options.onEvent(message as DebugProtocol.Event);
    }
  }
}
