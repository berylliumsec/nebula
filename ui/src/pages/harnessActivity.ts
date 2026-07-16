import type {
  HarnessActivityEvent,
  HarnessActivityItemKind,
  HarnessDetailedUsage,
} from "../api/types";

export type ReasoningSummaryState = "pending" | "available" | "not_provided";

export interface HarnessActivityItem {
  assistantId: string;
  key: string;
  turnId?: string;
  sessionId?: string;
  itemId?: string;
  parentItemId?: string;
  kind?: HarnessActivityItemKind;
  type: string;
  vendor?: HarnessActivityEvent["vendor"];
  status?: string;
  title: string;
  summary?: string;
  sequence: number;
  streams: Record<string, string>;
  payload: Record<string, unknown>;
  artifactIds: string[];
  usage?: HarnessDetailedUsage;
  occurredAt?: string;
}

const CHAT_CHROME_EVENT_TYPES = new Set([
  "message_delta",
  "started",
  "status",
  "turn_status",
  "completed",
]);

export function isTimelineActivity(event: HarnessActivityEvent): boolean {
  if (CHAT_CHROME_EVENT_TYPES.has(event.type)) return false;
  const vendorItemType = typeof event.payload.type === "string" ? event.payload.type : undefined;
  return vendorItemType !== "userMessage" && vendorItemType !== "agentMessage";
}

export function finalAssistantContent(streamed: string, durable: string): string {
  return durable.trim() ? durable : streamed;
}

const LEGACY_CODEX_REASONING_SUMMARY =
  "Codex is reasoning; hidden trace content is not retained.";

function summaryState(value: unknown): ReasoningSummaryState | undefined {
  return value === "pending" || value === "available" || value === "not_provided"
    ? value
    : undefined;
}

function isCodexReasoningItem(
  item: Pick<HarnessActivityItem, "kind" | "vendor" | "title" | "summary" | "payload" | "streams">,
): boolean {
  if (item.kind !== "reasoning") return false;
  return item.vendor === "codex_app_server"
    && (item.title === "Reasoning"
      || item.title === "Reasoning summary"
      || "reasoning_summary_state" in item.payload
      || "reasoning_summary" in item.streams)
    || item.summary === LEGACY_CODEX_REASONING_SUMMARY;
}

export function reduceHarnessActivity(
  items: HarnessActivityItem[],
  event: HarnessActivityEvent,
  assistantId: string,
): HarnessActivityItem[] {
  const sequence = event.sequence ?? 0;
  const key = event.itemId
    ? `${event.harnessTurnId ?? "turn"}:${event.itemId}`
    : `${event.harnessTurnId ?? "turn"}:${event.type}:${event.id ?? sequence}`;
  const existingIndex = items.findIndex((item) => item.key === key);
  const existing = existingIndex >= 0 ? items[existingIndex] : undefined;
  if (existing && sequence > 0 && existing.sequence >= sequence) return items;

  const stream = event.stream ?? (event.type === "message_delta" ? "message" : "output");
  const streams = { ...(existing?.streams ?? {}) };
  const payload = { ...(existing?.payload ?? {}), ...event.payload };
  const authoritativeReasoningSummary = typeof event.payload.reasoning_summary_text === "string"
    ? event.payload.reasoning_summary_text.slice(0, 65_536)
    : undefined;
  if (authoritativeReasoningSummary !== undefined) {
    streams.reasoning_summary = authoritativeReasoningSummary;
  } else if (event.delta) {
    streams[stream] = `${streams[stream] ?? ""}${event.delta}`.slice(0, 65_536);
  }

  const next: HarnessActivityItem = {
    assistantId,
    key,
    turnId: event.harnessTurnId ?? existing?.turnId,
    sessionId: event.harnessSessionId ?? existing?.sessionId,
    itemId: event.itemId ?? existing?.itemId,
    parentItemId: event.parentItemId ?? existing?.parentItemId,
    kind: event.itemKind ?? existing?.kind,
    type: event.type,
    vendor: event.vendor ?? existing?.vendor,
    status: event.itemStatus ?? existing?.status,
    title: event.title ?? existing?.title ?? event.type.replaceAll("_", " "),
    summary: event.summary ?? event.message ?? existing?.summary,
    sequence: Math.max(sequence, existing?.sequence ?? 0),
    streams,
    payload,
    artifactIds: [...new Set([...(existing?.artifactIds ?? []), ...event.artifactIds])],
    usage: event.detailedUsage ?? existing?.usage,
    occurredAt: event.occurredAt ?? existing?.occurredAt,
  };

  if (isCodexReasoningItem(next)) {
    next.title = "Reasoning";
    next.summary = undefined;
    const text = streams.reasoning_summary;
    const requestedState = summaryState(payload.reasoning_summary_state);
    if (text) {
      payload.reasoning_summary_state = "available";
      payload.reasoning_summary_text = text;
      if (typeof payload.reasoning_summary_source !== "string") {
        payload.reasoning_summary_source = "stream";
      }
    } else if (event.summary === LEGACY_CODEX_REASONING_SUMMARY) {
      payload.reasoning_summary_state = "not_provided";
    } else if (requestedState) {
      payload.reasoning_summary_state = requestedState;
    }
  }

  const updated = existingIndex >= 0
    ? items.map((item, index) => index === existingIndex ? next : item)
    : [...items, next];
  return updated.sort((left, right) => left.sequence - right.sequence);
}

export function reasoningSummaryState(item: HarnessActivityItem): ReasoningSummaryState | undefined {
  if (!isCodexReasoningItem(item)) return undefined;
  return summaryState(item.payload.reasoning_summary_state)
    ?? (item.streams.reasoning_summary ? "available" : undefined);
}

export function reasoningSummaryText(item: HarnessActivityItem): string | undefined {
  if (!isCodexReasoningItem(item)) return undefined;
  const snapshot = item.payload.reasoning_summary_text;
  if (typeof snapshot === "string" && snapshot) return snapshot;
  return item.streams.reasoning_summary || undefined;
}

export function shouldShowActivityKind(item: HarnessActivityItem): boolean {
  if (!item.kind) return false;
  const normalize = (value: string) => value.toLowerCase().replaceAll(/[_\s-]/g, "");
  return normalize(item.kind) !== normalize(item.title);
}
