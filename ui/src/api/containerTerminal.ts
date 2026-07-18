import type { ContainerTerminalSession } from "./types";
import { websocketAuthProtocol } from "./events";
import { logCaughtDiagnostic } from "../diagnostics";

export type ContainerTerminalSocketState =
  | "connecting"
  | "reconnecting"
  | "ready"
  | "closing"
  | "closed"
  | "error";

export interface ContainerTerminalExit {
  detail?: string;
  errorCode?: string;
  exitCode?: number;
  outcome: string;
}

export interface ContainerTerminalErrorMetadata {
  errorId?: string;
  requestId?: string;
  retryable?: boolean;
  reasonCode?: string;
  operatorDetail?: string;
  impact?: string;
}

export interface ContainerTerminalSocketOptions {
  apiBaseUrl: string;
  token?: string;
  session: ContainerTerminalSession;
  websocketFactory?: (url: string, protocols: string[]) => WebSocket;
  onState?: (state: ContainerTerminalSocketState) => void;
  onOutput: (data: Uint8Array) => void;
  onReady?: (limits: {
    maxDurationSeconds: number;
    idleTimeoutSeconds: number;
    reconnectGraceSeconds: number;
    replayMaxBytes: number;
  }) => void;
  onExit?: (result: ContainerTerminalExit) => void;
  onError?: (code: string, detail: string, metadata?: ContainerTerminalErrorMetadata) => void;
}

const TERMINAL_PROTOCOL = "nebula.container-terminal.v1";

