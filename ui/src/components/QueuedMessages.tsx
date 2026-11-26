import { useEffect, useState } from "react";
import { ArrowDown, ArrowUp, ChevronDown, ChevronRight, Pause, Pencil, Play, Send, Trash2, X } from "lucide-react";
import type { QueueItem, useChatQueue } from "../pages/useChatQueue";
import styles from "./QueuedMessages.module.css";

/** Messages Core has not sent yet, or could not send. A sent one belongs to the transcript. */
export function waitingQueueItems(items: QueueItem[] = []): QueueItem[] {
  return items.filter((item) => !["complete", "cancelled"].includes(item.status)
    && !(item.status === "sending" && item.turn_id));
}

const EDITABLE = new Set(["queued", "needs_review"]);

/** The heading line: a count and what happens next, or what needs the operator. */
export function queueHeading(items: QueueItem[], paused: boolean | undefined, sentWhen: string): string {
  const attention = items.filter((item) => item.status === "needs_review" || item.status === "failed").length;
  if (paused) return `Queue paused${attention ? ` · ${attention} needs attention` : ""}`;
  if (attention) return `${attention} queued ${attention === 1 ? "message needs" : "messages need"} attention`;
  if (items.some((item) => item.status === "claiming" || item.status === "sending")) return `${items.length} queued · sending`;
  return `${items.length} queued · sent in order ${sentWhen}`;
}

export interface QueuedMessagesProps {
  queue: ReturnType<typeof useChatQueue>;
  /** The region's accessible name. */
  label: string;
  /** Ends "sent in order …", for example "when Butler is free". */
  sentWhen: string;
  /** An error from the composer's own enqueue, shown with the queue's. */
  error?: string;
  /**
   * Send a waiting message now. The caller asks Core to put it first and
   * stops the running response, if any; without it there is no Send now.
   */
  onSendNow?: (item: QueueItem) => void;
  /** Whether Send now stops a running response (it only resumes otherwise). */
  interrupts?: boolean;
  /** Open the transcript for a message Core linked to a response. */
  onInspect?: () => void;
  /** Placement from the surface that docks the queue. */
  className?: string;
}

/**
 * Messages typed while the agent worked, docked above the composer. Core owns
 * the queue and sends it in order even after every tab closes; this only reads
 * and edits it. Each message can be sent now, edited, reordered or removed.
 */
