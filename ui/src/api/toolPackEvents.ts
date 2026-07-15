import { websocketAuthProtocol, type StreamState } from "./events";
import { logCaughtDiagnostic } from "../diagnostics";

export type ToolPackOperation = "install_catalog" | "install_local" | "verify" | "update" | "disable";
export type ToolPackProgressPhase = "pending" | "pulling" | "verifying" | "ready" | "failed";
export type ToolPackResultStatus = "pending" | "pulling" | "verifying" | "ready" | "failed" | "disabled";

export interface ToolPackProgressEvent {
  sequence: number;
  occurredAt: string;
  operationId: string;
  operation: ToolPackOperation;
  phase: ToolPackProgressPhase;
  installationId?: string;
  packIdentity?: string;
  manifestDigest?: string;
  resultStatus?: ToolPackResultStatus;
}

export interface ToolPackEventStreamOptions {
  apiBaseUrl: string;
  token?: string;
  afterSequence?: number;
  onEvent: (event: ToolPackProgressEvent) => void;
  onStateChange?: (state: StreamState) => void;
  onReplayGap?: () => void;
  maxReconnectDelayMs?: number;
}

const operations = new Set<ToolPackOperation>(["install_catalog", "install_local", "verify", "update", "disable"]);
const phases = new Set<ToolPackProgressPhase>(["pending", "pulling", "verifying", "ready", "failed"]);
const resultStatuses = new Set<ToolPackResultStatus>(["pending", "pulling", "verifying", "ready", "failed", "disabled"]);
const digestPattern = /^[0-9a-f]{64}$/;

function boundedString(value: unknown, maxLength: number): string | undefined {
  if (typeof value !== "string" || !value || value.length > maxLength) return undefined;
  if (/[\u0000-\u001f\u007f]/.test(value)) return undefined;
  return value;
}

export function parseToolPackProgressFrame(value: unknown): ToolPackProgressEvent | undefined {
  if (!value || typeof value !== "object") return undefined;
  const frame = value as Record<string, unknown>;
  const candidate = frame.kind === "event" && frame.event && typeof frame.event === "object"
    ? frame.event as Record<string, unknown>
    : frame;
  if (!Number.isSafeInteger(candidate.sequence) || Number(candidate.sequence) < 1) return undefined;
  if (!operations.has(candidate.operation as ToolPackOperation) || !phases.has(candidate.phase as ToolPackProgressPhase)) return undefined;
  const occurredAt = boundedString(candidate.occurred_at ?? candidate.occurredAt, 100);
  const operationId = boundedString(candidate.operation_id ?? candidate.operationId, 200);
  if (!occurredAt || Number.isNaN(Date.parse(occurredAt)) || !operationId) return undefined;
  const manifestDigest = boundedString(candidate.manifest_digest ?? candidate.manifestDigest, 64);
  if (manifestDigest && !digestPattern.test(manifestDigest)) return undefined;
  const resultStatus = candidate.result_status ?? candidate.resultStatus;
  if (resultStatus !== undefined && resultStatus !== null && !resultStatuses.has(resultStatus as ToolPackResultStatus)) return undefined;
  return {
    sequence: Number(candidate.sequence),
    occurredAt,
    operationId,
    operation: candidate.operation as ToolPackOperation,
    phase: candidate.phase as ToolPackProgressPhase,
    installationId: boundedString(candidate.installation_id ?? candidate.installationId, 200),
    packIdentity: boundedString(candidate.pack_identity ?? candidate.packIdentity, 500),
    manifestDigest,
    resultStatus: resultStatus ? resultStatus as ToolPackResultStatus : undefined,
  };
}

function toolPackEventUrl(baseUrl: string, afterSequence: number): string {
  const url = new URL(`${baseUrl.replace(/\/$/, "")}/tool-packs/events/ws`);
  url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
  url.searchParams.set("after_sequence", String(afterSequence));
  return url.toString();
}

/** Authenticated replay stream for sanitized tool-pack lifecycle progress. */
export class ToolPackEventStream {
  private readonly options: ToolPackEventStreamOptions;
  private socket?: WebSocket;
  private reconnectTimer?: ReturnType<typeof setTimeout>;
  private reconnectAttempt = 0;
  private stopped = true;
  private cursor: number;

  constructor(options: ToolPackEventStreamOptions) {
    this.options = options;
    this.cursor = Math.max(0, Math.floor(options.afterSequence ?? 0));
  }

  connect(): void {
    this.stopped = false;
    this.open(false);
  }

  disconnect(): void {
    this.stopped = true;
    if (this.reconnectTimer) clearTimeout(this.reconnectTimer);
    this.socket?.close(1000, "Client disconnected");
    this.socket = undefined;
    this.options.onStateChange?.("closed");
  }

  get lastSequence(): number {
    return this.cursor;
  }

  private open(reconnecting: boolean): void {
    if (typeof WebSocket === "undefined") {
      this.options.onStateChange?.("unsupported");
      return;
    }
    this.options.onStateChange?.(reconnecting ? "reconnecting" : "connecting");
    const protocols = ["nebula.tool-packs.v1"];
    if (this.options.token) protocols.push(websocketAuthProtocol(this.options.token));
    const socket = new WebSocket(toolPackEventUrl(this.options.apiBaseUrl, this.cursor), protocols);
    this.socket = socket;
    socket.addEventListener("open", () => {
      this.reconnectAttempt = 0;
      this.options.onStateChange?.("open");
    });
    socket.addEventListener("message", (message) => {
      try {
        const frame = JSON.parse(String(message.data)) as Record<string, unknown>;
        if (frame.kind === "replay_gap") {
          this.options.onReplayGap?.();
          return;
        }
        const event = parseToolPackProgressFrame(frame);
        if (!event || event.sequence <= this.cursor) return;
        this.cursor = event.sequence;
        this.options.onEvent(event);
      } catch (caughtError) {
        void logCaughtDiagnostic("interface.tool_pack_events.caught_failure_01", "A handled interface operation failed.", caughtError, "tool_pack_events");
        // Ignore malformed frames. Only the fixed sanitized event contract is surfaced.
      }
    });
    socket.addEventListener("close", (event) => {
      if (this.stopped || event.code === 1000) {
        this.options.onStateChange?.("closed");
        return;
      }
      if (event.code === 4401 || event.code === 4501) {
        this.stopped = true;
        this.options.onStateChange?.("unsupported");
        return;
      }
      this.scheduleReconnect();
    });
  }

  private scheduleReconnect(): void {
    const ceiling = this.options.maxReconnectDelayMs ?? 15_000;
    const delay = Math.min(500 * 2 ** this.reconnectAttempt, ceiling);
    this.reconnectAttempt += 1;
    this.options.onStateChange?.("reconnecting");
    this.reconnectTimer = setTimeout(() => {
      if (!this.stopped) this.open(true);
    }, delay);
  }
}
