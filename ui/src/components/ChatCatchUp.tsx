import { useEffect, useRef, useState } from "react";
import { RefreshCw, X } from "lucide-react";
import type { ApiClient } from "../api/client";
import { logCaughtDiagnostic } from "../diagnostics";
interface CatchUpEntry {id: string; message_id?: string; turn_id?: string; text: string; kind: string}
interface CatchUpRecord {initialized: boolean; revision: number; through_at: string; items: CatchUpEntry[]; pending: CatchUpEntry[]; truncated: boolean}
function readDeviceId() {
  const key = "nebula.assistant.read-device";
  const existing = localStorage.getItem(key); if (existing) return existing;
  const value = `reader-${Date.now()}-${Math.random().toString(36).slice(2)}`; localStorage.setItem(key, value); return value;
}
export function ChatCatchUp({api, sessionId, ready, atLatest, actionRevision, onMessage, onPending, onTurn}: {api: ApiClient; sessionId: string; ready: boolean; atLatest: boolean; actionRevision?: string; onMessage: (id: string) => void; onPending: () => void; onTurn: (id: string) => void}) {
  const [card, setCard] = useState<CatchUpRecord>(); const cardRef = useRef<CatchUpRecord | undefined>(undefined);
  const [pending, setPending] = useState<CatchUpEntry[]>([]); const [error, setError] = useState<string>(); const [refresh, setRefresh] = useState(0);
  const needsCatchupRef = useRef(true);
  const atLatestRef = useRef(atLatest); atLatestRef.current = atLatest;
  const acknowledgeRef = useRef<((record: CatchUpRecord) => Promise<void>) | undefined>(undefined);
  useEffect(() => {
    if (!ready) return;
    const controller = new AbortController(); let busy = false; let needsCatchup = needsCatchupRef.current;
    const path = `chat/sessions/${encodeURIComponent(sessionId)}`;
    const report = (e: unknown) => {if (!controller.signal.aborted) {void logCaughtDiagnostic("interface.assistant_chat.cursor_failed", "The device read cursor could not be synchronized.", e, "assistant_chat"); setError("Read state could not sync. Pending actions remain available.");}};
    let device: string;
    try {device = readDeviceId();} catch(e) {void logCaughtDiagnostic("interface.assistant_chat.cursor_operation_failed", "The read cursor operation failed.", e, "assistant_chat"); report(e); return;}
    const acknowledge = async (record: CatchUpRecord) => {
      await api.request(`${path}/read-cursor`, {method: "PUT", signal: controller.signal, body: JSON.stringify({device_id: device, expected_revision: record.revision, through_at: record.through_at})});
    };
    acknowledgeRef.current = async record => {
      if (busy) return; busy = true;
      try {await acknowledge(record); cardRef.current = undefined; setCard(undefined); setError(undefined);} catch(e) {void logCaughtDiagnostic("interface.assistant_chat.cursor_operation_failed", "The read cursor operation failed.", e, "assistant_chat"); report(e);} finally {busy = false;}
    };
    const check = async () => {
      if (document.hidden || busy) return;
      busy = true;
      try {
        const record = await api.request<CatchUpRecord>(`${path}/catch-up?device_id=${encodeURIComponent(device)}`, {signal: controller.signal});
        if (controller.signal.aborted) return;
        if (!record || !Array.isArray(record.items) || !Array.isArray(record.pending) || typeof record.revision !== "number") throw new Error("Unsupported read-cursor response");
        setPending(record.pending);
        if (cardRef.current || needsCatchup && record.initialized && record.items.length > 0) {
          cardRef.current = record; setCard(record);
        } else if (atLatestRef.current && (!record.initialized || record.items.length > 0)) await acknowledge(record);
        needsCatchup = false; needsCatchupRef.current = false;
      } catch(e) {void logCaughtDiagnostic("interface.assistant_chat.cursor_operation_failed", "The read cursor operation failed.", e, "assistant_chat"); report(e);} finally {busy = false;}
    };
    const visibility = () => {if (document.hidden) {needsCatchup = true; needsCatchupRef.current = true;} else void check();};
    void check(); const timer = setInterval(() => void check(), 8000); document.addEventListener("visibilitychange", visibility);
    return () => {controller.abort(); clearInterval(timer); document.removeEventListener("visibilitychange", visibility); acknowledgeRef.current = undefined;};
  }, [api, sessionId, ready, refresh, actionRevision]);
  return <>{error && <div className="chat-recovery-notice chat-catch-up-recovery" role="status"><p>{error}</p><button className="icon-button subtle" type="button" aria-label="Reload catch-up" title="Reload catch-up" onClick={() => {setError(undefined); setRefresh(value => value + 1);}}><RefreshCw size={16} aria-hidden="true" /></button></div>}{card && card.items.length > 0 && <section className="chat-catch-up" aria-label="Catch up on this conversation"><header><strong>Since you last read this conversation</strong><button className="icon-button subtle" type="button" aria-label="Dismiss catch-up" title="Dismiss catch-up" onClick={() => void acknowledgeRef.current?.(card)}><X size={16} aria-hidden="true" /></button></header>{card.truncated && <p>Showing up to 50 recent updates. Open the transcript for the full history.</p>}<ul>{card.items.map(item => <li key={item.id}><button className="button quiet" type="button" onClick={() => { item.kind === "failure" && item.turn_id ? onTurn(item.turn_id) : item.message_id ? onMessage(item.message_id) : item.turn_id ? onTurn(item.turn_id) : onPending(); void acknowledgeRef.current?.(card); }}><strong>{item.kind}</strong><span>{item.text}</span></button></li>)}</ul></section>}{pending.length > 0 && <div className="chat-pending-actions" role="status"><strong>{pending.length === 1 ? "1 action needs review" : `${pending.length} actions need review`}</strong><button className="button secondary" type="button" aria-label="Review pending actions" onClick={onPending}>Review</button></div>}</>;
}