function socketUrl(apiBaseUrl: string, path: string, afterSequence: number): string {
  const url = new URL(path, apiBaseUrl);
  url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
  url.searchParams.set("after_sequence", String(afterSequence));
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
  private connectionReady = false;
  private disposed = false;
  private closeRequested = false;
  private closeFrameSent = false;
  private currentTicket: string;
  private lastSequence: number;
  private reconnectGraceMilliseconds: number;
  private reconnectAttempt = 0;
  private reconnectDeadline?: number;
  private reconnectTimer?: ReturnType<typeof globalThis.setTimeout>;
  private lastResize?: string;

  constructor(private readonly options: ContainerTerminalSocketOptions) {
    this.currentTicket = options.session.websocketTicket;
    this.lastSequence = options.session.lastSequence;
    this.reconnectGraceMilliseconds = Math.max(
      1,
      options.session.reconnectGraceSeconds,
    ) * 1_000;
  }

  connect(): void {
    this.openSocket(false);
  }

  private openSocket(reconnecting: boolean): void {
    if (
      this.socket
      || this.disposed
      || this.exitReceived
      || (!reconnecting && this.reconnectTimer !== undefined)
    ) return;
    this.connectionReady = false;
    this.setState(reconnecting ? "reconnecting" : "connecting");
    const protocols = [TERMINAL_PROTOCOL];
    if (this.options.token) protocols.push(websocketAuthProtocol(this.options.token));
    protocols.push(`nebula.ticket.${this.currentTicket}`);
    const factory = this.options.websocketFactory ?? ((url, offered) => new WebSocket(url, offered));
    let socket: WebSocket;
    try {
      socket = factory(
        socketUrl(
          this.options.apiBaseUrl,
          this.options.session.websocketPath,
          this.lastSequence,
        ),
        protocols,
      );
    } catch (reason) {
      void logCaughtDiagnostic("interface.container_terminal.caught_failure_01", "A handled interface operation failed.", reason, "container_terminal");
      const suffix = reason instanceof Error && reason.message ? `: ${reason.message}` : ".";
      this.scheduleReconnect(`Terminal connection failed${suffix}`);
      return;
    }
    this.socket = socket;
    this.lastResize = undefined;
    socket.addEventListener("message", (event) => this.receive(event));
    // Browsers expose no actionable detail on the error event. The following
    // close event carries the protocol close code and drives reconnect policy.
    socket.addEventListener("error", () => undefined);
    socket.addEventListener("close", (event) => {
      if (this.socket === socket) this.socket = undefined;
      if (this.disposed) return;
      if (this.exitReceived) return;
      if (this.closeFrameSent) {
        this.failConnection(this.closeDetail(event));
        return;
      }
      this.scheduleReconnect(this.closeDetail(event));
    });
  }

  sendInput(data: string): void {
    this.send({ type: "input", data });
  }

  resize(columns: number, rows: number): void {
    if (!Number.isFinite(columns) || !Number.isFinite(rows)) return;
    const safeColumns = Math.min(1_000, Math.max(1, Math.trunc(columns)));
    const safeRows = Math.min(1_000, Math.max(1, Math.trunc(rows)));
    const signature = `${safeColumns}x${safeRows}`;
    if (signature === this.lastResize) return;
    if (this.send({ type: "resize", columns: safeColumns, rows: safeRows })) {
      this.lastResize = signature;
    }
  }

  requestClose(): void {
    if (this.disposed || this.exitReceived) return;
    this.closeRequested = true;
    this.setState("closing");
    if (this.socket?.readyState === 1) {
      this.closeFrameSent = true;
      this.send({ type: "close" });
    }
  }

  dispose(): void {
    this.disposed = true;
    if (this.reconnectTimer !== undefined) {
      globalThis.clearTimeout(this.reconnectTimer);
      this.reconnectTimer = undefined;
    }
    const socket = this.socket;
    this.socket = undefined;
    if (!socket) return;
    if (socket.readyState === 1) {
      socket.close(1000, "terminal view detached");
    } else if (socket.readyState === 0) {
      socket.close();
    }
  }

  private send(frame: Record<string, unknown>): boolean {
    if (!this.socket || this.socket.readyState !== 1) return false;
    this.socket.send(JSON.stringify(frame));
    return true;
  }

  private receive(event: MessageEvent): void {
    if (typeof event.data !== "string") {
      this.options.onError?.("invalid_frame", "Core sent a non-text terminal frame.");
      return;
    }
    let frame: unknown;
    try {
      frame = JSON.parse(event.data);
    } catch (caughtError) {
      void logCaughtDiagnostic("interface.container_terminal.caught_failure_02", "A handled interface operation failed.", caughtError, "container_terminal");
      this.options.onError?.("invalid_frame", "Core sent malformed terminal data.");
      return;
    }
    if (!frame || typeof frame !== "object" || !("type" in frame)) {
      this.options.onError?.("invalid_frame", "Core sent an invalid terminal frame.");
      return;
    }
    const value = frame as Record<string, unknown>;
    if (value.type === "ready") {
      this.connectionReady = true;
      this.reconnectAttempt = 0;
      this.reconnectDeadline = undefined;
      if (typeof value.reconnect_ticket === "string" && value.reconnect_ticket) {
        this.currentTicket = value.reconnect_ticket;
        this.options.session.websocketTicket = value.reconnect_ticket;
      }
      if (
        typeof value.reconnect_grace_seconds === "number"
        && value.reconnect_grace_seconds > 0
      ) {
        this.options.session.reconnectGraceSeconds = value.reconnect_grace_seconds;
        this.reconnectGraceMilliseconds = value.reconnect_grace_seconds * 1_000;
      }
      if (typeof value.replay_max_bytes === "number" && value.replay_max_bytes > 0) {
        this.options.session.replayMaxBytes = value.replay_max_bytes;
      }
      if (this.closeRequested) {
        this.closeFrameSent = true;
        this.setState("closing");
        this.send({ type: "close" });
        return;
      }
      this.setState("ready");
      this.options.onReady?.({
        maxDurationSeconds: typeof value.max_duration_seconds === "number" ? value.max_duration_seconds : 0,
        idleTimeoutSeconds: typeof value.idle_timeout_seconds === "number" ? value.idle_timeout_seconds : 0,
        reconnectGraceSeconds: this.options.session.reconnectGraceSeconds,
        replayMaxBytes: this.options.session.replayMaxBytes,
      });
      if (value.replay_truncated === true) {
        this.options.onError?.(
          "replay_truncated",
          "Some earlier terminal output is no longer available in the reconnect buffer.",
        );
      }
      return;
    }
    if (value.type === "output") {
      if (
        value.encoding !== "base64"
        || typeof value.data !== "string"
        || typeof value.sequence !== "number"
        || !Number.isSafeInteger(value.sequence)
        || value.sequence < 1
      ) {
        this.options.onError?.("invalid_frame", "Core sent invalid terminal output.");
        return;
      }
      if (value.sequence <= this.lastSequence) return;
      if (value.sequence > this.lastSequence + 1) {
        this.options.onError?.(
          "replay_gap",
          "Some terminal output could not be replayed after reconnecting.",
        );
      }
      try {
        const decoded = decodeBase64(value.data);
        this.lastSequence = value.sequence;
        this.options.session.lastSequence = value.sequence;
        this.options.onOutput(decoded);
      } catch (caughtError) {
        void logCaughtDiagnostic("interface.container_terminal.caught_failure_03", "A handled interface operation failed.", caughtError, "container_terminal");
        this.options.onError?.("invalid_frame", "Core sent malformed base64 terminal output.");
      }
      return;
    }
    if (value.type === "error") {
      this.options.onError?.(
        typeof value.code === "string" ? value.code : "terminal_error",
        typeof value.detail === "string" ? value.detail : "Terminal failed.",
        {
          errorId: typeof value.error_id === "string" ? value.error_id : undefined,
          requestId: typeof value.request_id === "string" ? value.request_id : undefined,
          retryable: typeof value.retryable === "boolean" ? value.retryable : undefined,
          reasonCode: typeof value.reason_code === "string" ? value.reason_code : undefined,
          operatorDetail: typeof value.operator_detail === "string" ? value.operator_detail : undefined,
          impact: typeof value.impact === "string" ? value.impact : undefined,
        },
      );
      return;
    }
    if (value.type === "exit") {
      this.exitReceived = true;
      if (this.reconnectTimer !== undefined) {
        globalThis.clearTimeout(this.reconnectTimer);
        this.reconnectTimer = undefined;
      }
      this.setState("closed");
      this.options.onExit?.({
        detail: typeof value.detail === "string" ? value.detail : undefined,
        errorCode: typeof value.error_code === "string" ? value.error_code : undefined,
        exitCode: typeof value.exit_code === "number" ? value.exit_code : undefined,
        outcome: typeof value.outcome === "string" ? value.outcome : "completed",
      });
    }
  }

  private setState(state: ContainerTerminalSocketState): void {
    this.options.onState?.(state);
  }

  private scheduleReconnect(detail: string): void {
    if (this.disposed || this.exitReceived) return;
    const now = Date.now();
    this.reconnectDeadline ??= now + this.reconnectGraceMilliseconds;
    const remaining = this.reconnectDeadline - now;
    if (remaining <= 0) {
      this.failConnection(detail);
      return;
    }
    this.setState("reconnecting");
    const delay = Math.min(250 * 2 ** Math.min(this.reconnectAttempt, 5), 5_000, remaining);
    this.reconnectAttempt += 1;
    this.reconnectTimer = globalThis.setTimeout(() => {
      this.reconnectTimer = undefined;
      this.openSocket(true);
    }, delay);
  }

  private failConnection(detail: string): void {
    if (this.exitReceived) return;
    this.exitReceived = true;
    this.setState("error");
    this.options.onError?.("connection_error", detail);
    this.options.onExit?.({ outcome: "disconnected" });
  }

  private closeDetail(event: CloseEvent): string {
    const phase = this.connectionReady ? "closed unexpectedly" : "failed";
    const reason = event.reason.trim();
    if (reason) {
      return `Terminal connection ${phase}: ${reason} (WebSocket close code ${event.code}).`;
    }
    if (event.code === 1006) {
      return this.connectionReady
        ? "Terminal connection closed unexpectedly (close code 1006). Check that Core is reachable and that the HTTP proxy allows WebSocket upgrades."
        : "Terminal connection failed before Core completed the WebSocket handshake (close code 1006). Check that Core is reachable and that the HTTP proxy allows WebSocket upgrades.";
    }
    return `Terminal connection ${phase} (WebSocket close code ${event.code}).`;
  }
}
