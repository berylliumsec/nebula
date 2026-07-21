import type { ChatCitation, ChatMessage, ChatUsage } from "../api/types";
import { finalAssistantContent } from "./harnessActivity";

export type ConversationMessageState = "complete" | "streaming" | "waiting_approval" | "error" | "cancelled";

export interface ReconciledConversationMessage extends ChatMessage {
  id: string;
  /** Stable UI identity retained while a temporary message receives its durable Core ID. */
  runtimeId?: string;
  createdAt: string;
  citations: ChatCitation[];
  usage?: ChatUsage;
  state: ConversationMessageState;
  durable: boolean;
  detail?: string;
  sequence?: number;
  harnessTurnId?: string;
}

interface CompletedAssistantMessage {
  temporaryAssistantId: string;
  durableAssistantId?: string;
  userId: string;
  content: string;
  citations: ChatCitation[];
  usage?: ChatUsage;
  harnessTurnId?: string;
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
    citations: completed.citations,
    usage: completed.usage,
    state: "complete",
    durable: Boolean(completed.durableAssistantId),
    harnessTurnId: completed.harnessTurnId ?? existing?.harnessTurnId,
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