export function QueuedMessages({ queue: controller, label, sentWhen, error, onSendNow, interrupts, onInspect, className }: QueuedMessagesProps) {
  const { queue, busy, mutate, reload } = controller;
  const [editing, setEditing] = useState<{ id: string; text: string }>();
  const items = waitingQueueItems(queue?.items);
  const problem = error || controller.error;
  const attention = Boolean(problem) || items.some((item) => item.status === "needs_review" || item.status === "failed");
  const [open, setOpen] = useState(true);
  // Something that needs the operator is never left collapsed.
  useEffect(() => { if (attention) setOpen(true); }, [attention]);
  if (!items.length && !problem) return null;
  const editable = items.filter((item) => EDITABLE.has(item.status));
  const heading = queueHeading(items, queue?.paused, sentWhen);
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
      messages: [{ ...first, role: "user", content: text, ...(blocks.length || first?.content_blocks ? { content_blocks: [{ type: "text", text }, ...blocks] } : {}) }] } });
    if (saved) setEditing(undefined);
  };
  const sendNowTitle = interrupts ? "Stop the current response and send this now" : "Send this next";
  return <section className={`${styles.queue} queued-messages${className ? ` ${className}` : ""}`} aria-label={label} data-state={attention ? "attention" : queue?.paused ? "paused" : "queued"}>
    {items.length > 0 && <div className={styles.heading}>
      <button type="button" className={styles.toggle} aria-expanded={open} onClick={() => setOpen((value) => !value)}>
        {open ? <ChevronDown size={14} aria-hidden="true" /> : <ChevronRight size={14} aria-hidden="true" />}
        <span role="status">{heading}</span>
      </button>
      <button className={styles.icon} type="button" aria-label={queue?.paused ? "Resume queue" : "Pause queue"} title={queue?.paused ? "Resume queue" : "Pause queue"} disabled={busy} onClick={() => void mutate({ action: queue?.paused ? "resume" : "pause" })}>
        {queue?.paused ? <Play size={14} aria-hidden="true" /> : <Pause size={14} aria-hidden="true" />}
      </button>
      <button className={styles.icon} type="button" aria-label="Clear queue" title="Clear queue" disabled={busy} onClick={() => void mutate({ action: "clear" })}><Trash2 size={14} aria-hidden="true" /></button>
    </div>}
    {problem && <div className={styles.error} role="alert"><span>{problem}</span><button className={styles.text} type="button" onClick={() => void reload()}>Reload queue</button></div>}
    {open && <ol className={styles.list}>
      {items.map((item, index) => {
        const text = item.request?.messages[0]?.content ?? "";
        const canEdit = EDITABLE.has(item.status);
        const position = editable.indexOf(item);
        return <li key={item.id} data-status={item.status}>
          <span className={styles.number} aria-hidden="true">{index + 1}</span>
          {editing?.id === item.id
            ? <form className={styles.edit} onSubmit={(event) => { event.preventDefault(); void saveEdit(item, editing.text); }}>
              <textarea aria-label="Edit queued text" data-selection-actions-disabled="true" value={editing.text} maxLength={20000} rows={2} autoFocus
                onChange={(event) => setEditing({ id: item.id, text: event.target.value })}
                onKeyDown={(event) => {
                  if (event.key === "Escape") { event.preventDefault(); setEditing(undefined); }
                  else if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) { event.preventDefault(); if (editing.text.trim()) void saveEdit(item, editing.text); }
                }} />
              <div><button className={styles.text} type="submit" aria-label="Save queued edit" disabled={busy || !editing.text.trim()}>Save</button><button className={styles.text} type="button" onClick={() => setEditing(undefined)}>Cancel</button></div>
            </form>
            : <>
              <span className={styles.body}>
                <span className={styles.message} title={text}>{text}</span>
                {item.status === "needs_review" && <small data-tone="attention">Needs review{item.detail ? ` · ${item.detail}` : ""}</small>}
                {item.status === "failed" && <small data-tone="attention">Failed{item.detail ? ` · ${item.detail}` : ""}</small>}
                {(item.status === "claiming" || item.status === "sending") && <small>Sending…</small>}
                {Boolean(item.request?.context_attachments?.length) && <small>With selected context</small>}
              </span>
              <span className={styles.actions}>
                {item.status === "needs_review" && <button className={styles.text} type="button" aria-label="Retry as new message" title="Retry as a new message" disabled={busy} onClick={() => void mutate({ action: "retry", item_id: item.id })}>Retry</button>}
                {item.turn_id && onInspect && <button className={styles.text} type="button" aria-label="Inspect conversation" onClick={onInspect}>Inspect</button>}
                {item.status === "queued" && onSendNow && <button className={styles.sendNow} type="button" aria-label={`Send queued message ${index + 1} now`} title={sendNowTitle} disabled={busy} onClick={() => onSendNow(item)}><Send size={13} aria-hidden="true" /><span>Send now</span></button>}
                {canEdit && <>
                  <button className={styles.icon} type="button" aria-label={`Edit queued message ${index + 1}`} title="Edit" disabled={busy} onClick={() => setEditing({ id: item.id, text })}><Pencil size={14} aria-hidden="true" /></button>
                  {editable.length > 1 && <>
                    <button className={styles.icon} type="button" aria-label={`Move queued message ${index + 1} up`} title="Move up" disabled={busy || position <= 0} onClick={() => move(item, -1)}><ArrowUp size={14} aria-hidden="true" /></button>
                    <button className={styles.icon} type="button" aria-label={`Move queued message ${index + 1} down`} title="Move down" disabled={busy || position >= editable.length - 1} onClick={() => move(item, 1)}><ArrowDown size={14} aria-hidden="true" /></button>
                  </>}
                  <button className={styles.icon} type="button" aria-label={`Remove queued message ${index + 1}`} title="Remove" disabled={busy} onClick={() => void mutate({ action: "remove", item_id: item.id })}><X size={14} aria-hidden="true" /></button>
                </>}
              </span>
            </>}
        </li>;
      })}
    </ol>}
  </section>;
}
