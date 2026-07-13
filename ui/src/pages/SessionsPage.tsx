import { useEffect, useMemo, useRef, useState, type FormEvent, type KeyboardEvent } from "react";
import {
  Braces,
  FileClock,
  FolderOpen,
  LoaderCircle,
  MessageSquare,
  Plus,
  Send,
  ShieldCheck,
  Square,
} from "lucide-react";
import { Link } from "react-router-dom";
import type {
  ChatCitation,
  ChatMessage,
  ChatSessionSummary,
  ChatUsage,
  ContextStatus,
  ExecutionCapabilities,
  ExecutionLanguage,
  PersistedChatMessage,
} from "../api/types";
import { AssistantMarkdown, type FencedRunCandidate } from "../components/AssistantMarkdown";
import { ExecutionHistory } from "../components/ExecutionHistory";
import { ExecutionReviewDialog } from "../components/ExecutionReviewDialog";
import { PageHeader } from "../components/PageHeader";
import { useConfirmation } from "../components/DialogSystem";
import { WorkspacePanel } from "../components/WorkspacePanel";
import { useWorkspace } from "../state/WorkspaceContext";

type SessionView = "chat" | "executions" | "workspace";
type MessageState = "complete" | "streaming" | "error" | "cancelled";

interface ConversationMessage extends ChatMessage {
  id: string;
  createdAt: string;
  citations: ChatCitation[];
  usage?: ChatUsage;
  state: MessageState;
  durable: boolean;
  detail?: string;
  sequence?: number;
}

