import type { QueueItem, useChatQueue } from "../pages/useChatQueue";
import { QueuedMessages } from "./QueuedMessages";

/**
 * A conversation's follow-up queue, docked above the composer. Once Core links
 * a sending item to its durable turn, the transcript owns it; a linked turn
 * comes back here only if Core moves it to needs_review or failed.
 */
export function ChatQueuePanel({ queue, onRefreshConversation, onSendNow, interrupts }: { queue: ReturnType<typeof useChatQueue>; onRefreshConversation: () => void; onSendNow?: (item: QueueItem) => void; interrupts?: boolean }) {
  return <QueuedMessages queue={queue} label="Core follow-up queue" sentWhen="after this response" onSendNow={onSendNow} interrupts={interrupts} onInspect={onRefreshConversation} />;
}
