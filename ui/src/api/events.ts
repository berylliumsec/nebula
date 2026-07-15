import type { EventCursor, RunEvent } from "./types";
import { logCaughtDiagnostic } from "../diagnostics";

export type StreamState = "connecting" | "open" | "reconnecting" | "closed" | "unsupported";

export interface EventStreamOptions {
  apiBaseUrl: string;
  token?: string;
  cursor?: Partial<EventCursor>;
  onEvent: (event: RunEvent) => void;
  onStateChange?: (state: StreamState) => void;
  maxReconnectDelayMs?: number;
}

function websocketUrl(baseUrl: string, cursor: EventCursor): string {
  if (!cursor.runId) throw new Error("runId is required for the Core event stream");
  const endpoint = `${baseUrl.replace(/\/$/, "")}/runs/${encodeURIComponent(cursor.runId)}/events/ws`;
  const url = new URL(endpoint);
  url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
  url.searchParams.set("after", String(cursor.after));
  if (cursor.engagementId) url.searchParams.set("engagement_id", cursor.engagementId);
  return url.toString();
}

function parseEventFrame(value: unknown): RunEvent | undefined {
  if (!value || typeof value !== "object") return undefined;
  const frame = value as Record<string, unknown>;
  if (frame.kind === "heartbeat" || frame.kind === "replay_complete") return undefined;
  const candidate = frame.kind === "event" && frame.event && typeof frame.event === "object"
    ? (frame.event as Record<string, unknown>)
    : frame;
  if (!Number.isSafeInteger(candidate.sequence)) return undefined;

  // Accept the current snake_case Core ledger and the generated camelCase UI
  // contract during the alpha migration. OpenAPI generation will replace this
  // compatibility mapper once the public wire shape is frozen.
  const payload = candidate.payload && typeof candidate.payload === "object"
    ? (candidate.payload as Record<string, unknown>)
    : {};
  const eventType = String(candidate.kind ?? candidate.event_type ?? "system.notice") as RunEvent["kind"];
  return {
    sequence: Number(candidate.sequence),
    id: String(candidate.id ?? `sequence-${candidate.sequence}`),
    kind: eventType,
    engagementId: candidate.engagementId
      ? String(candidate.engagementId)
      : payload.engagement_id
        ? String(payload.engagement_id)
        : undefined,
    runId: candidate.runId ? String(candidate.runId) : candidate.run_id ? String(candidate.run_id) : undefined,
    actor: candidate.actor ? String(candidate.actor) : candidate.actor_id ? String(candidate.actor_id) : undefined,
    occurredAt: String(candidate.occurredAt ?? candidate.occurred_at ?? new Date().toISOString()),
    summary: String(
      candidate.summary ?? payload.summary ?? eventType.replace(/[._]/g, " "),
    ),
    payload,
  };
}

export function websocketAuthProtocol(token: string): string {
  const bytes = new TextEncoder().encode(token);
  let binary = "";
  bytes.forEach((byte) => (binary += String.fromCharCode(byte)));
  return `nebula.auth.${btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "")}`;
}

/**
 * Authenticated, replayable event transport. Authentication is sent as a
 * WebSocket subprotocol so the one-time desktop token is never written to a
 * URL or access log. The server must echo `nebula.events.v1` and resume after
 * the supplied monotonically increasing sequence number.
 */
export class NebulaEventStream {
  private readonly options: EventStreamOptions;
  private socket?: WebSocket;
  private reconnectTimer?: ReturnType<typeof setTimeout>;
  private reconnectAttempt = 0;
  private stopped = true;
  private cursor: EventCursor;

  constructor(options: EventStreamOptions) {
    this.options = options;
    this.cursor = {
      after: options.cursor?.after ?? 0,
      engagementId: options.cursor?.engagementId,
      runId: options.cursor?.runId,
    };
  }

  connect(): void {
    this.stopped = false;
    if (!this.cursor.runId) {
      this.stopped = true;
      this.options.onStateChange?.("unsupported");
      return;
    }
    this.open(false);
  }

  disconnect(): void {
    this.stopped = true;
    if (this.reconnectTimer) clearTimeout(this.reconnectTimer);
    this.socket?.close(1000, "Client disconnected");
    this.options.onStateChange?.("closed");
  }

  get lastSequence(): number {
    return this.cursor.after;
  }

  private open(reconnecting: boolean): void {
    if (typeof WebSocket === "undefined") {
      this.options.onStateChange?.("unsupported");
      return;
    }

    this.options.onStateChange?.(reconnecting ? "reconnecting" : "connecting");
    const protocols = ["nebula.events.v1"];
    if (this.options.token) protocols.push(websocketAuthProtocol(this.options.token));
    const socket = new WebSocket(websocketUrl(this.options.apiBaseUrl, this.cursor), protocols);
    this.socket = socket;

    socket.addEventListener("open", () => {
      this.reconnectAttempt = 0;
      this.options.onStateChange?.("open");
    });

    socket.addEventListener("message", (message) => {
      try {
        const event = parseEventFrame(JSON.parse(String(message.data)));
        if (!event || event.sequence <= this.cursor.after) return;
        this.cursor.after = event.sequence;
        this.options.onEvent(event);
      } catch (caughtError) {
        void logCaughtDiagnostic("interface.events.caught_failure_01", "A handled interface operation failed.", caughtError, "events");
        // Malformed or non-event frames are intentionally ignored. The API
        // emits a structured system.notice when operator attention is needed.
      }
    });

    socket.addEventListener("close", (event) => {
      if (this.stopped || event.code === 1000) {
        this.options.onStateChange?.("closed");
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
