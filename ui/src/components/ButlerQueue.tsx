import type { QueueItem, useChatQueue } from "../pages/useChatQueue";
import { QueuedMessages } from "./QueuedMessages";

export { waitingQueueItems } from "./QueuedMessages";

/**
 * Butler's follow-up queue: messages sent while Butler was working. Core sends
 * them in order when Butler is free, even after this tab closes.
 */
export function ButlerQueue({ queue, error, onSendNow, interrupts }: { queue: ReturnType<typeof useChatQueue>; error?: string; onSendNow?: (item: QueueItem) => void; interrupts?: boolean }) {
  return <QueuedMessages queue={queue} label="Butler queue" sentWhen="when Butler is free" error={error} onSendNow={onSendNow} interrupts={interrupts} />;
}
