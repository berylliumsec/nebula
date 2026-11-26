import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { ExternalLink, X } from "lucide-react";
import type { ApiClient } from "../api/client";
import type { PersistedChatMessage } from "../api/types";
import { startVisiblePoll } from "../api/visiblePoll";
import { logCaughtDiagnostic } from "../diagnostics";
import styles from "./ButlerChatPopup.module.css";
import { ButlerMarkdown } from "../components/ButlerMarkdown";

const PAGE = 60;

/**
 * A chat's transcript over Butler mode: scroll it, then click outside or press
 * Esc to return. It reads the newest page and older pages on request, and
 * re-reads only while the chat is running and the page is visible.
 */
export function ButlerChatPopup({ api, sessionId, title, live, onClose, onOpenFull }: {
  api: ApiClient;
  sessionId: string;
  title: string;
  /** The chat is running: new messages may arrive. */
  live: boolean;
  onClose: () => void;
  /** Absent where there is no workbench to open (the iPhone app). */
  onOpenFull?: () => void;
}) {
  const [messages, setMessages] = useState<PersistedChatMessage[]>();
  const [error, setError] = useState("");
  const [older, setOlder] = useState<{ loading: boolean; done: boolean; error?: string }>({ loading: false, done: false });
  const scroller = useRef<HTMLDivElement>(null);
  const closeButton = useRef<HTMLButtonElement>(null);
  const followLatest = useRef(true);
  const returnFocus = useRef<Element | null>(null);

  // Core pages history only with replaced messages included; an edit's
  // replaced messages are kept for reference but are not the transcript.
  const visible = (items: PersistedChatMessage[]) => items.filter((item) => !item.replacedAt && (item.role === "user" || item.role === "assistant") && item.content.trim());

  const readLatest = useCallback(async (signal?: AbortSignal) => {
    const page = await api.listChatMessages(sessionId, signal, { includeReplaced: true, limit: PAGE });
    setMessages((current) => {
      if (!current) return page;
      // Keep older pages already read; replace the newest page with Core's copy.
      const firstNew = page[0]?.sequence ?? Number.POSITIVE_INFINITY;
      return [...current.filter((item) => item.sequence < firstNew), ...page];
    });
    if (page.length < PAGE) setOlder((current) => ({ ...current, done: true }));
    setError("");
  }, [api, sessionId]);

  useEffect(() => {
    returnFocus.current = document.activeElement;
    closeButton.current?.focus();
    return () => { if (returnFocus.current instanceof HTMLElement) returnFocus.current.focus(); };
  }, []);

  useEffect(() => {
    setMessages(undefined);
    setOlder({ loading: false, done: false });
    followLatest.current = true;
    const controller = new AbortController();
    let first = true;
    startVisiblePoll({
      intervalMs: 5000,
      maxIntervalMs: 20_000,
      signal: controller.signal,
      read: async (signal) => {
        if (!first && !live) return "idle";
        first = false;
        try {
          await readLatest(signal);
          return live ? "active" : "idle";
        } catch (caught) {
          if (signal.aborted) return "stop";
          void logCaughtDiagnostic("interface.butler_mode.popup_read_failed", "Butler mode could not read a chat transcript.", caught, "butler_mode");
          setError(caught instanceof Error ? caught.message : "Core could not read this chat.");
          return "idle";
        }
      },
    });
    return () => controller.abort();
  }, [readLatest, live]);

  useLayoutEffect(() => {
    const element = scroller.current;
    if (element && followLatest.current) element.scrollTop = element.scrollHeight;
  }, [messages]);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      event.preventDefault();
      event.stopPropagation();
      onClose();
    };
    window.addEventListener("keydown", onKey, true);
    return () => window.removeEventListener("keydown", onKey, true);
  }, [onClose]);

  const loadOlder = async () => {
    const first = messages?.[0];
    if (!first || older.loading) return;
    setOlder((current) => ({ ...current, loading: true, error: undefined }));
    const element = scroller.current;
    const before = element ? element.scrollHeight - element.scrollTop : 0;
    try {
      const page = await api.listChatMessages(sessionId, undefined, { includeReplaced: true, limit: PAGE, beforeMessageId: first.id });
      followLatest.current = false;
      setMessages((current) => [...page, ...(current ?? [])]);
      setOlder({ loading: false, done: page.length < PAGE });
      // Keep the operator's place while older messages appear above.
      requestAnimationFrame(() => { if (element) element.scrollTop = element.scrollHeight - before; });
    } catch (caught) {
      void logCaughtDiagnostic("interface.butler_mode.popup_older_failed", "Butler mode could not read older messages.", caught, "butler_mode");
      setOlder({ loading: false, done: false, error: caught instanceof Error ? caught.message : "Core could not read older messages." });
    }
  };

  const shown = messages ? visible(messages) : [];
  return <div className={styles.layer}>
    <button type="button" className={styles.scrim} aria-label="Close chat" tabIndex={-1} onClick={onClose} />
    <section className={styles.popup} role="dialog" aria-modal="true" aria-label={`Chat: ${title}`}>
      <header className={styles.head}>
        <h2>{title}</h2>
        {live && <span className={styles.live}><i aria-hidden="true" /> Running</span>}
        {onOpenFull && <button type="button" className={styles.link} onClick={onOpenFull}>Open in workbench <ExternalLink size={13} aria-hidden="true" /></button>}
        <button ref={closeButton} type="button" className={styles.close} aria-label="Close chat" title="Close (Esc)" onClick={onClose}><X size={16} aria-hidden="true" /></button>
      </header>
      <div ref={scroller} className={styles.transcript} role="log" aria-label={`${title} transcript`} tabIndex={0} onScroll={(event) => { const element = event.currentTarget; followLatest.current = element.scrollHeight - element.scrollTop - element.clientHeight < 48; }}>
        {messages && !older.done && <button type="button" className={styles.link} disabled={older.loading} onClick={() => void loadOlder()}>{older.loading ? "Loading earlier messages…" : "Show earlier messages"}</button>}
        {older.error && <p className={styles.error} role="alert">{older.error}</p>}
        {!messages && !error && <p className={styles.muted} role="status">Reading the chat…</p>}
        {error && <p className={styles.error} role="alert">{error}</p>}
        {messages && shown.length === 0 && <p className={styles.muted}>No messages yet.</p>}
        {shown.map((message) => <div key={message.id} className={message.role === "user" ? styles.question : styles.answer}>
          {message.role === "user" ? message.content : <ButlerMarkdown content={message.content} />}
        </div>)}
      </div>
    </section>
  </div>;
}
