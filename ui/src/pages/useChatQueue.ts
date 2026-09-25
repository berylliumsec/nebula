import { logCaughtDiagnostic } from "../diagnostics";
import { useCallback, useEffect, useRef, useState } from "react";
import { chatRequestBody, type ApiClient } from "../api/client";
import { sameJson, startVisiblePoll, type PollOutcome, type VisiblePoll } from "../api/visiblePoll";
import type { ChatCompletionRequest } from "../api/types";

export interface QueueItem {
  id: string; key: string; status: string; detail?: string; turn_id?: string;
  /** Core drops the request of a settled (complete or cancelled) follow-up. */
  request?: { messages: {role: string; content: string; content_blocks?: {type: string; [key: string]: unknown}[]}[]; context_attachments?: unknown[]; [key: string]: unknown };
}
export interface ChatQueue { revision: number; paused: boolean; items: QueueItem[] }
/** How often a queue with undispatched or sending work is read back. */
const QUEUE_POLL_MS = 2_000;
/** An idle, unchanged queue backs off to this. */
const QUEUE_IDLE_POLL_MS = 10_000;
/** Core dispatches (or is dispatching) this queue without any operator action. */
function queueIsMoving(queue: ChatQueue | undefined): boolean {
  return Boolean(queue?.items.some(item => item.status === "claiming" || item.status === "sending"
    || (!queue.paused && item.status === "queued")));
}
function validateQueue(value: ChatQueue): ChatQueue {
  if (!value || !Array.isArray(value.items) || typeof value.revision !== "number" || typeof value.paused !== "boolean") throw new Error("Core returned an unsupported queue response");
  return value;
}
export function useChatQueue(api: ApiClient | undefined, sessionId: string) {
  const [queue, setQueue] = useState<ChatQueue>();
  const current = useRef<ChatQueue | undefined>(undefined);
  const [error, setError] = useState<string>();
  const [busy, setBusy] = useState(false);
  const locked = useRef(false);
  const pending = useRef<{content: string; key: string} | undefined>(undefined);
  const path = `chat/sessions/${encodeURIComponent(sessionId)}/queue`;
  const activePath = useRef(path); activePath.current = path;
  const reload = useCallback(async () => {
    if (!api || !sessionId) return;
    const saved = validateQueue(await api.request<ChatQueue>(path));
    if (activePath.current === path) {
      if (!sameJson(current.current, saved)) {current.current = saved; setQueue(saved);}
      // New or edited work moves soon; read it at the base cadence again.
      pollRef.current?.reset();
    }
    return saved;
  }, [api, sessionId, path]);
  const pollRef = useRef<VisiblePoll | undefined>(undefined);
  useEffect(() => {
    let disposed = false; const controller = new AbortController();
    current.current = undefined; setQueue(undefined); setError(undefined); pending.current = undefined;
    if (!api || !sessionId) return;
    // Core answers 304 while the queue is unchanged, and an unchanged queue
    // keeps its identity, so an idle poll re-renders nothing.
    let etag: string | undefined;
    pollRef.current = startVisiblePoll({
      intervalMs: QUEUE_POLL_MS,
      maxIntervalMs: QUEUE_IDLE_POLL_MS,
      signal: controller.signal,
      read: async (signal): Promise<PollOutcome> => {
        try {
          const answer = await api.requestIfChanged<ChatQueue>(path, etag, {signal});
          if (disposed) return "stop";
          if (!answer) return queueIsMoving(current.current) ? "active" : "idle";
          const saved = validateQueue(answer.value);
          // A mutation owns the queue while it runs; take its result instead.
          if (locked.current) return "active";
          etag = answer.etag;
          const changed = !sameJson(current.current, saved);
          if (changed) {current.current = saved; setQueue(saved);}
          return changed || queueIsMoving(saved) ? "active" : "idle";
        } catch (e) {
          void logCaughtDiagnostic("interface.assistant_chat.operation_failed", "An assistant chat operation failed.", e, "assistant_chat");
          if (!disposed) setError(e instanceof Error ? e.message : "Queue could not be read. Reload to retry.");
          return "active";
        }
      },
    });
    return () => {disposed = true; controller.abort(); pollRef.current = undefined;};
  }, [api, sessionId, path]);
  const mutate = async (body: Record<string, unknown>) => {
    if (!api || !sessionId || locked.current) return false;
    locked.current = true; setBusy(true); setError(undefined);
    try {
      const before = current.current ?? await reload();
      if (!before) return false;
      const saved = validateQueue(await api.request<ChatQueue>(path, {method: "POST", body: JSON.stringify({...body, expected_revision: before.revision})}));
      if (activePath.current === path) {current.current = saved; setQueue(saved);} await reload(); return activePath.current === path;
    } catch (e) { void logCaughtDiagnostic("interface.assistant_chat.operation_failed", "An assistant chat operation failed.", e, "assistant_chat");
      setError(`${e instanceof Error ? e.message : "Queue change failed"}. Reload the queue and reapply your edit; your draft is retained.`);
      return false;
    } finally {locked.current = false; setBusy(false);}
  };
  const enqueue = async (request: ChatCompletionRequest, options: {paused?: boolean; first?: boolean; key?: string; uncertain?: boolean} = {}) => {
    const wire = chatRequestBody(request, true);
    const content = JSON.stringify(wire);
    if (pending.current?.content !== content) pending.current = {content, key: `queue-${Date.now()}-${Math.random().toString(36).slice(2)}`};
    const accepted = await mutate({action: "enqueue", request: wire, idempotency_key: options.key ?? pending.current.key, paused: options.paused, first: options.first, imported_uncertain: options.uncertain});
    if (accepted) pending.current = undefined;
    return accepted;
  };
  return {queue, error, busy, mutate, enqueue, reload: async () => {try {await reload(); setError(undefined);} catch (e) { void logCaughtDiagnostic("interface.assistant_chat.operation_failed", "An assistant chat operation failed.", e, "assistant_chat");setError(e instanceof Error ? e.message : "Queue reload failed");}}};
}