function makeId(prefix: string): string {
  return `${prefix}-${globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random()}`}`;
}

function timeLabel(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "Now";
  return new Intl.DateTimeFormat(undefined, { hour: "numeric", minute: "2-digit" }).format(date);
}

function persistedMessage(message: PersistedChatMessage): ConversationMessage {
  return {
    id: message.id,
    role: message.role,
    content: message.content,
    createdAt: message.createdAt,
    citations: message.citations,
    usage: message.usage,
    state: "complete",
    durable: true,
    sequence: message.sequence,
  };
}

export function SessionsPage() {
  const confirm = useConfirmation();
  const [view, setView] = useState<SessionView>("chat");
  const [mobileListOpen, setMobileListOpen] = useState(false);
  const {
    api,
    activeOperator,
    coreState,
    engagement,
    knowledgeSources,
    previewMode,
    providers,
  } = useWorkspace();
  const [executionCapabilities, setExecutionCapabilities] = useState<ExecutionCapabilities>();
  const [runCandidate, setRunCandidate] = useState<FencedRunCandidate>();
  const [executionRefresh, setExecutionRefresh] = useState(0);
  const [sessions, setSessions] = useState<ChatSessionSummary[]>([]);
  const [sessionId, setSessionId] = useState("");
  const [providerId, setProviderId] = useState("");
  const [model, setModel] = useState("");
  const [includeKnowledge, setIncludeKnowledge] = useState(true);
  const [messages, setMessages] = useState<ConversationMessage[]>([]);
  const [draft, setDraft] = useState("");
  const [sending, setSending] = useState(false);
  const [loadingHistory, setLoadingHistory] = useState(false);
  const [chatError, setChatError] = useState<string>();
  const [contextStatus, setContextStatus] = useState<ContextStatus>();
  const [contextLoading, setContextLoading] = useState(false);
  const [contextError, setContextError] = useState<string>();
  const abortRef = useRef<AbortController | undefined>(undefined);
  const scrollRef = useRef<HTMLDivElement>(null);
  const enabledProviders = useMemo(() => providers.filter((provider) => provider.enabled), [providers]);
  const selectedProvider = enabledProviders.find((provider) => provider.id === providerId);
  const providerIsLocal = selectedProvider?.kind === "local" || selectedProvider?.privacy === "local_only";
  const cloudKnowledgeAllowed = providerIsLocal || selectedProvider?.permitsSensitiveData === true;
  const canUseKnowledge = knowledgeSources.length > 0 && cloudKnowledgeAllowed;

  useEffect(() => {
    if (!enabledProviders.length) {
      setProviderId("");
      setModel("");
      return;
    }
    if (enabledProviders.some((provider) => provider.id === providerId)) return;
    const provider = enabledProviders[0];
    setProviderId(provider.id);
    setModel(provider.defaultModel ?? provider.models[0] ?? "");
  }, [enabledProviders, providerId]);

  useEffect(() => {
    if (model || !selectedProvider) return;
    setModel(selectedProvider.defaultModel ?? selectedProvider.models[0] ?? "");
  }, [model, selectedProvider]);

  useEffect(() => {
    if (coreState !== "online" || !selectedProvider) return;
    setIncludeKnowledge(canUseKnowledge);
  }, [canUseKnowledge, coreState, selectedProvider]);

  useEffect(() => {
    abortRef.current?.abort();
    setSending(false);
    setSessions([]);
    setSessionId("");
    setMessages([]);
    setDraft("");
    setChatError(undefined);
    setContextStatus(undefined);
    setContextLoading(false);
    setContextError(undefined);
    setRunCandidate(undefined);
  }, [engagement?.id]);

  useEffect(() => {
    if (!api || coreState !== "online" || !engagement) {
      setExecutionCapabilities(undefined);
      return;
    }
    const controller = new AbortController();
    void api.executionCapabilities(engagement.id, controller.signal)
      .then(setExecutionCapabilities)
      .catch(() => setExecutionCapabilities(undefined));
    return () => controller.abort();
  }, [api, coreState, engagement]);

  useEffect(() => {
    if (!api || coreState !== "online" || !engagement) {
      setSessions([]);
      return;
    }
    const controller = new AbortController();
    void api.listChatSessions(engagement.id, controller.signal)
      .then((page) => setSessions(page.items.sort((left, right) => right.updatedAt.localeCompare(left.updatedAt))))
      .catch((error) => {
        if (!controller.signal.aborted) setChatError(error instanceof Error ? error.message : "Could not load conversations.");
      });
    return () => controller.abort();
  }, [api, coreState, engagement]);

  useEffect(() => {
    return () => abortRef.current?.abort();
  }, []);

  useEffect(() => {
    scrollRef.current?.scrollTo?.({ top: scrollRef.current.scrollHeight, behavior: "smooth" });
  }, [messages, sending]);

  const refreshSessions = async (selectedId?: string) => {
    if (!api || !engagement) return;
    const page = await api.listChatSessions(engagement.id);
    setSessions(page.items.sort((left, right) => right.updatedAt.localeCompare(left.updatedAt)));
    if (selectedId) setSessionId(selectedId);
  };

  const newConversation = () => {
    abortRef.current?.abort();
    setSending(false);
    setSessionId("");
    setMessages([]);
    setDraft("");
    setChatError(undefined);
    setContextStatus(undefined);
    setContextLoading(false);
    setContextError(undefined);
    setView("chat");
    setMobileListOpen(false);
  };

  const selectProvider = (id: string) => {
    const provider = enabledProviders.find((item) => item.id === id);
    setProviderId(id);
    setModel(provider?.defaultModel ?? provider?.models[0] ?? "");
    setIncludeKnowledge(Boolean(knowledgeSources.length && (provider?.kind === "local" || provider?.privacy === "local_only" || provider?.permitsSensitiveData)));
  };

  const selectSession = async (id: string) => {
    if (!id) {
      newConversation();
      return;
    }
    if (!api) return;
    setLoadingHistory(true);
    setContextLoading(true);
    setChatError(undefined);
    setContextError(undefined);
    try {
      const summary = sessions.find((session) => session.id === id);
      const [history, contextResult] = await Promise.all([
        api.listChatMessages(id),
        api.getChatContext(id).then((context) => ({ context })).catch(() => ({ context: undefined })),
      ]);
      setSessionId(id);
      setMessages(history.map(persistedMessage));
      setContextStatus(contextResult.context);
      setContextError(contextResult.context ? undefined : "Working memory inspection is temporarily unavailable.");
      if (summary) {
        setProviderId(summary.providerId);
        setModel(summary.model ?? "");
      }
      setView("chat");
      setMobileListOpen(false);
    } catch (error) {
      setChatError(error instanceof Error ? error.message : "Could not load the selected conversation.");
    } finally {
      setLoadingHistory(false);
      setContextLoading(false);
    }
  };

  const openAttachedChat = async (id: string) => {
    if (!api || !engagement) return;
    setLoadingHistory(true);
    setContextLoading(true);
    setChatError(undefined);
    setContextError(undefined);
    try {
      const [page, history, contextResult] = await Promise.all([
        api.listChatSessions(engagement.id),
        api.listChatMessages(id),
        api.getChatContext(id).then((context) => ({ context })).catch(() => ({ context: undefined })),
      ]);
      const ordered = page.items.sort((left, right) => right.updatedAt.localeCompare(left.updatedAt));
      const summary = ordered.find((session) => session.id === id);
      setSessions(ordered);
      setSessionId(id);
      setMessages(history.map(persistedMessage));
      setContextStatus(contextResult.context);
      setContextError(contextResult.context ? undefined : "Working memory inspection is temporarily unavailable.");
      if (summary) {
        setProviderId(summary.providerId);
        setModel(summary.model ?? "");
      }
      setView("chat");
      setMobileListOpen(false);
    } catch (error) {
      setChatError(error instanceof Error ? error.message : "Could not open the execution conversation.");
    } finally {
      setLoadingHistory(false);
      setContextLoading(false);
    }
  };

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    const content = draft.trim();
    if (!content || sending || !api || coreState !== "online" || !engagement || !selectedProvider || !model.trim()) return;

    const wantsKnowledge = includeKnowledge && knowledgeSources.length > 0;
    let allowCloudKnowledge = false;
    if (wantsKnowledge && !providerIsLocal) {
      if (!selectedProvider.permitsSensitiveData) {
        setChatError("This provider profile is text-only. Enable engagement/document data in Settings or turn off knowledge retrieval.");
        return;
      }
      allowCloudKnowledge = await confirm({
        title: "Share cited excerpts?",
        message: `Allow this request to send redacted excerpts from ${knowledgeSources.length} knowledge source${knowledgeSources.length === 1 ? "" : "s"} to ${selectedProvider.name}? Local-only sources will remain blocked.`,
        confirmLabel: "Allow this request",
      });
      if (!allowCloudKnowledge) {
        setChatError("Message not sent because cloud knowledge transfer was not approved.");
        return;
      }
    }

    const now = new Date().toISOString();
    const userId = makeId("user");
    const assistantId = makeId("assistant");
    const durableHistory = messages.filter((message) => message.durable && message.state === "complete");
    const userMessage: ConversationMessage = {
      id: userId,
      role: "user",
      content,
      createdAt: now,
      citations: [],
      state: "complete",
      durable: false,
    };
    const assistantMessage: ConversationMessage = {
      id: assistantId,
      role: "assistant",
      content: "",
      createdAt: now,
      citations: [],
      state: "streaming",
      durable: false,
    };
    setMessages((current) => [...current, userMessage, assistantMessage]);
    setDraft("");
    setChatError(undefined);
    setSending(true);
    const controller = new AbortController();
    abortRef.current = controller;
    const initialSessionId = sessionId || undefined;
    let returnedSessionId = initialSessionId;

    try {
      const response = await api.streamChat({
        providerId: selectedProvider.id,
        engagementId: engagement.id,
        sessionId: returnedSessionId,
        model: model.trim(),
        messages: returnedSessionId
          ? [{ role: "user", content }]
          : [
              ...durableHistory.map(({ role, content: historyContent }) => ({ role, content: historyContent })),
              { role: "user", content },
            ],
        includeKnowledge: wantsKnowledge,
        allowCloudKnowledge,
      }, (streamEvent) => {
        if (streamEvent.type === "started") {
          returnedSessionId = streamEvent.sessionId ?? returnedSessionId;
          if (streamEvent.sessionId) setSessionId(streamEvent.sessionId);
        }
        if (streamEvent.type === "delta" && streamEvent.delta) {
          setMessages((current) => current.map((message) => message.id === assistantId
            ? { ...message, content: message.content + streamEvent.delta }
            : message));
        }
        if (streamEvent.type === "done") {
          returnedSessionId = streamEvent.sessionId ?? returnedSessionId;
          setMessages((current) => current.map((message) => {
            if (message.id === userId) return { ...message, durable: true };
            if (message.id !== assistantId) return message;
            return {
              ...message,
              id: streamEvent.message.id ?? message.id,
              content: streamEvent.message.content,
              citations: streamEvent.citations,
              usage: streamEvent.usage,
              state: "complete",
              durable: Boolean(streamEvent.message.id),
            };
          }));
        }
        if (streamEvent.type === "error") setChatError(streamEvent.detail);
      }, controller.signal);
      returnedSessionId = response.sessionId ?? returnedSessionId;
      if (returnedSessionId) {
        await refreshSessions(returnedSessionId);
        setContextLoading(true);
        try {
          setContextStatus(await api.getChatContext(returnedSessionId));
          setContextError(undefined);
        } catch {
          setContextError("The answer completed, but working memory inspection is temporarily unavailable.");
        } finally {
          setContextLoading(false);
        }
      }
    } catch (error) {
      const cancelled = controller.signal.aborted;
      const detail = cancelled ? "Response stopped by the operator." : error instanceof Error ? error.message : "Chat completion failed.";
      setMessages((current) => current.map((message) => message.id === assistantId
        ? { ...message, state: cancelled ? "cancelled" : "error", detail }
        : message));
      setChatError(detail);
      if (returnedSessionId && !cancelled) {
        try {
          const authoritative = await api.listChatMessages(returnedSessionId);
          if (authoritative.length) setMessages(authoritative.map(persistedMessage));
          await refreshSessions(returnedSessionId);
        } catch {
          // Keep the visible safe error when Core history cannot be refreshed.
          setSessionId(initialSessionId ?? "");
        }
      } else if (!initialSessionId) {
        setSessionId("");
      }
    } finally {
      if (abortRef.current === controller) abortRef.current = undefined;
      setSending(false);
    }
  };

  const onComposerKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      event.currentTarget.form?.requestSubmit();
    }
  };

  const canSend = Boolean(api && coreState === "online" && engagement && selectedProvider && model.trim() && draft.trim() && !sending);
  const runnableLanguages = useMemo(() => new Set<ExecutionLanguage>(
    executionCapabilities?.runtimes
      .filter((runtime) => runtime.offline && runtime.scopedNetwork)
      .map((runtime) => runtime.language) ?? [],
  ), [executionCapabilities]);

  return (
    <div className="page sessions-page">
      <PageHeader
        eyebrow="Operator workspace"
        title="Sessions"
        description="Cited analyst chat, reviewed disposable code execution, durable history, and the engagement workspace share one controlled surface."
        actions={view === "chat" ? <button className="button primary" type="button" disabled={previewMode || !engagement} title={!engagement ? "Create or select an engagement before starting chat" : undefined} onClick={newConversation}><Plus size={16} /> New chat</button> : undefined}
      />

      <div className="session-toolbar">
        <div className="session-tabs" role="tablist" aria-label="Session views">
          <button type="button" role="tab" aria-selected={view === "chat"} onClick={() => setView("chat")}><MessageSquare size={16} /> Analyst chat</button>
          <button type="button" role="tab" aria-selected={view === "executions"} onClick={() => setView("executions")}><FileClock size={16} /> Executions</button>
          <button type="button" role="tab" aria-selected={view === "workspace"} onClick={() => setView("workspace")}><FolderOpen size={16} /> Workspace</button>
        </div>
        {view === "chat" && <button className="session-mobile-list" type="button" aria-pressed={mobileListOpen} onClick={() => setMobileListOpen((value) => !value)}><MessageSquare size={15} /> {mobileListOpen ? "Current chat" : "Conversations"}</button>}
        <div className="session-scope"><ShieldCheck size={15} /> Human controlled · {engagement?.name ?? (previewMode ? "ACME-EXT preview" : "no engagement")}</div>
      </div>

      <div className={`session-layout ${view}${mobileListOpen ? " mobile-list-open" : ""}`}>
        {view === "chat" && <aside className="session-list" aria-label="Conversations">
          <header><div><span>Conversations</span><strong>{previewMode ? "Preview" : `${sessions.length} saved`}</strong></div><button className="icon-button subtle" type="button" aria-label="New conversation" disabled={previewMode || !engagement} onClick={newConversation}><Plus size={16} /></button></header>
          <nav>
            <button className={!sessionId ? "active" : undefined} type="button" onClick={newConversation}><MessageSquare size={16} /><span><strong>New conversation</strong><small>{selectedProvider?.name ?? "Choose a provider"}</small></span></button>
            {sessions.map((session) => <button className={session.id === sessionId ? "active" : undefined} type="button" key={session.id} onClick={() => void selectSession(session.id)}><MessageSquare size={16} /><span><strong>{session.title}</strong><small>{session.model || "Saved conversation"}</small></span></button>)}
            {previewMode && <button className="active" type="button" onClick={() => setMobileListOpen(false)}><MessageSquare size={16} /><span><strong>Gateway applicability review</strong><small>Local preview</small></span></button>}
          </nav>
        </aside>}
        <section className="session-workspace">
          {view === "executions" && api && engagement ? (
            <ExecutionHistory api={api} engagementId={engagement.id} refreshKey={executionRefresh} onRerun={setRunCandidate} providers={providers} onChatAttached={openAttachedChat} />
          ) : view === "workspace" && api && engagement ? (
            <WorkspacePanel api={api} engagementId={engagement.id} engagementName={engagement.name} />
          ) : view !== "chat" ? (
            <div className="empty-state"><FolderOpen size={24} /><strong>Select an engagement</strong><p>Execution history and workspace files are engagement-scoped.</p></div>
          ) : (
            <div className="chat-panel">
              {!previewMode && <div className="chat-context-bar">
                <label><span>Provider</span><select aria-label="Chat provider" value={providerId} disabled={sending || Boolean(sessionId)} onChange={(event) => selectProvider(event.target.value)}><option value="">Select provider</option>{enabledProviders.map((provider) => <option value={provider.id} key={provider.id}>{provider.name} · {provider.state}</option>)}</select></label>
                <label><span>Model</span><input aria-label="Chat model" value={model} disabled={sending || Boolean(sessionId)} list="chat-models" placeholder="Exact model ID" onChange={(event) => setModel(event.target.value)} /><datalist id="chat-models">{selectedProvider?.models.map((item) => <option value={item} key={item} />)}</datalist></label>
                <label className="chat-knowledge-toggle"><input type="checkbox" checked={includeKnowledge && canUseKnowledge} disabled={!canUseKnowledge || sending} onChange={(event) => setIncludeKnowledge(event.target.checked)} /><span>Use knowledge<small>{knowledgeSources.length ? cloudKnowledgeAllowed ? `${knowledgeSources.length} source${knowledgeSources.length === 1 ? "" : "s"}` : "Profile is text-only" : "No sources loaded"}</small></span></label>
              </div>}
              <div className="chat-scroll" ref={scrollRef} aria-live="polite">
                {previewMode ? <>
                  <article className="chat-message assistant"><span className="chat-avatar">N</span><div><header><strong>Nebula assistant</strong><span>19:07</span></header><AssistantMarkdown content="I found two prior observations relevant to the gateway. Both are cited below; no command was executed." durable={false} runnableLanguages={runnableLanguages} onRun={setRunCandidate} /><span className="citation-chip"><Braces size={13} /> Observation #184 · Nmap import</span><span className="citation-chip"><Braces size={13} /> Advisory · CVE record</span></div></article>
                  <article className="chat-message operator"><span className="chat-avatar">JD</span><div><header><strong>You</strong><span>19:08</span></header><p>Summarize the applicability evidence and list what still needs independent verification.</p></div></article>
                </> : loadingHistory ? <div className="chat-thinking"><LoaderCircle className="spin" size={14} /> Loading conversation…</div> : messages.length ? messages.map((message) => (
                  <article className={`chat-message ${message.role === "user" ? "operator" : "assistant"}`} data-sequence={message.sequence} key={message.id} tabIndex={-1}>
                    <span className="chat-avatar">{message.role === "user" ? "You" : "N"}</span>
                    <div><header><strong>{message.role === "user" ? "You" : "Nebula assistant"}</strong><span>{timeLabel(message.createdAt)}</span>{message.usage && <span>{message.usage.totalTokens} tokens</span>}</header>{message.content && (message.role === "assistant" && message.state === "complete" ? <AssistantMarkdown content={message.content} messageId={message.id} durable={message.durable} runnableLanguages={runnableLanguages} onRun={setRunCandidate} /> : <p>{message.content}</p>)}{message.state === "streaming" && !message.content && <div className="chat-thinking"><span /><span /><span /> Waiting for provider</div>}{message.detail && <p className="chat-message-error" role="alert">{message.detail}</p>}{message.citations.map((citation) => <Link className="citation-chip" to={`/knowledge?source=${encodeURIComponent(citation.sourceId)}`} title={citation.excerpt} key={`${citation.sourceId}-${citation.chunkId}`}><Braces size={13} /> {citation.name}{citation.page ? ` · p. ${citation.page}` : ""}</Link>)}</div>
                  </article>
                )) : <div className="empty-state compact"><MessageSquare size={23} /><strong>Start an analyst conversation</strong><p>Chat is analysis-only. Commands remain inert fenced text until you choose Run and complete exact review.</p></div>}
              </div>
              {chatError && <p className="chat-error" role="alert">{chatError}</p>}
              <form className="chat-composer" onSubmit={(event) => void submit(event)}>
                <label className="sr-only" htmlFor="analyst-message">Message the analyst assistant</label>
                <textarea id="analyst-message" value={draft} disabled={previewMode || !engagement || !selectedProvider || loadingHistory} placeholder={previewMode ? "Connect Nebula Core to chat…" : !engagement ? "Create or select an engagement to chat…" : selectedProvider ? "Ask about this engagement…" : "Add and select a provider in Settings…"} rows={3} onKeyDown={onComposerKeyDown} onChange={(event) => setDraft(event.target.value)} />
                <footer><span>{includeKnowledge && canUseKnowledge ? providerIsLocal ? "Cited retrieval stays local" : "Cloud excerpts require confirmation" : "Text-only chat · executable tools disabled"}</span>{sending ? <button className="button secondary square" type="button" aria-label="Stop response" onClick={() => abortRef.current?.abort()}><Square size={15} /></button> : <button className="button primary square" type="submit" disabled={!canSend} aria-label="Send message"><Send size={16} /></button>}</footer>
              </form>
            </div>
          )}
        </section>

        {view === "chat" && <aside className="session-inspector" aria-label="Session inspector">
          <header><div><span>Context</span><strong>Session details</strong></div></header>
          <dl><div><dt>Active operator</dt><dd>{previewMode ? "Jordan Diaz" : activeOperator?.displayName ?? "No active operator"}</dd></div><div><dt>Conversation</dt><dd>{sessionId ? sessions.find((session) => session.id === sessionId)?.title ?? "Saved chat" : "Unsaved chat"}</dd></div><div><dt>Provider</dt><dd>{selectedProvider?.name ?? "Not selected"}</dd></div><div><dt>Code Run</dt><dd><span className={`status-dot ${executionCapabilities?.ready ? "healthy" : "unavailable"}`} /> {executionCapabilities?.ready ? "Review available" : "Unavailable"}</dd></div></dl>
          <section><h3>Working memory</h3>{contextLoading ? <p role="status">Loading working memory…</p> : contextError ? <p role="alert">{contextError}</p> : contextStatus?.status ? <><div className="scope-chip-list"><span>Memory: {contextStatus.status.replace("_", " ")}</span><span>{contextStatus.estimatedInputTokens} / {contextStatus.targetInputTokens} estimated tokens</span></div>{contextStatus.snapshot?.memory ? <div className="empty-state mini"><Braces size={19} /><p>{contextStatus.snapshot.memory.summary}</p><small>Derived through sequence {contextStatus.compactedThrough} · {contextStatus.snapshot.providerId}/{contextStatus.snapshot.model} · {contextStatus.snapshot.usage.totalTokens} compaction tokens · ${contextStatus.snapshot.costUsd.toFixed(4)}</small><div className="scope-chip-list">{contextStatus.snapshot.sourceReferences.filter((source) => source.sequence).slice(0, 8).map((source) => <button aria-label={`Go to transcript message ${source.sequence}`} type="button" key={`${source.sourceId}-${source.sequence}`} onClick={() => { const target = document.querySelector<HTMLElement>(`[data-sequence="${source.sequence}"]`); target?.scrollIntoView?.({ behavior: "smooth", block: "center" }); target?.focus(); }}>Message #{source.sequence}</button>)}</div></div> : contextStatus.status === "failed" ? <p role="alert">Context compaction failed. Retry with the configured provider before continuing this conversation.</p> : <p>{contextStatus.status === "stale" ? "Working memory is stale and will refresh before the next answer that requires compaction." : "Compaction has not been needed for this conversation."}</p>}</> : <p>Select a saved conversation to inspect its working memory.</p>}</section>
          <section><h3>Knowledge boundary</h3><div className="scope-chip-list"><span>{knowledgeSources.length} source{knowledgeSources.length === 1 ? "" : "s"}</span><span>{providerIsLocal ? "Local retrieval" : includeKnowledge && canUseKnowledge ? "Confirm each cloud request" : "Text only"}</span></div></section>
          <section><h3>Execution boundary</h3><div className="empty-state mini"><Braces size={19} /><p>Chat has no tools. A completed, persisted supported fence can be copied or separately reviewed for a disposable container.</p></div></section>
          <section><h3>Session evidence</h3><div className="empty-state mini"><Braces size={19} /><p>Derived memory is not evidence. Citations still identify canonical ingested chunks and transcript messages.</p></div></section>
        </aside>}
      </div>
      {runCandidate && api && engagement && <ExecutionReviewDialog api={api} engagementId={engagement.id} candidate={runCandidate} capabilities={executionCapabilities} onClose={() => setRunCandidate(undefined)} onStarted={() => { setExecutionRefresh((value) => value + 1); setView("executions"); }} />}
    </div>
  );
}
