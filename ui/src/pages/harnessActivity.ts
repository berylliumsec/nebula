import type {
  HarnessActivityEvent,
  HarnessActivityItemKind,
  HarnessDetailedUsage,
  HarnessGoalSnapshot,
  HarnessPlanEntry,
  HarnessSessionActivity,
} from "../api/types";

export type ReasoningSummaryState = "pending" | "available" | "not_provided";

export function isSameHarnessSessionActivity(
  current: HarnessSessionActivity | undefined,
  next: HarnessSessionActivity,
): boolean {
  return current?.sessionId === next.sessionId
    && current.sessionStatus === next.sessionStatus
    && current.busy === next.busy
    && current.live === next.live
    && current.turnId === next.turnId
    && current.turnStatus === next.turnStatus
    && current.turnOrigin === next.turnOrigin
    && current.startedAt === next.startedAt
    && current.lastActivityAt === next.lastActivityAt
    && current.detail === next.detail
    && current.mode === next.mode
    && JSON.stringify(current.plan) === JSON.stringify(next.plan)
    && JSON.stringify(current.goal) === JSON.stringify(next.goal);
}

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
  mode?: string;
  plan?: HarnessPlanEntry[];
  goal?: HarnessGoalSnapshot;
}

export interface HarnessActivityDisplayGroup {
  key: string;
  type: "item" | "narrative";
  items: HarnessActivityItem[];
}

export function groupHarnessActivityItems(
  items: HarnessActivityItem[],
): HarnessActivityDisplayGroup[] {
  const narrativeByParent = new Map<string, HarnessActivityItem[]>();
  for (const item of items) {
    if (item.kind !== "reasoning") continue;
    const parent = item.parentItemId ?? "";
    narrativeByParent.set(parent, [...(narrativeByParent.get(parent) ?? []), item]);
  }

  const emittedNarrativeParents = new Set<string>();
  const groups: HarnessActivityDisplayGroup[] = [];
  for (const item of items) {
    if (item.kind !== "reasoning") {
      groups.push({ key: item.key, type: "item", items: [item] });
      continue;
    }
    const parent = item.parentItemId ?? "";
    if (emittedNarrativeParents.has(parent)) continue;
    emittedNarrativeParents.add(parent);
    groups.push({
      key: `narrative:${parent || "root"}:${item.key}`,
      type: "narrative",
      items: narrativeByParent.get(parent) ?? [item],
    });
  }
  return groups;
}

const CHAT_CHROME_EVENT_TYPES = new Set([
  "message_delta",
  "started",
  "status",
  "turn_status",
  "completed",
]);

const OPERATOR_ACTION_EVENT_TYPES = new Set([
  "approval",
  "approval_required",
  "interaction",
  "checkpoint",
  "error",
  "interrupted",
]);

const OPERATOR_WORK_ITEM_KINDS = new Set([
  "reasoning",
  "command",
  "file_change",
  "tool",
  "web_search",
  "browser",
  "image",
  "skill",
  "subagent",
]);

const TURN_DETAIL_ITEM_KINDS = new Set([
  "plan",
  "mode",
  "goal",
  "hook",
  "review",
  "compaction",
]);

const LEGACY_OPERATOR_WORK_ITEM_TYPES = new Set([
  "commandExecution",
  "fileChange",
  "mcpToolCall",
  "dynamicToolCall",
  "webSearch",
  "imageGeneration",
  "imageView",
  "skill",
  "collabAgentToolCall",
  "subAgentActivity",
]);

const ACTION_REQUIRED_STATUSES = new Set([
  "failed",
  "interrupted",
  "waiting_approval",
  "waiting_input",
]);

const OPERATOR_NOTICE_SEVERITIES = new Set(["warning", "error", "critical"]);

export type HarnessActivityTier = "transcript" | "details" | "diagnostics";

