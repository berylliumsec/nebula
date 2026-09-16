import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { GripHorizontal, LoaderCircle, Minimize2, Send, Square, X } from "lucide-react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import type { ApiClient } from "../api/client";
import type { ChatCompletionRequest, ChatMessage, ChatSessionSummary } from "../api/types";
import type { SelectionActionDraft } from "./selection";
import { createPortal } from "react-dom";
import { sha256Hex } from "../sha256";
import { logCaughtDiagnostic } from "../diagnostics";
import styles from "./AskNebulaPopup.module.css";

export type AssistantSnapshot = Omit<ChatCompletionRequest, "messages" | "contextAttachments">;

export function AskNebulaPopup({ api, snapshot, context, onClose, onVisibilityChange }: {
  api?: ApiClient; snapshot?: AssistantSnapshot; context: SelectionActionDraft; onClose(): void; onVisibilityChange?: (visible: boolean) => void;
}) {
  const panel = useRef<HTMLDivElement>(null);
  const input = useRef<HTMLTextAreaElement>(null);
  const launcher = useRef<HTMLButtonElement>(null);
  const launcherShell = useRef<HTMLDivElement>(null);
  const [hidden, setHidden] = useState(false);
  const previousContext = useRef(context);
  useEffect(() => {
    if (previousContext.current !== context) {
      previousContext.current = context;
      setHidden(false);
    }
  }, [context]);
  const [launcherPosition, setLauncherPosition] = useState<{ x: number; y: number }>();
  const [position, setPosition] = useState({ x: Math.max(12, window.innerWidth - 584), y: 80 });
  const drag = useRef<{ x: number; y: number; left: number; top: number } | undefined>(undefined);
  const launcherDrag = useRef<{ x: number; y: number; left: number; top: number } | undefined>(undefined);
  const moveLauncher = (x: number, y: number) => {
    const rect = launcherShell.current?.getBoundingClientRect();
    const viewport = window.visualViewport;
    const left = viewport?.offsetLeft ?? 0;
    const top = viewport?.offsetTop ?? 0;
    const nativeSurface = document.querySelector('.persistent-browser:not([hidden]) .workbench-browser .browser-surface')?.getBoundingClientRect();
    const launcherHeight = rect?.height ?? 64;
    const maxY = Math.min(top + (viewport?.height ?? window.innerHeight) - launcherHeight - 12,
      nativeSurface ? nativeSurface.top - launcherHeight - 12 : Infinity);
    setLauncherPosition({
      x: Math.max(left + 12, Math.min(x, left + (viewport?.width ?? window.innerWidth) - (rect?.width ?? 288) - 12)),
      y: Math.max(top + 12, Math.min(y, maxY)),
    });
  };
  const move = (x: number, y: number) => {
    const rect = panel.current?.getBoundingClientRect();
    const viewport = window.visualViewport;
    const left = viewport?.offsetLeft ?? 0;
    const top = viewport?.offsetTop ?? 0;
    setPosition({
      x: Math.max(left + 12, Math.min(x, left + (viewport?.width ?? window.innerWidth) - (rect?.width ?? 560) - 12)),
      y: Math.max(top + 12, Math.min(y, top + (viewport?.height ?? window.innerHeight) - (rect?.height ?? 220) - 12)),
    });
  };
  useLayoutEffect(() => {
    if (hidden) {
      const rect = launcherShell.current?.getBoundingClientRect();
      if (rect) moveLauncher(rect.left, rect.top);
      launcher.current?.focus(); return;
    }
    const clamp = () => {
      const rect = panel.current?.getBoundingClientRect();
      if (rect) move(rect.left, rect.top);
    };
    const observer = new ResizeObserver(clamp);
    if (panel.current) observer.observe(panel.current);
    window.addEventListener("resize", clamp);
    window.visualViewport?.addEventListener("resize", clamp);
    window.visualViewport?.addEventListener("scroll", clamp);
    if (input.current) {
      input.current.value = question;
      input.current.focus();
    }
    clamp();
    return () => {
      observer.disconnect(); window.removeEventListener("resize", clamp);
      window.visualViewport?.removeEventListener("resize", clamp);
      window.visualViewport?.removeEventListener("scroll", clamp);
    };
  }, [hidden]);
  useLayoutEffect(() => {
    if (!hidden || !launcherPosition) return;
    const clamp = () => {
      const rect = launcherShell.current?.getBoundingClientRect();
      if (rect) moveLauncher(rect.left, rect.top);
    };
    window.addEventListener("resize", clamp);
    window.visualViewport?.addEventListener("resize", clamp);
    window.visualViewport?.addEventListener("scroll", clamp);
    return () => {
      window.removeEventListener("resize", clamp);
      window.visualViewport?.removeEventListener("resize", clamp);
      window.visualViewport?.removeEventListener("scroll", clamp);
    };
  }, [hidden, Boolean(launcherPosition)]);
  const [question, setQuestion] = useState("");
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [answer, setAnswer] = useState("");
  const [busy, setBusy] = useState(false);
  const [stopping, setStopping] = useState(false);
  const [progress, setProgress] = useState("Thinking…");
  const commentary = useRef("");
  const harnessTurnId = useRef<string | undefined>(undefined);
  const [ready, setReady] = useState(false);
  const [error, setError] = useState<string>();
  const [attempt, setAttempt] = useState(0);
  const branch = useRef<ChatSessionSummary | undefined>(undefined);
  const renewLease = useRef<(() => void) | undefined>(undefined);
  const controller = useRef<AbortController | undefined>(undefined);
  const needsAction = useRef(false);
  const turnId = useRef<string | undefined>(undefined);
  const output = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!api || !snapshot) return;
    let closed = false;
    let created: ChatSessionSummary | undefined;
    let leaseTimer: ReturnType<typeof setInterval> | undefined;
    let openTimer: ReturnType<typeof setTimeout> | undefined;
    const discard = (id: string) => {
      void api.discardTemporaryChat(id).catch(reason => {
        void logCaughtDiagnostic("interface.ask_nebula.cleanup_failed", "The popup closed; Core will expire its temporary conversation.", reason, "chat");
      });
    };
    setReady(false);
    setError(undefined);
    openTimer = setTimeout(() => {
      if (!closed) setError("Opening the temporary conversation is taking too long. Try again; your main conversation is unchanged.");
    }, 15_000);
    void api.createTemporaryChat(snapshot).then(session => {
      clearTimeout(openTimer);
      created = session;
      if (closed) { discard(session.id); return; }
      branch.current = session;
      const renew = () => {
        void api.keepTemporaryChatAlive(session.id).catch(reason => {
          void logCaughtDiagnostic("interface.ask_nebula.keepalive_failed", "The temporary assistant could not renew its active lease.", reason, "chat");
        });
      };
      leaseTimer = setInterval(renew, 5 * 60_000);
      window.addEventListener("focus", renew);
      renewLease.current = renew;
      setReady(true);
      setError(undefined);
    }).catch(reason => {
      clearTimeout(openTimer);
      void logCaughtDiagnostic("interface.ask_nebula.open_failed", "The temporary assistant could not open.", reason, "chat");
      if (!closed) setError(reason instanceof Error ? reason.message : "Could not open a temporary conversation. Try again.");
    });
    const unload = () => { if (created) discard(created.id); };
    window.addEventListener("pagehide", unload);
    return () => {
      closed = true;
      clearTimeout(openTimer);
      controller.current?.abort();
      clearInterval(leaseTimer);
      if (renewLease.current) window.removeEventListener("focus", renewLease.current);
      renewLease.current = undefined;
      window.removeEventListener("pagehide", unload);
      if (created) discard(created.id);
      branch.current = undefined;
    };
  }, [api, snapshot, attempt]);

  useEffect(() => { output.current?.scrollTo?.({ top: output.current.scrollHeight }); }, [hidden, messages, answer]);

  const stop = async () => {
    if (!api || stopping) return;
    const activeController = controller.current;
    setStopping(true);
    setError(undefined);
    let timeout: ReturnType<typeof setTimeout> | undefined;
    try {
      const cancel = async () => {
        if (harnessTurnId.current) return api.stopHarnessTurn(harnessTurnId.current);
        if (turnId.current) return api.cancelChatTurn(turnId.current);
        const pending = branch.current && await api.getPendingChatTurn(branch.current.id);
        if (!pending) throw new Error("Core has not confirmed this response yet. Try Stop again, or close the popup to discard it.");
        return pending.harnessTurnId ? api.stopHarnessTurn(pending.harnessTurnId) : api.cancelChatTurn(pending.id);
      };
      await Promise.race([cancel(), new Promise<never>((_, reject) => {
        timeout = setTimeout(() => reject(new Error("Core has not confirmed Stop. Try Stop again, or close the popup to discard it.")), 10_000);
      })]);
      activeController?.abort();
      needsAction.current = false;
      setBusy(false);
      setProgress("Stopped");
    } catch (reason) {
      void logCaughtDiagnostic("interface.ask_nebula.stop_failed", "The temporary response could not be stopped.", reason, "chat");
      setError(reason instanceof Error ? reason.message : "Could not stop the response. Try Stop again.");
    } finally { clearTimeout(timeout); setStopping(false); }
  };
  const ask = async () => {
    const session = branch.current;
    const prompt = input.current?.value.trim() ?? question.trim();
    if (!api || !session || !prompt || busy || stopping) return;
    const abort = new AbortController();
    controller.current = abort;
    turnId.current = undefined;
    harnessTurnId.current = undefined;
    commentary.current = "";
    setProgress("Connecting…");
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
        if ("harnessTurnId" in event && event.harnessTurnId) harnessTurnId.current = event.harnessTurnId;
        if (event.type === "started") { if (event.turnId) turnId.current = event.turnId; setProgress("Thinking…"); }
        if (event.type === "connection") setProgress(event.state === "reconnecting" ? "Reconnecting… You can still stop this response." : "Connected");
        if (event.type === "status") setProgress(event.detail);
        if (event.type === "output_delta" && event.stream === "commentary" && event.delta) {
          commentary.current = (commentary.current + event.delta).slice(-1200);
          setProgress(commentary.current);
        }
        if (event.type === "output_delta" && event.stream === "reasoning_summary" && !commentary.current) setProgress("Thinking through the context…");
        if (event.type === "turn_status" && ["cancelled", "interrupted", "failed"].includes(event.itemStatus ?? "")) {
          needsAction.current = false; abort.abort(); setBusy(false);
          setError(event.itemStatus === "failed" ? "The response failed. You can try your question again." : "Response stopped. Your question is ready to edit or resend.");
        }
        if (event.type === "delta" || event.type === "message_delta") setAnswer(current => current + event.delta);
        if (event.type === "approval_required" || event.type === "approval" || event.type === "interaction") {
          needsAction.current = true;
          setError("This question needs an action. Stop this response and ask a question that can be answered from the context.");
        }
      }, abort.signal);
      if (!abort.signal.aborted && result) {
        needsAction.current = false;
        setMessages(current => [...current, { role: "user", content: prompt }, result.message]);
        if (input.current) input.current.value = "";
        setQuestion(""); setAnswer("");
      }
    } catch (reason) {
      if (!abort.signal.aborted) {
        void logCaughtDiagnostic("interface.ask_nebula.response_failed", "The temporary assistant response failed.", reason, "chat");
        setError(reason instanceof Error ? reason.message : "The response failed. You can try your question again.");
      }
    } finally { setBusy(needsAction.current && !abort.signal.aborted); }
  };

  const launcherStatus = stopping ? "Stopping…" : busy ? needsAction.current ? "Action needed" : "Responding…" :
    error ? "Needs attention" : messages.length ? "Response ready" : ready ? "Ready to ask" : "Preparing…";
  return createPortal(hidden ?
    <div ref={launcherShell} className={styles.launcher} style={launcherPosition ? { left: launcherPosition.x, top: launcherPosition.y, right: "auto", bottom: "auto" } : undefined}>
      <button type="button" className={`icon-button subtle ${styles.move}`} aria-label="Move hidden Ask Nebula" title="Drag to move · arrow keys to reposition"
        onPointerDown={event => { if (event.button !== 0) return; const rect = launcherShell.current?.getBoundingClientRect(); if (!rect) return; event.currentTarget.setPointerCapture(event.pointerId); launcherDrag.current = { x: event.clientX, y: event.clientY, left: rect.left, top: rect.top }; }}
        onPointerMove={event => { if (launcherDrag.current) moveLauncher(launcherDrag.current.left + event.clientX - launcherDrag.current.x, launcherDrag.current.top + event.clientY - launcherDrag.current.y); }}
        onPointerUp={() => { launcherDrag.current = undefined; }} onPointerCancel={() => { launcherDrag.current = undefined; }} onLostPointerCapture={() => { launcherDrag.current = undefined; }}
        onKeyDown={event => { const directions: Record<string, [number, number]> = { ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, -1], ArrowDown: [0, 1] }; const direction = directions[event.key]; if (direction) { event.preventDefault(); const rect = launcherShell.current?.getBoundingClientRect(); if (rect) moveLauncher(rect.left + direction[0] * 24, rect.top + direction[1] * 24); } }}><GripHorizontal size={18} aria-hidden="true" /></button>
      <button ref={launcher} type="button" className={styles.launcherShow} aria-label={`Show Ask Nebula, ${launcherStatus}`} title="Show the same temporary conversation"
        onClick={() => { setHidden(false); onVisibilityChange?.(true); }}>
        <span className={styles.launcherText}><strong>Ask Nebula</strong><small role="status">{launcherStatus}</small></span>
        <span className={styles.show}>Show</span>
      </button>
    </div> :
    <div ref={panel} className={styles.popup} role="dialog" aria-labelledby="ask-nebula-title" style={{ left: position.x, top: position.y }}>
    <div data-selection-actions-disabled>
      <div className={styles.header}>
        <button type="button" className={`icon-button subtle ${styles.move}`} aria-label="Move Ask Nebula" title="Drag to move · arrow keys to reposition"
          onPointerDown={event => { if (event.button !== 0) return; event.currentTarget.setPointerCapture(event.pointerId); drag.current = { x: event.clientX, y: event.clientY, left: position.x, top: position.y }; }}
          onPointerMove={event => { if (drag.current) move(drag.current.left + event.clientX - drag.current.x, drag.current.top + event.clientY - drag.current.y); }}
          onPointerUp={() => { drag.current = undefined; }} onPointerCancel={() => { drag.current = undefined; }} onLostPointerCapture={() => { drag.current = undefined; }}
          onKeyDown={event => { const directions: Record<string, [number, number]> = { ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, -1], ArrowDown: [0, 1] }; const direction = directions[event.key]; if (direction) { event.preventDefault(); move(position.x + direction[0] * 24, position.y + direction[1] * 24); } }}><GripHorizontal size={18} aria-hidden="true" /></button>
        <div className={styles.title}><h2 id="ask-nebula-title">Ask Nebula</h2><small>Temporary · discarded when closed</small></div>
        <button className="icon-button subtle" type="button" aria-label="Hide Ask Nebula" title="Hide and keep this conversation"
          onClick={() => { setQuestion(input.current?.value ?? question); setHidden(true); onVisibilityChange?.(false); }}><Minimize2 size={18} aria-hidden="true" /></button>
        <button className="icon-button subtle" type="button" aria-label="Close Ask Nebula" title="Close and discard" onClick={onClose}><X size={18} aria-hidden="true" /></button></div>
      <details className={styles.context}><summary>{context.source.label}{context.truncated ? " · shortened" : ""}</summary><pre>{context.text}</pre></details>
      <div className={styles.transcript} ref={output}>
        {messages.map((message, index) => <div key={index} className={message.role === "user" ? styles.question : styles.answer}><ReactMarkdown remarkPlugins={[remarkGfm]}>{message.content}</ReactMarkdown></div>)}
        {answer && <div className={styles.answer}><ReactMarkdown remarkPlugins={[remarkGfm]}>{answer}</ReactMarkdown></div>}
      </div>
      {!api || !snapshot ? <p role="alert">Connect an assistant runtime to ask a question.</p> : !ready && !error ? <p role="status"><LoaderCircle size={14} className="spin" /> Preparing a temporary copy of the conversation…</p> : null}
      {error && <div role="alert" className={styles.error}>{error}{!ready && <button type="button" className="button quiet" onClick={() => setAttempt(value => value + 1)}>Try again</button>}</div>}
      {busy && <p role="status" className={styles.status}>{stopping ? "Stopping…" : needsAction.current ? "Action needed" : progress}</p>}
      <form className={styles.composer} onSubmit={event => { event.preventDefault(); void ask(); }}>
        <textarea ref={input} aria-label="Question for Nebula" placeholder={messages.length ? "Ask a follow-up…" : "What would you like to know?"} rows={2} maxLength={4000} disabled={busy || stopping} onInput={event => setQuestion(event.currentTarget.value)} onKeyDown={event => { if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) { event.preventDefault(); void ask(); } }} />
        {busy ? <button className="icon-button subtle" type="button" aria-label="Stop response" title="Stop response" disabled={stopping} onClick={() => void stop()}><Square size={17} /></button> : <button className="icon-button subtle" type="submit" aria-label="Ask question" title="Ask question" disabled={stopping || !ready || !question.trim()}><Send size={18} /></button>}
      </form>
    </div>
  </div>, document.body);
}
