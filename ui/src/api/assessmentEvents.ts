import { websocketAuthProtocol, type StreamState } from "./events";
import { logCaughtDiagnostic } from "../diagnostics";

export interface AssessmentEvent {
  id: string;
  sequence: number;
  eventType: string;
  occurredAt: string;
  payload: Record<string, unknown>;
}

interface AssessmentEventStreamOptions {
  apiBaseUrl: string;
  assessmentId: string;
  token?: string;
  after?: number;
  onEvent: (event: AssessmentEvent) => void;
  onStateChange?: (state: StreamState) => void;
}

function endpoint(options: AssessmentEventStreamOptions, after: number): string {
  const url = new URL(
    `${options.apiBaseUrl.replace(/\/$/, "")}/browser-assessments/${encodeURIComponent(options.assessmentId)}/events/ws`,
  );
  url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
  url.searchParams.set("after", String(after));
  return url.toString();
}

function parseFrame(value: unknown): AssessmentEvent | undefined {
  if (!value || typeof value !== "object") return undefined;
  const frame = value as Record<string, unknown>;
  if (frame.kind !== "event" || !frame.event || typeof frame.event !== "object") return undefined;
  const event = frame.event as Record<string, unknown>;
  if (!Number.isSafeInteger(event.sequence)) return undefined;
  return {
    id: String(event.id ?? `assessment-event-${event.sequence}`),
    sequence: Number(event.sequence),
    eventType: String(event.event_type ?? "browser_assessment.updated"),
    occurredAt: String(event.occurred_at ?? new Date().toISOString()),
    payload: event.payload && typeof event.payload === "object"
      ? event.payload as Record<string, unknown>
      : {},
  };
}

/** Cursored assessment stream. Reconnect resumes after the last acknowledged event. */
export class AssessmentEventStream {
  private readonly options: AssessmentEventStreamOptions;
  private socket?: WebSocket;
  private timer?: ReturnType<typeof setTimeout>;
  private stopped = true;
  private attempt = 0;
  private cursor: number;

  constructor(options: AssessmentEventStreamOptions) {
    this.options = options;
    this.cursor = options.after ?? 0;
  }

  connect(): void {
    this.stopped = false;
    this.open(false);
  }

  disconnect(): void {
    this.stopped = true;
    if (this.timer) clearTimeout(this.timer);
    this.socket?.close(1000, "Assessment workspace closed");
    this.options.onStateChange?.("closed");
  }

  private open(reconnecting: boolean): void {
    if (typeof WebSocket === "undefined") {
      this.options.onStateChange?.("unsupported");
      return;
    }
    this.options.onStateChange?.(reconnecting ? "reconnecting" : "connecting");
    const protocols = ["nebula.events.v1"];
    if (this.options.token) protocols.push(websocketAuthProtocol(this.options.token));
    const socket = new WebSocket(endpoint(this.options, this.cursor), protocols);
    this.socket = socket;
    socket.addEventListener("open", () => {
      this.attempt = 0;
      this.options.onStateChange?.("open");
    });
    socket.addEventListener("message", (message) => {
      try {
        const event = parseFrame(JSON.parse(String(message.data)));
        if (!event || event.sequence <= this.cursor) return;
        this.cursor = event.sequence;
        this.options.onEvent(event);
      } catch (caught) {
        void logCaughtDiagnostic(
          "interface.security_browser.assessment_event_invalid",
          "A Security Browser assessment event could not be read.",
          caught,
          "security_browser",
        );
      }
    });
    socket.addEventListener("close", (event) => {
      if (this.stopped || event.code === 1000) {
        this.options.onStateChange?.("closed");
        return;
      }
      this.options.onStateChange?.("reconnecting");
      const delay = Math.min(500 * 2 ** this.attempt, 15_000);
      this.attempt += 1;
      this.timer = setTimeout(() => this.open(true), delay);
    });
  }
}