export function harnessActivityTier(event: HarnessActivityEvent): HarnessActivityTier {
  if (CHAT_CHROME_EVENT_TYPES.has(event.type)) return "diagnostics";
  const vendorItemType = typeof event.payload.type === "string" ? event.payload.type : undefined;
  if (vendorItemType === "userMessage" || vendorItemType === "agentMessage") return "diagnostics";
  if (OPERATOR_ACTION_EVENT_TYPES.has(event.type)) return "transcript";
  if (event.itemStatus && ACTION_REQUIRED_STATUSES.has(event.itemStatus)) return "transcript";
  if (event.itemKind && OPERATOR_WORK_ITEM_KINDS.has(event.itemKind)) return "transcript";
  if (vendorItemType && LEGACY_OPERATOR_WORK_ITEM_TYPES.has(vendorItemType)) return "transcript";
  const severity = typeof event.payload.severity === "string"
    ? event.payload.severity.toLowerCase()
    : undefined;
  if (event.type === "notice" && severity && OPERATOR_NOTICE_SEVERITIES.has(severity)) {
    return "transcript";
  }
  if (event.type === "usage" || (event.itemKind && TURN_DETAIL_ITEM_KINDS.has(event.itemKind))) {
    return "details";
  }
  return "diagnostics";
}

export function isTimelineActivity(event: HarnessActivityEvent): boolean {
  return harnessActivityTier(event) !== "diagnostics";
}

export function harnessActivityItemTier(item: HarnessActivityItem): HarnessActivityTier {
  if (OPERATOR_ACTION_EVENT_TYPES.has(item.type)) return "transcript";
  if (item.status && ACTION_REQUIRED_STATUSES.has(item.status)) return "transcript";
  if (item.kind && OPERATOR_WORK_ITEM_KINDS.has(item.kind)) return "transcript";
  if (item.kind && TURN_DETAIL_ITEM_KINDS.has(item.kind)) return "details";
  if (item.type === "usage" || item.usage) return "details";
  const severity = typeof item.payload.severity === "string"
    ? item.payload.severity.toLowerCase()
    : undefined;
  return item.type === "notice" && severity && OPERATOR_NOTICE_SEVERITIES.has(severity)
    ? "transcript"
    : "diagnostics";
}

export function summarizeHarnessActivity(items: HarnessActivityItem[]): string {
  const transcript = items.filter((item) => harnessActivityItemTier(item) === "transcript");
  const actions = transcript.filter((item) => item.kind && item.kind !== "reasoning").length;
  const commentary = transcript.some((item) => item.kind === "reasoning");
  const attention = transcript.some((item) =>
    OPERATOR_ACTION_EVENT_TYPES.has(item.type)
    || Boolean(item.status && ACTION_REQUIRED_STATUSES.has(item.status))
  );
  const durationMs = items.reduce((maximum, item) => Math.max(maximum, item.usage?.durationMs ?? 0), 0);
  const parts: string[] = [];
  if (attention) parts.push("Attention needed");
  if (actions) parts.push(`${actions} action${actions === 1 ? "" : "s"}`);
  if (commentary) parts.push("commentary");
  if (durationMs) parts.push(`${(durationMs / 1000).toFixed(durationMs < 10_000 ? 1 : 0)}s`);
  return parts.join(" · ") || "Turn details";
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
    mode: event.mode ?? existing?.mode,
    plan: event.plan ?? existing?.plan,
    goal: event.goal ?? existing?.goal,
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

export function shouldShowActivityItem(item: HarnessActivityItem): boolean {
  if (item.kind !== "reasoning") return true;
  if (item.payload.reasoning_summary_malformed === true) return true;
  const completed = ["completed", "complete", "success"].includes(item.status ?? "");
  return !(completed && reasoningSummaryState(item) === "not_provided");
}

export function shouldShowActivityKind(item: HarnessActivityItem): boolean {
  if (!item.kind) return false;
  const normalize = (value: string) => value.toLowerCase().replaceAll(/[_\s-]/g, "");
  return normalize(item.kind) !== normalize(item.title);
}

export function harnessCostLabel(item: HarnessActivityItem): string | undefined {
  const cost = item.usage?.costUsd;
  if (!cost) return undefined;
  const precision = cost < 0.0001 ? 6 : 4;
  const scale = 10 ** precision;
  const displayed = (Math.round(cost * scale) / scale).toFixed(precision);
  return item.vendor === "codex_app_server"
    ? `≈$${displayed} API equivalent`
    : `$${displayed}`;
}
