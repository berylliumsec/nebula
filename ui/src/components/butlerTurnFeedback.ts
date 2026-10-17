import type { ChatStreamEvent, ChatTurn } from "../api/types";

export interface ButlerTurnFeedback {
  turnId?: string;
  startedAt: number;
  lastSignalAt?: number;
  label: string;
  recent: { label: string; at: number }[];
}

function shortLabel(value: string): string {
  const text = value.replace(/\s+/g, " ").trim();
  return text.length > 150 ? `${text.slice(0, 147).trimEnd()}…` : text;
}

function toolLabel(displayName?: string): string {
  return shortLabel(displayName || "a tool");
}

export function beginButlerFeedback(label: string, at = Date.now()): ButlerTurnFeedback {
  return { startedAt: at, label, recent: [{ label, at }] };
}

function advance(current: ButlerTurnFeedback | undefined, label: string, at: number, turnId?: string, startedAt?: number): ButlerTurnFeedback {
  const base = current && (!turnId || !current.turnId || current.turnId === turnId)
    ? current : beginButlerFeedback("Butler is working", startedAt ?? at);
  const changed = base.label !== label;
  if (!changed && at - (base.lastSignalAt ?? 0) < 5_000 && (!turnId || base.turnId === turnId)) return base;
  return {
    ...base,
    turnId: turnId ?? base.turnId,
    startedAt: startedAt ?? base.startedAt,
    lastSignalAt: at,
    label,
    recent: changed ? [...base.recent, { label, at }].slice(-4) : base.recent,
  };
}

/** Only named stream events become progress; arguments and reasoning stay private. */
export function feedbackFromButlerEvent(current: ButlerTurnFeedback | undefined, event: ChatStreamEvent, at = Date.now()): ButlerTurnFeedback | undefined {
  if (event.type === "connection") {
    return event.state === "reconnecting" ? advance(current, "Reconnecting to Butler’s response", at) : current;
  }
  if (event.type === "queued") return advance(current, shortLabel(event.detail || "Waiting for Core capacity"), at, event.turnId, Date.parse(event.queuedAt) || undefined);
  if (event.type === "admitted") return advance(current, "Butler is starting", at, event.turnId);
  if (event.type === "started") return advance(current, "Butler is working", at, event.turnId);
  if (event.type === "reasoning_delta") return advance(current, "Butler is thinking", at, event.turnId);
  if (event.type === "delta" || event.type === "message_delta") return advance(current, "Butler is writing a reply", at, event.turnId);
  if (event.type === "tool_started") return advance(current, `Using ${toolLabel(event.displayName)}`, at, event.turnId);
  if (event.type === "tool_completed") return advance(current,
    event.status === "failed" ? `${toolLabel(event.displayName)} failed; Butler is reviewing the result` : `Finished ${toolLabel(event.displayName)}; Butler is continuing`, at, event.turnId);
  if (event.type === "approval_required") return advance(current, "Approval needed to continue", at, event.turnId);
  if (event.type === "callback_required") return advance(current, shortLabel(event.summary || "Waiting for results"), at, event.turnId);
  if (event.type === "restarted") return advance(current, "Core restarted Butler’s response", at, event.turnId);
  if (event.type === "status") return advance(current, shortLabel(event.detail || "Butler is working"), at);
  if (event.type === "item_upsert" && event.itemStatus === "running" && event.title
    && ["tool", "command", "web_search", "browser", "subagent", "plan"].includes(event.itemKind ?? "")) {
    return advance(current, shortLabel(event.title), at);
  }
  if (event.type === "interaction") return advance(current, "Input needed to continue", at);
  return current;
}

/** A saved turn is the authority after reload or a missed stream frame. */
export function feedbackFromPendingTurn(current: ButlerTurnFeedback | undefined, turn: ChatTurn, at = Date.now()): ButlerTurnFeedback {
  const startedAt = Date.parse(turn.queuedAt ?? turn.startedAt ?? "") || at;
  const sameTurn = current?.turnId === turn.id;
  const lastTool = turn.toolCalls?.at(-1);
  let label: string | undefined;
  if (turn.status === "queued") label = turn.queuePosition ? `Waiting for Core capacity · position ${turn.queuePosition}` : "Waiting for Core capacity";
  else if (turn.status === "waiting_approval") label = "Approval needed to continue";
  else if (turn.status === "waiting_callback") label = shortLabel(turn.waitSummary || "Waiting for results");
  else if (turn.status === "failed") label = "Butler’s reply needs recovery";
  else if (turn.status === "interrupted") label = "Butler’s response was interrupted";
  else if (turn.status === "finalizing") label = "Saving Butler’s reply";
  else if (lastTool?.status === "running") label = `Using ${toolLabel(lastTool.displayName)}`;
  else if (lastTool?.status === "failed") label = `${toolLabel(lastTool.displayName)} failed; Butler is reviewing the result`;
  else if (!sameTurn || current?.label.startsWith("Waiting for Core capacity")
    || current?.label === "Approval needed to continue" || current?.label === "Input needed to continue"
    || turn.status === "routing" && current?.label === turn.waitSummary) label = "Butler is working";
  if (sameTurn && !label) return current;
  return advance(sameTurn ? current : undefined, label ?? "Butler is working", at, turn.id, startedAt);
}

export function formatButlerDuration(milliseconds: number): string {
  const seconds = Math.max(0, Math.floor(milliseconds / 1000));
  return seconds < 60 ? `${seconds}s` : `${Math.floor(seconds / 60)}m ${String(seconds % 60).padStart(2, "0")}s`;
}
