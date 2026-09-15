import { useEffect, useRef, useState } from "react";
import { LoaderCircle, Send, Square, X } from "lucide-react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import type { ApiClient } from "../api/client";
import type { ChatCompletionRequest, ChatMessage, ChatSessionSummary } from "../api/types";
import type { SelectionActionDraft } from "./selection";
import { ModalSurface } from "./DialogSystem";
import { sha256Hex } from "../sha256";
import { logCaughtDiagnostic } from "../diagnostics";
import styles from "./AskNebulaPopup.module.css";

export type AssistantSnapshot = Omit<ChatCompletionRequest, "messages" | "contextAttachments">;

export function AskNebulaPopup({ api, snapshot, context, onClose }: {
  api?: ApiClient; snapshot?: AssistantSnapshot; context: SelectionActionDraft; onClose(): void;
}) {
  const [question, setQuestion] = useState("");
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [answer, setAnswer] = useState("");
  const [busy, setBusy] = useState(false);
  const [ready, setReady] = useState(false);
  const [error, setError] = useState<string>();
  const [attempt, setAttempt] = useState(0);
  const branch = useRef<ChatSessionSummary | undefined>(undefined);
  const controller = useRef<AbortController | undefined>(undefined);
  const needsAction = useRef(false);
  const turnId = useRef<string | undefined>(undefined);
  const output = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!api || !snapshot) return;
    let closed = false;
    let created: ChatSessionSummary | undefined;
    const discard = (id: string) => {
      void api.discardTemporaryChat(id).catch(reason => {
        void logCaughtDiagnostic("interface.ask_nebula.cleanup_failed", "The popup closed; Core will expire its temporary conversation.", reason, "chat");
      });
    };
    setReady(false);
    setError(undefined);
    void api.createTemporaryChat(snapshot).then(session => {
      created = session;
      if (closed) { discard(session.id); return; }
      branch.current = session;
      setReady(true);
    }).catch(reason => {
      if (!closed) setError(reason instanceof Error ? reason.message : "Could not open a temporary conversation. Try again.");
    });
    const unload = () => { if (created) discard(created.id); };
    window.addEventListener("pagehide", unload);
    return () => {
      closed = true;
      controller.current?.abort();
      window.removeEventListener("pagehide", unload);
      if (created) discard(created.id);
      branch.current = undefined;
    };
  }, [api, snapshot, attempt]);

  useEffect(() => { output.current?.scrollTo?.({ top: output.current.scrollHeight }); }, [messages, answer]);

  const stop = async () => {
    if (!api || !turnId.current) return;
    try { await api.cancelChatTurn(turnId.current); controller.current?.abort(); setBusy(false); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "Could not stop the response. Try Stop again."); }
  };
  const ask = async () => {
    const session = branch.current;
    if (!api || !session || !question.trim() || busy) return;
    const prompt = question.trim();
    const abort = new AbortController();
    controller.current = abort;
    turnId.current = undefined;
    needsAction.current = false;
    setBusy(true); setError(undefined); setAnswer("");
    try {
      const result = await api.streamChat({
        ...snapshot, backend: session.backend, providerId: session.providerId,
        harnessProfileId: session.harnessProfileId, harnessSessionId: undefined,
        sessionId: session.id, model: session.model, toolsEnabled: false,
        messages: [{ role: "user", content: prompt }],
        contextAttachments: [{ sourceKind: context.source.kind, sourceId: context.source.id ?? context.source.kind,
          sourceLabel: context.source.label, text: context.text, sha256: sha256Hex(context.text), truncated: context.truncated }],
      }, event => {
        if (abort.signal.aborted) return;
        if (event.type === "started") turnId.current = event.turnId;
        if (event.type === "delta" || event.type === "message_delta") setAnswer(current => current + event.delta);
        if (event.type === "approval_required" || event.type === "approval" || event.type === "interaction") {
          needsAction.current = true;
          setError("This question needs an action. Stop this response and ask a question that can be answered from the context.");
        }
      }, abort.signal);
      if (!abort.signal.aborted && result) {
        needsAction.current = false;
        setMessages(current => [...current, { role: "user", content: prompt }, result.message]);
        setQuestion(""); setAnswer("");
      }
    } catch (reason) {
      if (!abort.signal.aborted) setError(reason instanceof Error ? reason.message : "The response failed. You can try your question again.");
    } finally { setBusy(needsAction.current && !abort.signal.aborted); }
  };

  return <ModalSurface className={styles.popup} labelledBy="ask-nebula-title" onClose={onClose}>
    <div data-selection-actions-disabled>
      <div className={styles.header}><div><h2 id="ask-nebula-title">Ask Nebula</h2><small>Temporary · discarded when closed</small></div>
        <button className="icon-button subtle" type="button" aria-label="Close Ask Nebula" title="Close and discard" onClick={onClose}><X size={18} aria-hidden="true" /></button></div>
      <details className={styles.context}><summary>{context.source.label}{context.truncated ? " · shortened" : ""}</summary><pre>{context.text}</pre></details>
      <div className={styles.transcript} ref={output}>
        {messages.map((message, index) => <div key={index} className={message.role === "user" ? styles.question : styles.answer}><ReactMarkdown remarkPlugins={[remarkGfm]}>{message.content}</ReactMarkdown></div>)}
        {answer && <div className={styles.answer}><ReactMarkdown remarkPlugins={[remarkGfm]}>{answer}</ReactMarkdown></div>}
      </div>
      {!api || !snapshot ? <p role="alert">Connect an assistant runtime to ask a question.</p> : !ready && !error ? <p role="status"><LoaderCircle size={14} className="spin" /> Preparing a temporary copy of the conversation…</p> : null}
      {error && <div role="alert" className={styles.error}>{error}{!ready && <button type="button" className="button quiet" onClick={() => setAttempt(value => value + 1)}>Try again</button>}</div>}
      {busy && <p role="status" className={styles.status}>{needsAction.current ? "Action needed" : "Thinking…"}</p>}
      <form className={styles.composer} onSubmit={event => { event.preventDefault(); void ask(); }}>
        <textarea data-autofocus aria-label="Question for Nebula" placeholder={messages.length ? "Ask a follow-up…" : "What would you like to know?"} rows={2} maxLength={4000} value={question} disabled={busy} onChange={event => setQuestion(event.target.value)} onKeyDown={event => { if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) { event.preventDefault(); void ask(); } }} />
        {busy ? <button className="icon-button subtle" type="button" aria-label="Stop response" title="Stop response" onClick={() => void stop()}><Square size={17} /></button> : <button className="icon-button subtle" type="submit" aria-label="Ask question" title="Ask question" disabled={!ready || !question.trim()}><Send size={18} /></button>}
      </form>
    </div>
  </ModalSurface>;
}
