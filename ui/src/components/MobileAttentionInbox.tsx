import { useEffect, useRef, useState } from "react";
import { Clipboard, RefreshCw, X } from "lucide-react";
import type { ChatAttentionItem } from "../api/types";
import { sameJson, startVisiblePoll, type VisiblePoll } from "../api/visiblePoll";
import { logCaughtDiagnostic } from "../diagnostics";
import { useWorkspace } from "../state/WorkspaceContext";
import { ModalSurface } from "./DialogSystem";

/** Core owns the pending actions. This list only locates their conversations. */
export function MobileAttentionInbox({ onOpenChat }: { onOpenChat: (item: ChatAttentionItem) => void }) {
  const { api, workspaceState } = useWorkspace();
  const [items, setItems] = useState<ChatAttentionItem[]>([]);
  const [error, setError] = useState<string>();
  const [open, setOpen] = useState(false);
  const [loading, setLoading] = useState(true);
  const pollRef = useRef<VisiblePoll | undefined>(undefined);
  const connected = Boolean(api && (workspaceState === "ready" || workspaceState === "degraded"));
  const unavailable = !connected || Boolean(error);

  useEffect(() => {
    if (!api || !connected) {
      setItems([]);
      setLoading(false);
      return;
    }
    const controller = new AbortController();
    pollRef.current = startVisiblePoll({
      signal: controller.signal,
      intervalMs: 30_000,
      read: async signal => {
        try {
          const next = await api.listChatAttention(signal);
          if (signal.aborted) return "stop";
          setItems(current => sameJson(current, next) ? current : next);
          setError(undefined);
          setLoading(false);
          return "idle";
        } catch (caught) {
          if (signal.aborted) return "stop";
          void logCaughtDiagnostic("interface.mobile_attention.read_failed", "Pending chat actions could not be loaded.", caught, "mobile-attention");
          setItems([]);
          setError("Could not load pending actions. Your conversations remain available.");
          setLoading(false);
          return "active";
        }
      },
    });
    return () => {
      controller.abort();
      pollRef.current = undefined;
    };
  }, [api, connected]);

  useEffect(() => {
    const refresh = () => pollRef.current?.poke();
    window.addEventListener("nebula:chat-attention-changed", refresh);
    return () => window.removeEventListener("nebula:chat-attention-changed", refresh);
  }, []);

  return <>
    <button className="icon-button subtle mobile-attention-trigger" type="button" aria-label={unavailable ? "Needs you, unavailable" : items.length ? `Needs you, ${items.length} pending` : "Needs you"} title={unavailable ? "Pending actions unavailable" : "Needs you"} aria-haspopup="dialog" aria-expanded={open} onClick={() => { setOpen(true); pollRef.current?.poke(); }}>
      <Clipboard size={20} aria-hidden="true" />{(items.length > 0 || unavailable) && <span className="mobile-attention-count" aria-hidden="true">{unavailable ? "!" : items.length > 9 ? "9+" : items.length}</span>}
    </button>
    {open && <ModalSurface as="section" className="mobile-attention-inbox" labelledBy="mobile-attention-title" onClose={() => setOpen(false)}>
      <header><div><h2 id="mobile-attention-title">Needs you</h2><small>Pending agent approvals and questions across projects</small></div><button className="icon-button subtle" type="button" aria-label="Close Needs you" title="Close" onClick={() => setOpen(false)}><X size={20} aria-hidden="true" /></button></header>
      <div className="mobile-attention-content">
        {!connected ? <div className="empty-state mini" role="status"><strong>Core is disconnected</strong><p>Reconnect to check pending actions. Your saved chats remain on Core.</p></div>
          : loading ? <p role="status">Loading pending actions…</p>
          : error ? <div role="alert"><p>{error}</p><button className="button secondary" type="button" onClick={() => { setLoading(true); pollRef.current?.poke(); }}><RefreshCw size={16} aria-hidden="true" /> Retry</button></div>
          : items.length ? <ul>{items.map(item => <li key={`${item.kind}:${item.actionId}`}><button type="button" onClick={() => { setOpen(false); onOpenChat(item); }}><strong>{item.kind === "approval" ? "Approval needed" : "Agent has a question"}</strong><span>{item.sessionTitle}</span><small>{item.engagementName}</small></button></li>)}</ul>
          : <div className="empty-state mini" role="status"><strong>All caught up</strong><p>No agent approvals or questions are waiting.</p></div>}
      </div>
    </ModalSurface>}
  </>;
}
