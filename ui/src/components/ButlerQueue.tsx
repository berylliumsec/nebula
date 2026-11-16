import { useState } from "react";
import { ArrowDown, ArrowUp, Pencil, X } from "lucide-react";
import type { QueueItem, useChatQueue } from "../pages/useChatQueue";
import styles from "./NebulaControlAssistant.module.css";

/** Messages Core has not sent yet, or could not send. A sent one belongs to the transcript. */
export function waitingQueueItems(items: QueueItem[] = []): QueueItem[] {
  return items.filter((item) => !["complete", "cancelled"].includes(item.status)
    && !(item.status === "sending" && item.turn_id));
}

const EDITABLE = new Set(["queued", "needs_review"]);

/**
 * Butler's follow-up queue: messages sent while Butler was working. Core sends
 * them in order when Butler is free, even after this tab closes; the panel only
 * reads and edits Core's queue.
 */
export function ButlerQueue({ queue: controller, error }: { queue: ReturnType<typeof useChatQueue>; error?: string }) {
  const { queue, busy, mutate, reload } = controller;
  const [editing, setEditing] = useState<{ id: string; text: string }>();
  const items = waitingQueueItems(queue?.items);
  const problem = error || controller.error;
  if (!items.length && !problem) return null;
  const attention = items.filter((item) => item.status === "needs_review" || item.status === "failed").length;
  const editable = items.filter((item) => EDITABLE.has(item.status));
  const heading = queue?.paused
    ? `Queue paused${attention ? ` · ${attention} needs attention` : ""}`
    : attention ? `${attention} queued ${attention === 1 ? "message needs" : "messages need"} attention`
      : `${items.length} queued · sent in order when Butler is free`;
  const move = (item: QueueItem, direction: number) => {
    const order = editable.map((row) => row.id);
    const index = order.indexOf(item.id);
    const target = index + direction;
    if (index < 0 || target < 0 || target >= order.length) return;
    [order[index], order[target]] = [order[target], order[index]];
    void mutate({ action: "reorder", order });
  };
  const saveEdit = async (item: QueueItem, text: string) => {
    const first = item.request?.messages[0];
    const blocks = (first?.content_blocks ?? []).filter((block) => block.type !== "text");
    const saved = await mutate({ action: "edit", item_id: item.id, request: { ...item.request,
      messages: [{ ...first, role: "user", content: text, ...(blocks.length ? { content_blocks: [{ type: "text", text }, ...blocks] } : {}) }] } });
    if (saved) setEditing(undefined);
  };
  return <section className={styles.queue} aria-label="Butler queue" data-state={attention || problem ? "attention" : queue?.paused ? "paused" : "queued"}>
    {items.length > 0 && <div className={styles.queueHeading}>
      <strong role="status">{heading}</strong>
      <button className="button quiet" type="button" disabled={busy} onClick={() => void mutate({ action: queue?.paused ? "resume" : "pause" })}>{queue?.paused ? "Resume" : "Pause"}</button>
      <button className="button quiet" type="button" disabled={busy} onClick={() => void mutate({ action: "clear" })}>Clear</button>
    </div>}
    {problem && <div className={styles.queueError} role="alert"><span>{problem}</span><button className="button quiet" type="button" onClick={() => void reload()}>Reload queue</button></div>}
    <ol>
      {items.map((item, index) => {
        const text = item.request?.messages[0]?.content ?? "";
        const canEdit = EDITABLE.has(item.status);
        const position = editable.indexOf(item);
        return <li key={item.id}>
          {editing?.id === item.id
            ? <form className={styles.queueEdit} onSubmit={(event) => { event.preventDefault(); void saveEdit(item, editing.text); }}>
              <textarea aria-label={`Edit queued message ${index + 1}`} data-selection-actions-disabled="true" value={editing.text} maxLength={4000} rows={2}
                onChange={(event) => setEditing({ id: item.id, text: event.target.value })} />
              <div><button className="button quiet" type="submit" disabled={busy || !editing.text.trim()}>Save</button><button className="button quiet" type="button" onClick={() => setEditing(undefined)}>Cancel</button></div>
            </form>
            : <>
              <span className={styles.queueText}>
                <span>{text}</span>
                {item.status === "needs_review" && <small data-tone="attention">Needs review{item.detail ? ` · ${item.detail}` : ""}</small>}
                {item.status === "failed" && <small data-tone="attention">Failed{item.detail ? ` · ${item.detail}` : ""}</small>}
                {(item.status === "claiming" || item.status === "sending") && <small>Sending…</small>}
                {Boolean(item.request?.context_attachments?.length) && <small>With selected context</small>}
              </span>
              {canEdit && <span className={styles.queueActions}>
                {item.status === "needs_review" && <button className="button quiet" type="button" disabled={busy} onClick={() => void mutate({ action: "retry", item_id: item.id })}>Retry</button>}
                <button className="icon-button subtle" type="button" aria-label={`Edit queued message ${index + 1}`} title="Edit" disabled={busy} onClick={() => setEditing({ id: item.id, text })}><Pencil size={14} /></button>
                {editable.length > 1 && <>
                  <button className="icon-button subtle" type="button" aria-label={`Move queued message ${index + 1} up`} title="Move up" disabled={busy || position <= 0} onClick={() => move(item, -1)}><ArrowUp size={14} /></button>
                  <button className="icon-button subtle" type="button" aria-label={`Move queued message ${index + 1} down`} title="Move down" disabled={busy || position >= editable.length - 1} onClick={() => move(item, 1)}><ArrowDown size={14} /></button>
                </>}
                <button className="icon-button subtle" type="button" aria-label={`Remove queued message ${index + 1}`} title="Remove" disabled={busy} onClick={() => void mutate({ action: "remove", item_id: item.id })}><X size={14} /></button>
              </span>}
            </>}
        </li>;
      })}
    </ol>
  </section>;
}
