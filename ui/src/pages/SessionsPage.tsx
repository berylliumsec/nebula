import { useEffect, useMemo, useRef, useState, type FormEvent, type KeyboardEvent } from "react";
import {
  Braces,
  LoaderCircle,
  MessageSquare,
  Plus,
  Send,
  ShieldCheck,
  Square,
  SquareTerminal,
} from "lucide-react";
import { Link } from "react-router-dom";
import type {
  ChatCitation,
  ChatMessage,
  ChatSessionSummary,
  ChatUsage,
  PersistedChatMessage,
} from "../api/types";
import { ApiTerminalTransport } from "../api/terminal";
import { PageHeader } from "../components/PageHeader";
import { TerminalPanel } from "../components/TerminalPanel";
import { useWorkspace } from "../state/WorkspaceContext";

type SessionView = "terminal" | "chat";
type MessageState = "complete" | "streaming" | "error" | "cancelled";

interface ConversationMessage extends ChatMessage {
  id: string;
  createdAt: string;
  citations: ChatCitation[];
  usage?: ChatUsage;
  state: MessageState;
  durable: boolean;
  detail?: string;
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
  };
}

export function SessionsPage() {
  const [view, setView] = useState<SessionView>("chat");
  const [terminalSessionId, setTerminalSessionId] = useState(() => makeId("human"));
  const {
    api,
    activeOperator,
    coreState,
    engagement,
    health,
    knowledgeSources,
    previewMode,
    providers,
  } = useWorkspace();
  const terminalTransport = useMemo(
    () => (api && coreState === "online" && health?.humanPty === "ready"
      ? new ApiTerminalTransport(api.baseUrl, api.getToken())
      : undefined),
    [api, coreState, health?.humanPty],
  );
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
    setTerminalSessionId(makeId("human"));
  }, [engagement?.id]);

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
    setView("chat");
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
    setChatError(undefined);
    try {
      const summary = sessions.find((session) => session.id === id);
      const history = await api.listChatMessages(id);
      setSessionId(id);
      setMessages(history.map(persistedMessage));
      if (summary) {
        setProviderId(summary.providerId);
        setModel(summary.model ?? "");
      }
      setView("chat");
    } catch (error) {
      setChatError(error instanceof Error ? error.message : "Could not load the selected conversation.");
    } finally {
      setLoadingHistory(false);
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
      allowCloudKnowledge = window.confirm(
        `Allow this request to send redacted excerpts from ${knowledgeSources.length} knowledge source${knowledgeSources.length === 1 ? "" : "s"} to ${selectedProvider.name}? Local-only sources will remain blocked.`,
      );
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
        messages: [
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
              content: streamEvent.message.content,
              citations: streamEvent.citations,
              usage: streamEvent.usage,
              state: "complete",
              durable: true,
            };
          }));
        }
        if (streamEvent.type === "error") setChatError(streamEvent.detail);
      }, controller.signal);
      returnedSessionId = response.sessionId ?? returnedSessionId;
      if (returnedSessionId) await refreshSessions(returnedSessionId);
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

  return (
    <div className="page sessions-page">
      <PageHeader
        eyebrow="Operator workspace"
        title="Sessions"
        description="Human-operated terminals and cited analyst conversations stay separate from sandboxed tool calls."
        actions={view === "terminal"
          ? <button className="button primary" type="button" disabled={previewMode || health?.humanPty !== "ready"} onClick={() => setTerminalSessionId(makeId("human"))}><Plus size={16} /> New terminal</button>
          : <button className="button primary" type="button" disabled={previewMode || !engagement} title={!engagement ? "Create or select an engagement before starting chat" : undefined} onClick={newConversation}><Plus size={16} /> New chat</button>}
      />

      <div className="session-toolbar">
        <div className="session-tabs" role="tablist" aria-label="Session views">
          <button type="button" role="tab" aria-selected={view === "terminal"} onClick={() => setView("terminal")}><SquareTerminal size={16} /> Human terminal</button>
          <button type="button" role="tab" aria-selected={view === "chat"} onClick={() => setView("chat")}><MessageSquare size={16} /> Analyst chat</button>
        </div>
        <div className="session-scope"><ShieldCheck size={15} /> Human controlled · {engagement?.name ?? (previewMode ? "ACME-EXT preview" : "no engagement")}</div>
      </div>

      <div className="session-layout">
        <section className="session-workspace">
          {view === "terminal" ? (
            <TerminalPanel sessionId={terminalSessionId} transport={terminalTransport} />
          ) : (
            <div className="chat-panel">
              {!previewMode && <div className="chat-context-bar">
                <label><span>Conversation</span><select aria-label="Conversation" value={sessionId} disabled={sending || loadingHistory} onChange={(event) => void selectSession(event.target.value)}><option value="">New conversation</option>{sessions.map((session) => <option value={session.id} key={session.id}>{session.title}</option>)}</select></label>
                <label><span>Provider</span><select aria-label="Chat provider" value={providerId} disabled={sending || Boolean(sessionId)} onChange={(event) => selectProvider(event.target.value)}><option value="">Select provider</option>{enabledProviders.map((provider) => <option value={provider.id} key={provider.id}>{provider.name} · {provider.state}</option>)}</select></label>
                <label><span>Model</span><input aria-label="Chat model" value={model} disabled={sending || Boolean(sessionId)} list="chat-models" placeholder="Exact model ID" onChange={(event) => setModel(event.target.value)} /><datalist id="chat-models">{selectedProvider?.models.map((item) => <option value={item} key={item} />)}</datalist></label>
                <label className="chat-knowledge-toggle"><input type="checkbox" checked={includeKnowledge && canUseKnowledge} disabled={!canUseKnowledge || sending} onChange={(event) => setIncludeKnowledge(event.target.checked)} /><span>Use knowledge<small>{knowledgeSources.length ? cloudKnowledgeAllowed ? `${knowledgeSources.length} source${knowledgeSources.length === 1 ? "" : "s"}` : "Profile is text-only" : "No sources loaded"}</small></span></label>
              </div>}
              <div className="chat-scroll" ref={scrollRef} aria-live="polite">
                {previewMode ? <>
                  <article className="chat-message assistant"><span className="chat-avatar">N</span><div><header><strong>Nebula assistant</strong><span>19:07</span></header><p>I found two prior observations relevant to the gateway. Both are cited below; no command was executed.</p><span className="citation-chip"><Braces size={13} /> Observation #184 · Nmap import</span><span className="citation-chip"><Braces size={13} /> Advisory · CVE record</span></div></article>
                  <article className="chat-message operator"><span className="chat-avatar">JD</span><div><header><strong>You</strong><span>19:08</span></header><p>Summarize the applicability evidence and list what still needs independent verification.</p></div></article>
                </> : loadingHistory ? <div className="chat-thinking"><LoaderCircle className="spin" size={14} /> Loading conversation…</div> : messages.length ? messages.map((message) => (
                  <article className={`chat-message ${message.role === "user" ? "operator" : "assistant"}`} key={message.id}>
                    <span className="chat-avatar">{message.role === "user" ? "You" : "N"}</span>
                    <div><header><strong>{message.role === "user" ? "You" : "Nebula assistant"}</strong><span>{timeLabel(message.createdAt)}</span>{message.usage && <span>{message.usage.totalTokens} tokens</span>}</header>{message.content && <p>{message.content}</p>}{message.state === "streaming" && !message.content && <div className="chat-thinking"><span /><span /><span /> Waiting for provider</div>}{message.detail && <p className="chat-message-error" role="alert">{message.detail}</p>}{message.citations.map((citation) => <Link className="citation-chip" to={`/knowledge?source=${encodeURIComponent(citation.sourceId)}`} title={citation.excerpt} key={`${citation.sourceId}-${citation.chunkId}`}><Braces size={13} /> {citation.name}{citation.page ? ` · p. ${citation.page}` : ""}</Link>)}</div>
                  </article>
                )) : <div className="empty-state compact"><MessageSquare size={23} /><strong>Start an analyst conversation</strong><p>Select a configured provider and exact model. Chat is analysis-only; executable tools stay disabled.</p></div>}
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

        <aside className="session-inspector" aria-label="Session inspector">
          <header><div><span>Context</span><strong>Session details</strong></div></header>
          <dl><div><dt>Active operator</dt><dd>{previewMode ? "Jordan Diaz" : activeOperator?.displayName ?? "No active operator"}</dd></div><div><dt>{view === "chat" ? "Conversation" : "Terminal"}</dt><dd>{view === "chat" ? sessionId ? sessions.find((session) => session.id === sessionId)?.title ?? "Saved chat" : "Unsaved chat" : terminalSessionId}</dd></div><div><dt>Provider</dt><dd>{view === "chat" ? selectedProvider?.name ?? "Not selected" : "Not applicable"}</dd></div><div><dt>Human PTY</dt><dd><span className={`status-dot ${health?.humanPty === "ready" ? "healthy" : "unavailable"}`} /> {health?.humanPty ?? "Core unavailable"}</dd></div></dl>
          {view === "chat" ? <><section><h3>Knowledge boundary</h3><div className="scope-chip-list"><span>{knowledgeSources.length} source{knowledgeSources.length === 1 ? "" : "s"}</span><span>{providerIsLocal ? "Local retrieval" : includeKnowledge && canUseKnowledge ? "Confirm each cloud request" : "Text only"}</span></div></section><section><h3>Session evidence</h3><div className="empty-state mini"><Braces size={19} /><p>Citations identify ingested chunks and open the matching source record. Chat cannot execute tools.</p></div></section></> : <section><h3>Human terminal boundary</h3><div className="empty-state mini"><SquareTerminal size={19} /><p>The terminal is human-operated and does not use a model provider or knowledge retrieval. Output is not evidence until explicitly captured.</p></div></section>}
        </aside>
      </div>
    </div>
  );
}
