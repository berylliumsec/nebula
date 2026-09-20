import type { ChatCitation, ChatMessage, ChatUsage, ToolSuggestionSummary } from "../api/types";
import { finalAssistantContent } from "./harnessActivity";

export type ConversationMessageState = "complete" | "streaming" | "waiting_approval" | "error" | "cancelled";

export interface ReconciledConversationMessage extends ChatMessage {
  id: string;
  /** Stable UI identity retained while a temporary message receives its durable Core ID. */
  runtimeId?: string;
  createdAt: string;
  citations: ChatCitation[];
  usage?: ChatUsage;
  /** How long the turn ran, and how much of that waited on an approval. */
  elapsedMs?: number;
  approvalWaitMs?: number;
  state: ConversationMessageState;
  durable: boolean;
  recoveredHarnessTurn?: boolean;
  detail?: string;
  sequence?: number;
  harnessTurnId?: string;
  toolSuggestions?: ToolSuggestionSummary;
}

interface CompletedAssistantMessage {
  temporaryAssistantId: string;
  durableAssistantId?: string;
  userId: string;
  content: string;
  reasoning?: string;
  citations: ChatCitation[];
  usage?: ChatUsage;
  elapsedMs?: number;
  approvalWaitMs?: number;
  harnessTurnId?: string;
  toolSuggestions?: ToolSuggestionSummary;
  createdAt: string;
}

export function reconcileCompletedAssistantMessage(
  messages: ReconciledConversationMessage[],
  completed: CompletedAssistantMessage,
): ReconciledConversationMessage[] {
  const durableId = completed.durableAssistantId ?? completed.temporaryAssistantId;
  const durable = messages.find((message) => message.id === durableId);
  const temporary = messages.find((message) => message.id === completed.temporaryAssistantId);
  const existing = durable ?? temporary;
  const finalized: ReconciledConversationMessage = {
    ...(existing ?? {
      id: durableId,
      role: "assistant",
      content: "",
      createdAt: completed.createdAt,
      citations: [],
      state: "streaming",
      durable: false,
    }),
    id: durableId,
    runtimeId: existing?.runtimeId
      ?? (existing?.id === completed.temporaryAssistantId && durableId !== completed.temporaryAssistantId
        ? completed.temporaryAssistantId
        : undefined),
    role: "assistant",
    content: finalAssistantContent(existing?.content ?? "", completed.content),
    reasoning: completed.reasoning || existing?.reasoning,
    citations: completed.citations,
    usage: completed.usage,
    elapsedMs: completed.elapsedMs ?? existing?.elapsedMs,
    approvalWaitMs: completed.approvalWaitMs ?? existing?.approvalWaitMs,
    state: "complete",
    durable: Boolean(completed.durableAssistantId),
    harnessTurnId: completed.harnessTurnId ?? existing?.harnessTurnId,
    toolSuggestions: completed.toolSuggestions ?? existing?.toolSuggestions,
  };

  const reconciled = messages
    .filter((message) => (
      message.id !== durableId && message.id !== completed.temporaryAssistantId
    ))
    .map((message) => message.id === completed.userId ? { ...message, durable: true } : message);
  const insertionIndex = existing
    ? messages.findIndex((message) => message.id === existing.id)
    : reconciled.length;
  reconciled.splice(Math.min(insertionIndex, reconciled.length), 0, finalized);
  return reconciled;
}

/** Restore event-owned turns that ended before Core saved a final chat message. */
export async function recoverHarnessHistory(
  messages: ReconciledConversationMessage[],
  getTurn: (id: string) => Promise<{id: string; status: string; error?: string}>,
): Promise<ReconciledConversationMessage[]> {
  const represented = new Set(messages.filter(message => message.role === "assistant").map(message => message.harnessTurnId));
  const result: ReconciledConversationMessage[] = [];
  for (const message of messages) {
    result.push(message);
    const turnId = message.harnessTurnId;
    if (message.role !== "user" || !turnId || represented.has(turnId)) continue;
    const turn = await getTurn(turnId);
    if (!["failed", "cancelled", "interrupted"].includes(turn.status)) continue;
    represented.add(turnId);
    result.push({
      id: `assistant-harness-recovery-${turnId}`,
      role: "assistant", content: "", createdAt: message.createdAt, citations: [],
      state: turn.status === "cancelled" ? "cancelled" : "error",
      durable: false, recoveredHarnessTurn: true, harnessTurnId: turnId,
      detail: turn.status === "cancelled" ? undefined : turn.error ?? "The harness turn was interrupted before its outcome was known.",
    });
  }
  return result;
}
