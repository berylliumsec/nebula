import { logCaughtDiagnostic } from "../diagnostics";
import { useCallback, useEffect, useRef, useState } from "react";
import { chatRequestBody, type ApiClient } from "../api/client";
import type { ChatCompletionRequest } from "../api/types";

export interface QueueItem {
  id: string; key: string; status: string; detail?: string; turn_id?: string;
  request: { messages: {role: string; content: string; content_blocks?: {type: string; [key: string]: unknown}[]}[]; context_attachments?: unknown[]; [key: string]: unknown };
}
export interface ChatQueue { revision: number; paused: boolean; items: QueueItem[] }
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
    if (activePath.current === path) {current.current = saved; setQueue(saved);} return saved;
  }, [api, sessionId, path]);
  useEffect(() => {
    let disposed = false; const controller = new AbortController();
    current.current = undefined; setQueue(undefined); setError(undefined); pending.current = undefined;
    if (!api || !sessionId) return;
    const poll = async () => {
      try { const saved = validateQueue(await api.request<ChatQueue>(path, {signal: controller.signal})); if (!disposed && !locked.current) {current.current = saved; setQueue(saved);} }
      catch (e) { void logCaughtDiagnostic("interface.assistant_chat.operation_failed", "An assistant chat operation failed.", e, "assistant_chat"); if (!disposed) setError(e instanceof Error ? e.message : "Queue could not be read. Reload to retry."); }
    };
    void poll(); const timer = setInterval(() => void poll(), 2000);
    return () => {disposed = true; controller.abort(); clearInterval(timer);};
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
