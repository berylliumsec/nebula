import type { ChatStreamEvent, ChatToolResult, ChatTurn } from "../api/types";
import type { NativeActivitySource } from "../components/activityLedgerModel";

/**
 * What a Butler mode thread shows while Butler works: the same tool steps a
 * normal chat shows, from this device's stream or, after a reload or on
 * another device, from Core's pending turn.
 */
export interface ThreadActivity {
  turnId?: string;
  /** queued: waiting for Core capacity; running: admitted and working. */
  state: "queued" | "running" | "waiting" | "failed";
  /** Queue position and reason, or what a waiting turn waits for. */
  detail?: string;
  cards: NativeActivitySource[];
}

const LIVE_ID = "live";

function card(toolCallId: string, capability: string, status: string, extra: Partial<NativeActivitySource> = {}): NativeActivitySource {
  return { assistantId: LIVE_ID, toolCallId, capability, status, evidenceIds: [], artifacts: [], ...extra };
}

function queuedDetail(reason: string | undefined, position: number | undefined): string {
  return `${reason || "Waiting for Core capacity"}${position ? ` · position ${position}` : ""}`;
}

/**
 * Advance a thread's activity from one stream event; unrelated events keep it unchanged.
 * `turnId` is always the chat turn: a harness's tool frames name the harness
 * turn instead, so they never replace it.
 */
export function activityFromEvent(current: ThreadActivity | undefined, event: ChatStreamEvent): ThreadActivity | undefined {
  const base: ThreadActivity = current ?? { state: "running", cards: [] };
  switch (event.type) {
    case "queued":
      return { ...base, turnId: event.turnId, state: "queued", detail: queuedDetail(event.detail, event.queuePosition) };
    case "admitted":
      return { ...base, turnId: event.turnId, state: "running", detail: undefined };
    case "started":
      // A harness turn starts without queued/admitted frames; this is the one
      // frame that names its chat turn, which is what Stop cancels.
      return event.turnId && event.turnId !== base.turnId ? { ...base, turnId: event.turnId } : current;
    case "tool_started": {
      const started = card(event.toolCallId, event.capability, "running", { displayName: event.displayName, arguments: event.arguments });
      const known = base.cards.some((item) => item.toolCallId === event.toolCallId);
      return { ...base, state: "running", detail: undefined,
        cards: known ? base.cards.map((item) => item.toolCallId === event.toolCallId ? { ...item, ...started } : item) : [...base.cards, started] };
    }
    case "tool_completed": {
      const done = card(event.toolCallId, event.capability, event.status, { displayName: event.displayName, summary: event.summary });
      const known = base.cards.some((item) => item.toolCallId === event.toolCallId);
      return { ...base, cards: known
        ? base.cards.map((item) => item.toolCallId === event.toolCallId ? { ...item, ...done, arguments: item.arguments } : item)
        : [...base.cards, done] };
    }
    default:
      return current;
  }
}

/** A thread's activity as Core saved it: for a reload, another device, or a turn Core resumed. */
export function activityFromPendingTurn(turn: ChatTurn): ThreadActivity {
  const failed = turn.status === "failed" || turn.status === "interrupted";
  const saved = turn.toolCalls ?? [];
  const savedIds = new Set(saved.map((call) => call.toolCallId));
  const cards = [
    ...saved.map((call) => card(call.toolCallId, call.capability, call.status, { displayName: call.displayName, arguments: call.arguments, summary: call.summary })),
    ...turn.toolCallIds.filter((id) => !savedIds.has(id)).map((id) => card(id, "Command runtime", failed ? "failed" : turn.status === "waiting_approval" ? "waiting_approval" : "running")),
  ];
  if (turn.status === "queued") return { turnId: turn.id, state: "queued", detail: queuedDetail(turn.admissionReason, turn.queuePosition), cards };
  if (failed) return { turnId: turn.id, state: "failed", detail: turn.error, cards };
  if (turn.status === "waiting_approval" || turn.status === "waiting_callback") {
    return { turnId: turn.id, state: "waiting", detail: turn.waitSummary ?? (turn.status === "waiting_approval" ? "Waiting for your approval" : "Waiting for a callback"), cards };
  }
  return { turnId: turn.id, state: "running", cards };
}

/** The finished turn's steps, from the answer Core saved. */
export function activityFromSavedResults(messageId: string, results: readonly ChatToolResult[] | undefined): NativeActivitySource[] {
  return (results ?? []).map((result) => ({
    assistantId: messageId,
    toolCallId: result.toolCallId,
    capability: result.capability,
    displayName: result.displayName,
    status: result.status,
    summary: result.summary,
    evidenceIds: result.evidenceIds,
    resultArtifactId: result.resultArtifactId,
    artifacts: [],
    receipt: result.receipt,
  }));
}

/** The ledger status word for a live thread: the same states a chat's message uses. */
export function ledgerStatus(activity: ThreadActivity): string {
  if (activity.state === "queued") return "queued";
  if (activity.state === "failed") return "error";
  if (activity.state === "waiting") return "waiting_approval";
  return "streaming";
}
