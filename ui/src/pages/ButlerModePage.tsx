import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent, type KeyboardEvent } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { ArrowLeft, ArrowUp, ChevronDown, ChevronRight, ExternalLink, Plus, Search, SlidersHorizontal, X } from "lucide-react";
import type { ApiClient } from "../api/client";
import { boardElapsed, getButlerBoardIfChanged, groupButlerBoard, listButlerThreadsIfChanged, type ButlerBoard, type ButlerBoardGrouping, type ButlerBoardItem, type ButlerThread } from "../api/butlerBoard";
import type { ChatMessage, ChatStreamEvent } from "../api/types";
import { startVisiblePoll } from "../api/visiblePoll";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { useConfirmation } from "../components/DialogSystem";
import { logCaughtDiagnostic } from "../diagnostics";
import { useCompactLayout } from "../hooks/useCompactLayout";
import { readStorage, writeStorage } from "../state/browserStorage";
import { useWorkspace } from "../state/WorkspaceContext";
import { butlerQuestionAbout, butlerThreadRequest, butlerThreadReservation, butlerThreadRuntime, butlerThreadTitle } from "./butlerThreads";
import styles from "./ButlerModePage.module.css";

type Approval = Awaited<ReturnType<ApiClient["getApproval"]>>;
type LiveTurn = { prompt: string; text: string; browserAction?: boolean; error?: string; done?: boolean };

const GROUPING_KEY = "nebula.butler-mode.grouping";
const SUBAGENTS_KEY = "nebula.butler-mode.subagents";
const STATE_LABEL = { waiting: "Needs you", working: "Working", failed: "Failed", idle: "Idle" } as const;
const THREAD_STATE_LABEL = { waiting: "Needs you", working: "Working", idle: "Answered" } as const;

function chatPath(projectId: string, sessionId: string): string {
  return `/projects/${encodeURIComponent(projectId)}/workbench?view=chat&session=${encodeURIComponent(sessionId)}`;
}

function approvalDetail(approval: Approval | undefined): string | undefined {
  if (!approval) return undefined;
  const request = approval.exact_request as { tool_name?: unknown; arguments?: unknown };
  const tool = typeof request.tool_name === "string" ? request.tool_name : "Tool request";
  const args = request.arguments && typeof request.arguments === "object" ? JSON.stringify(request.arguments, null, 2) : "";
  const text = `${tool}${approval.target ? ` · ${approval.target}` : ""}${args ? `\n${args}` : ""}`;
  return text.length > 1200 ? `${text.slice(0, 1199)}…` : text;
}

/**
 * Butler mode: talk to Butler in parallel threads beside a status board of
 * every project's chats. Core owns the board, threads and transcripts; the
 * URL owns the open thread and phone tab; this device remembers only how the
 * board is grouped.
 */
export function ButlerModePage() {
  const { api, coreState, engagement, engagements, providers, harnesses } = useWorkspace();
  const navigate = useNavigate();
  const confirm = useConfirmation();
  const compact = useCompactLayout();
  const [params, setParams] = useSearchParams();
  const openThreadId = params.get("thread") ?? undefined;
  const phoneTab = params.get("tab") === "threads" ? "threads" : "board";
  // The project Butler mode was opened from: new threads start there and Exit returns there.
  const originProject = params.get("project") ?? engagement?.id;

  const [board, setBoard] = useState<ButlerBoard>();
  const [boardError, setBoardError] = useState("");
  const [search, setSearch] = useState("");
  const [query, setQuery] = useState("");
  const [grouping, setGrouping] = useState<ButlerBoardGrouping>(() => readStorage(GROUPING_KEY) === "project" ? "project" : "attention");
  const [showSubagents, setShowSubagents] = useState(() => readStorage(SUBAGENTS_KEY) === "true");
  const [organizeOpen, setOrganizeOpen] = useState(false);
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set());
  const [idle, setIdle] = useState<{ open: boolean; items: ButlerBoardItem[]; nextOffset?: number; loading: boolean; error?: string }>({ open: false, items: [], loading: false });
  const [expanded, setExpanded] = useState<string>();
  const [approval, setApproval] = useState<{ id: string; value?: Approval; error?: string }>();
  const [deciding, setDeciding] = useState<string>();
  const [actionNote, setActionNote] = useState<Record<string, string>>({});

  const [threads, setThreads] = useState<ButlerThread[]>([]);
  const [threadsError, setThreadsError] = useState("");
  const [messages, setMessages] = useState<Record<string, ChatMessage[]>>({});
  const [threadError, setThreadError] = useState<Record<string, string>>({});
  const [live, setLive] = useState<Record<string, LiveTurn>>({});
  const [draft, setDraft] = useState("");
  const [target, setTarget] = useState<"new" | "reply">("new");
  const [about, setAbout] = useState<ButlerBoardItem>();
  // The operator's pick for new threads; until then they start in the origin project.
  const [projectChoice, setNewThreadProject] = useState<string>();
  const newThreadProject = projectChoice ?? originProject;
  const [composerError, setComposerError] = useState("");
  const [starting, setStarting] = useState(false);

  const boardEtag = useRef<string | undefined>(undefined);
  const threadsEtag = useRef<string | undefined>(undefined);
  const boardPoll = useRef<{ poke: () => void } | undefined>(undefined);
  const threadsPoll = useRef<{ poke: () => void } | undefined>(undefined);
  const streams = useRef(new Map<string, AbortController>());
  const previousThreadState = useRef(new Map<string, string>());
  const composerRef = useRef<HTMLTextAreaElement>(null);
  const [clock, setClock] = useState(() => Date.now());

  useEffect(() => { const timer = window.setTimeout(() => setQuery(search), 300); return () => window.clearTimeout(timer); }, [search]);
  useEffect(() => { const timer = window.setInterval(() => setClock(Date.now()), 15_000); return () => window.clearInterval(timer); }, []);
  useEffect(() => () => { for (const controller of streams.current.values()) controller.abort(); }, []);

  // The board: Core's cross-project read, polled while visible.
  useEffect(() => {
    if (!api || coreState !== "online") return;
    const controller = new AbortController();
    boardEtag.current = undefined;
    const poll = startVisiblePoll({
      intervalMs: 4000,
      maxIntervalMs: 20_000,
      signal: controller.signal,
      read: async (signal) => {
        try {
          const answer = await getButlerBoardIfChanged(api, { q: query }, boardEtag.current, signal);
          setBoardError("");
          if (!answer) return "idle";
          boardEtag.current = answer.etag;
          setBoard(answer.board);
          return answer.board.counts.working || answer.board.counts.waiting ? "active" : "idle";
        } catch (caught) {
          if (signal.aborted) return "stop";
          void logCaughtDiagnostic("interface.butler_mode.board_failed", "Butler mode could not read the status board.", caught, "butler_mode");
          setBoardError(caught instanceof Error ? caught.message : "Core could not read the status board.");
          return "idle";
        }
      },
    });
    boardPoll.current = poll;
    return () => controller.abort();
  }, [api, coreState, query]);

  // Threads: every project's Butler mode threads, newest first.
  useEffect(() => {
    if (!api || coreState !== "online") return;
    const controller = new AbortController();
    threadsEtag.current = undefined;
    const poll = startVisiblePoll({
      intervalMs: 3000,
      maxIntervalMs: 15_000,
      signal: controller.signal,
      read: async (signal) => {
        try {
          const answer = await listButlerThreadsIfChanged(api, threadsEtag.current, signal);
          setThreadsError("");
          if (!answer) return "idle";
          threadsEtag.current = answer.etag;
          setThreads(answer.threads);
          return answer.threads.some((item) => item.state === "working") ? "active" : "idle";
        } catch (caught) {
          if (signal.aborted) return "stop";
          void logCaughtDiagnostic("interface.butler_mode.threads_failed", "Butler mode could not read its threads.", caught, "butler_mode");
          setThreadsError(caught instanceof Error ? caught.message : "Core could not read Butler's threads.");
          return "idle";
        }
      },
    });
    threadsPoll.current = poll;
    return () => controller.abort();
  }, [api, coreState]);

  const loadMessages = useCallback(async (sessionId: string) => {
    if (!api) return;
    try {
      const saved = await api.listChatMessages(sessionId);
      setMessages((current) => ({ ...current, [sessionId]: saved.filter((item) => item.role === "user" || item.role === "assistant") }));
      setThreadError((current) => { const next = { ...current }; delete next[sessionId]; return next; });
    } catch (caught) {
      void logCaughtDiagnostic("interface.butler_mode.thread_read_failed", "Butler mode could not read a thread.", caught, "butler_mode");
      setThreadError((current) => ({ ...current, [sessionId]: caught instanceof Error ? caught.message : "Core could not read this thread." }));
    }
  }, [api]);

  useEffect(() => { if (openThreadId) void loadMessages(openThreadId); }, [openThreadId, loadMessages]);

  // A thread Core finished (on this device or another) shows its saved answer.
  useEffect(() => {
    for (const thread of threads) {
      const before = previousThreadState.current.get(thread.sessionId);
      previousThreadState.current.set(thread.sessionId, thread.state);
      if (before && before !== thread.state && thread.sessionId === openThreadId && !streams.current.has(thread.sessionId)) void loadMessages(thread.sessionId);
    }
  }, [threads, openThreadId, loadMessages]);

  const openThread = useCallback((sessionId: string | undefined) => {
    setParams((current) => {
      const next = new URLSearchParams(current);
      if (sessionId) { next.set("thread", sessionId); next.set("tab", "threads"); } else next.delete("thread");
      return next;
    }, { replace: false });
    setTarget(sessionId ? "reply" : "new");
  }, [setParams]);

  const setPhoneTab = (tab: "board" | "threads") => setParams((current) => { const next = new URLSearchParams(current); next.set("tab", tab); if (tab === "board") next.delete("thread"); return next; });

  const exit = () => navigate(originProject ? `/projects/${encodeURIComponent(originProject)}/workbench` : "/");

  useEffect(() => {
    const onKey = (event: globalThis.KeyboardEvent) => {
      if (event.key !== "Escape" || event.defaultPrevented) return;
      if (organizeOpen) { setOrganizeOpen(false); return; }
      const active = document.activeElement;
      if (active instanceof HTMLElement && active.closest("[role=dialog]")) return;
      exit();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  });

  const groups = useMemo(() => groupButlerBoard(board?.items ?? [], grouping, showSubagents), [board, grouping, showSubagents]);
  const openThreadSummary = threads.find((item) => item.sessionId === openThreadId);
  const runningThreads = threads.filter((item) => item.state === "working").length;
  const visibleProjects = engagements.filter((item) => item.status !== "archived");

  const toggleGroup = (key: string) => setCollapsed((current) => { const next = new Set(current); if (next.has(key)) next.delete(key); else next.add(key); return next; });

  const loadIdle = async (offset: number) => {
    if (!api) return;
    setIdle((current) => ({ ...current, open: true, loading: true, error: undefined }));
    try {
      const answer = await getButlerBoardIfChanged(api, { q: query, state: "idle", offset, limit: 50 }, undefined);
      const page = answer?.board;
      setIdle((current) => ({ open: true, loading: false, items: offset ? [...current.items, ...(page?.items ?? [])] : page?.items ?? [], nextOffset: page?.nextOffset }));
    } catch (caught) {
      void logCaughtDiagnostic("interface.butler_mode.idle_failed", "Butler mode could not list idle chats.", caught, "butler_mode");
      setIdle((current) => ({ ...current, loading: false, error: caught instanceof Error ? caught.message : "Core could not list idle chats." }));
    }
  };
  useEffect(() => { setIdle({ open: false, items: [], loading: false }); }, [query]);

  const expand = async (item: ButlerBoardItem) => {
    if (expanded === item.sessionId) { setExpanded(undefined); return; }
    setExpanded(item.sessionId);
    setApproval(undefined);
    if (api && item.attention?.kind === "approval" && item.attention.actionId) {
      const id = item.attention.actionId;
      setApproval({ id });
      try {
        const value = await api.getApproval(id);
        setApproval((current) => current?.id === id ? { id, value } : current);
      } catch (caught) {
        void logCaughtDiagnostic("interface.butler_mode.approval_read_failed", "Butler mode could not read an approval.", caught, "butler_mode");
        setApproval((current) => current?.id === id ? { id, error: caught instanceof Error ? caught.message : "Core could not read this approval." } : current);
      }
    }
  };

  const decide = async (item: ButlerBoardItem, decision: "approve" | "reject") => {
    if (!api || !item.attention?.actionId) return;
    setDeciding(item.sessionId);
    try {
      await api.decideApproval(item.attention.actionId, { decision });
      // Core reports the chat's new state on the next board read.
      setActionNote((current) => ({ ...current, [item.sessionId]: decision === "approve" ? "Approval sent. Waiting for Core to resume the chat…" : "Denied. Waiting for Core to update the chat…" }));
      boardEtag.current = undefined;
      boardPoll.current?.poke();
    } catch (caught) {
      void logCaughtDiagnostic("interface.butler_mode.approval_failed", "Butler mode could not record an approval decision.", caught, "butler_mode");
      setActionNote((current) => ({ ...current, [item.sessionId]: `${caught instanceof Error ? caught.message : "Core could not record the decision."} Retry, or open the chat.` }));
    } finally {
      setDeciding(undefined);
    }
  };

  const stopChat = async (item: ButlerBoardItem) => {
    if (!api || !item.turnId) return;
    const approved = await confirm({ title: `Stop "${item.title}"?`, message: "The chat's current response stops. Work it already did is kept, and you can send it a new message later.", confirmLabel: "Stop response", tone: "danger" });
    if (!approved) return;
    try {
      await api.cancelChatTurn(item.turnId);
      setActionNote((current) => ({ ...current, [item.sessionId]: "Stop requested. Waiting for Core to confirm…" }));
      boardEtag.current = undefined;
      boardPoll.current?.poke();
    } catch (caught) {
      void logCaughtDiagnostic("interface.butler_mode.stop_failed", "Butler mode could not stop a chat.", caught, "butler_mode");
      setActionNote((current) => ({ ...current, [item.sessionId]: `${caught instanceof Error ? caught.message : "Core could not stop the response."} Retry, or open the chat.` }));
    }
  };

  const askAbout = (item: ButlerBoardItem) => {
    setAbout(item);
    setTarget("new");
    setNewThreadProject(item.project.id);
    if (compact) setPhoneTab("threads");
    window.setTimeout(() => composerRef.current?.focus(), 0);
  };

  const runTurn = async (projectId: string, sessionId: string, prompt: string, content: string, runtime: Extract<ReturnType<typeof butlerThreadRuntime>, { ok: true }>) => {
    if (!api) return;
    const controller = new AbortController();
    streams.current.set(sessionId, controller);
    setLive((current) => ({ ...current, [sessionId]: { prompt, text: "" } }));
    threadsPoll.current?.poke();
    try {
      await api.streamChat(butlerThreadRequest(projectId, sessionId, runtime, harnesses, content), (event: ChatStreamEvent) => {
        if (event.type === "delta" || event.type === "message_delta") {
          setLive((current) => current[sessionId] ? { ...current, [sessionId]: { ...current[sessionId], text: current[sessionId].text + event.delta } } : current);
        }
        if (event.type === "tool_completed" && event.receipt?.state === "pending_browser_apply") {
          setLive((current) => current[sessionId] ? { ...current, [sessionId]: { ...current[sessionId], browserAction: true } } : current);
        }
      }, controller.signal);
      setLive((current) => current[sessionId] ? { ...current, [sessionId]: { ...current[sessionId], done: true } } : current);
    } catch (caught) {
      if (controller.signal.aborted) return;
      void logCaughtDiagnostic("interface.butler_mode.turn_failed", "A Butler mode thread could not finish its response.", caught, "butler_mode");
      setLive((current) => ({ ...current, [sessionId]: { ...(current[sessionId] ?? { prompt, text: "" }), error: `${caught instanceof Error ? caught.message : "Butler's response failed."} The thread keeps what Core saved; reopen it to check before retrying.` } }));
    } finally {
      streams.current.delete(sessionId);
      await loadMessages(sessionId);
      setLive((current) => { if (current[sessionId]?.error) return current; const next = { ...current }; delete next[sessionId]; return next; });
      threadsEtag.current = undefined;
      threadsPoll.current?.poke();
    }
  };

  const send = async (event?: FormEvent) => {
    event?.preventDefault();
    const prompt = draft.trim();
    if (!api || !prompt || starting) return;
    setComposerError("");
    const replyThread = target === "reply" ? openThreadSummary : undefined;
    if (target === "reply" && !replyThread) { setComposerError("Choose a thread to reply to, or start a new thread."); return; }
    if (replyThread && (replyThread.state !== "idle" || streams.current.has(replyThread.sessionId))) { setComposerError("Butler is still answering this thread. Start a new thread to ask something else now."); return; }
    const projectId = replyThread?.project.id ?? newThreadProject ?? engagement?.id;
    if (!projectId) { setComposerError("Choose the project Butler should work in."); return; }
    setStarting(true);
    try {
      const current = await api.getControlAssistantSession(projectId);
      const config = current ? (await api.getChatAssistantConfig(current.id)).settings : undefined;
      const runtime = butlerThreadRuntime(config, providers, harnesses);
      if (!runtime.ok) { setComposerError(runtime.error); return; }
      const content = replyThread ? prompt : butlerQuestionAbout(about?.project.id === projectId ? about : undefined, prompt);
      const sessionId = replyThread?.sessionId ?? (await api.getOrCreateControlAssistantSession(butlerThreadReservation(projectId, runtime, butlerThreadTitle(prompt)))).id;
      setDraft("");
      setAbout(undefined);
      if (!replyThread) {
        const project = engagements.find((item) => item.id === projectId);
        setThreads((existing) => [{ sessionId, title: butlerThreadTitle(prompt), project: { id: projectId, name: project?.name ?? "" }, state: "working", updatedAt: new Date().toISOString() }, ...existing.filter((item) => item.sessionId !== sessionId)]);
        setMessages((existing) => ({ ...existing, [sessionId]: [] }));
        openThread(sessionId);
        setTarget("new");
      }
      void runTurn(projectId, sessionId, prompt, content, runtime);
    } catch (caught) {
      void logCaughtDiagnostic("interface.butler_mode.thread_start_failed", "Butler mode could not start a thread.", caught, "butler_mode");
      setComposerError(`${caught instanceof Error ? caught.message : "Core could not start the thread."} Your message is still in the box; retry when Core answers.`);
    } finally {
      setStarting(false);
    }
  };

  const onComposerKey = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) { event.preventDefault(); void send(); }
  };

  const counts = board?.counts;
  const boardView = <section className={styles.board} aria-label="Status board">
    <div className={styles.boardToolbar}>
      <div className={styles.organize}>
        <button type="button" className={styles.select} aria-haspopup="dialog" aria-expanded={organizeOpen} onClick={() => setOrganizeOpen((open) => !open)}>
          <SlidersHorizontal size={14} aria-hidden="true" /> Group: {grouping === "project" ? "Project" : "Attention"} <ChevronDown size={13} aria-hidden="true" />
        </button>
        {organizeOpen && <div className={styles.popover} role="dialog" aria-label="Organize the board">
          <div className={styles.popoverHeading}>Group by</div>
          {(["attention", "project"] as const).map((value) => <label key={value} className={styles.option}>
            <input type="radio" name="butler-board-grouping" checked={grouping === value} onChange={() => { setGrouping(value); writeStorage(GROUPING_KEY, value); }} />
            {value === "attention" ? "Attention" : "Project"}
          </label>)}
          <div className={styles.popoverHeading}>Show</div>
          <label className={styles.option}>
            <input type="checkbox" checked={showSubagents} onChange={(event) => { setShowSubagents(event.target.checked); writeStorage(SUBAGENTS_KEY, String(event.target.checked)); }} />
            Subagent chats as their own cards
          </label>
        </div>}
      </div>
      {board?.truncated && <span className={styles.muted}>Showing the first {board.items.length} active chats; search to narrow.</span>}
    </div>
    {boardError && <div className={styles.error} role="alert">{boardError} The board retries on its own.</div>}
    {!board && !boardError && <p className={styles.muted}>Reading every project's chats…</p>}
    {board && groups.length === 0 && <p className={styles.empty}>{query ? `No running, waiting or failed chats match “${query}”.` : "Nothing is running and nothing needs you."}</p>}
    {groups.map((group) => {
      const open = !collapsed.has(group.key);
      return <div key={group.key} className={styles.group}>
        <button type="button" className={styles.groupHead} aria-expanded={open} onClick={() => toggleGroup(group.key)}>
          {open ? <ChevronDown size={14} aria-hidden="true" /> : <ChevronRight size={14} aria-hidden="true" />}
          <span className={styles.groupLabel}>{group.label}</span>
          <span className={styles.muted}>{group.items.length}</span>
          {grouping === "project" && <span className={styles.groupCounts}>
            {group.counts.waiting > 0 && <span><i className={styles.dot} data-state="waiting" aria-hidden="true" />{group.counts.waiting} need you</span>}
            {group.counts.working > 0 && <span><i className={styles.dot} data-state="working" aria-hidden="true" />{group.counts.working} working</span>}
            {group.counts.failed > 0 && <span><i className={styles.dot} data-state="failed" aria-hidden="true" />{group.counts.failed} failed</span>}
          </span>}
        </button>
        {open && <div className={styles.cards}>{group.items.map((item) => <BoardCard key={item.sessionId} item={item} clock={clock} expanded={expanded === item.sessionId} approval={approval} deciding={deciding === item.sessionId} note={actionNote[item.sessionId]} onToggle={() => void expand(item)} onDecide={(decision) => void decide(item, decision)} onStop={() => void stopChat(item)} onAsk={() => askAbout(item)} onOpen={() => navigate(chatPath(item.project.id, item.sessionId))} showProject={grouping !== "project"} />)}</div>}
      </div>;
    })}
    {counts && counts.idle > 0 && <div className={styles.group}>
      <button type="button" className={styles.groupHead} aria-expanded={idle.open} onClick={() => idle.open ? setIdle((current) => ({ ...current, open: false })) : void loadIdle(0)}>
        {idle.open ? <ChevronDown size={14} aria-hidden="true" /> : <ChevronRight size={14} aria-hidden="true" />}
        <span className={styles.groupLabel}>Idle</span><span className={styles.muted}>{counts.idle.toLocaleString()}{idle.open ? "" : " · hidden"}</span>
      </button>
      {idle.open && <>
        {idle.error && <div className={styles.error} role="alert">{idle.error} <button type="button" className={styles.link} onClick={() => void loadIdle(idle.items.length)}>Retry</button></div>}
        <div className={styles.cards}>{idle.items.map((item) => <BoardCard key={item.sessionId} item={item} clock={clock} expanded={expanded === item.sessionId} approval={approval} deciding={false} onToggle={() => void expand(item)} onDecide={() => undefined} onStop={() => undefined} onAsk={() => askAbout(item)} onOpen={() => navigate(chatPath(item.project.id, item.sessionId))} showProject />)}</div>
        {idle.loading && <p className={styles.muted}>Loading idle chats…</p>}
        {idle.nextOffset !== undefined && !idle.loading && <button type="button" className={styles.link} onClick={() => void loadIdle(idle.nextOffset!)}>Show more idle chats</button>}
      </>}
    </div>}
  </section>;

  const threadsRail = <nav className={styles.rail} aria-label="Butler threads">
    <div className={styles.railHead}>
      <h2>Threads</h2>
      {runningThreads > 0 && <span className={styles.muted}>{runningThreads} running</span>}
      <button type="button" className={styles.newThread} onClick={() => { openThread(undefined); setTarget("new"); composerRef.current?.focus(); }}><Plus size={14} aria-hidden="true" /> New</button>
    </div>
    {threadsError && <div className={styles.error} role="alert">{threadsError}</div>}
    {threads.length === 0 && !threadsError && <p className={styles.empty}>Ask Butler something below. Each question runs as its own thread, so you can ask the next one right away.</p>}
    <ul className={styles.threadList}>{threads.map((thread) => {
      const state = live[thread.sessionId] && !live[thread.sessionId].error ? "working" : thread.state;
      return <li key={thread.sessionId}>
        <button type="button" className={styles.thread} aria-current={thread.sessionId === openThreadId ? "true" : undefined} onClick={() => openThread(thread.sessionId)}>
          <span className={styles.threadHead}><i className={styles.dot} data-state={state === "idle" ? "done" : state} aria-hidden="true" /><span className={styles.threadTitle}>{thread.title}</span><span className={styles.threadState} data-state={state}>{THREAD_STATE_LABEL[state]}</span></span>
          <span className={styles.muted}>{thread.project.name}</span>
        </button>
      </li>;
    })}</ul>
  </nav>;

  const openLive = openThreadId ? live[openThreadId] : undefined;
  const threadMessages = openThreadId ? messages[openThreadId] ?? [] : [];
  const conversation = <section className={styles.conversation} aria-label={openThreadSummary ? `Thread: ${openThreadSummary.title}` : "New Butler thread"}>
    <div className={styles.threadBar}>
      {compact && <button type="button" className={styles.iconButton} aria-label="Back to threads" title="Back to threads" onClick={() => openThread(undefined)}><ArrowLeft size={16} aria-hidden="true" /></button>}
      <h2>{openThreadSummary?.title ?? "New thread"}</h2>
      {openThreadSummary && <span className={styles.muted}>{openThreadSummary.project.name}</span>}
    </div>
    <div className={styles.messages} role="log" aria-label="Butler thread messages" aria-live="polite">
      {!openThreadId && <p className={styles.empty}>Ask Butler about anything across your projects — what needs you, what's stuck, what to pause. Butler keeps working on each thread while you start the next.</p>}
      {openThreadId && threadError[openThreadId] && <div className={styles.error} role="alert">{threadError[openThreadId]} <button type="button" className={styles.link} onClick={() => void loadMessages(openThreadId)}>Retry</button></div>}
      {threadMessages.map((message, index) => <div key={message.id ?? index} className={message.role === "user" ? styles.question : styles.answer}>
        {message.role === "user" ? message.content : <ReactMarkdown remarkPlugins={[remarkGfm]}>{message.content}</ReactMarkdown>}
      </div>)}
      {openLive && !threadMessages.some((message) => message.role === "user" && message.content.endsWith(openLive.prompt)) && <div className={styles.question}>{openLive.prompt}</div>}
      {openLive && (openLive.text ? <div className={styles.answer}><ReactMarkdown remarkPlugins={[remarkGfm]}>{openLive.text}</ReactMarkdown></div> : !openLive.error && <p className={styles.working}><i className={styles.dot} data-state="working" aria-hidden="true" /> Butler is working on this…</p>)}
      {openLive?.browserAction && <p className={styles.note}>Butler asked this device to finish a step in the browser (for example a picker or setting). Open the Butler panel to complete it there.</p>}
      {openLive?.error && <div className={styles.error} role="alert">{openLive.error}</div>}
      {openThreadSummary && !openLive && openThreadSummary.state !== "idle" && <p className={styles.working}><i className={styles.dot} data-state={openThreadSummary.state} aria-hidden="true" /> {openThreadSummary.state === "waiting" ? "Butler's answer is waiting for you. Open its conversation to approve or answer." : "Butler is working on this thread…"}</p>}
    </div>
    <form className={styles.composer} onSubmit={(event) => void send(event)}>
      {about && <div className={styles.about}><span className={styles.muted}>About</span><span className={styles.chip}><i className={styles.dot} data-state={about.state} aria-hidden="true" />{about.title}<button type="button" className={styles.chipClose} aria-label={`Stop asking about ${about.title}`} title="Remove" onClick={() => setAbout(undefined)}><X size={12} aria-hidden="true" /></button></span></div>}
      {composerError && <div className={styles.error} role="alert">{composerError}</div>}
      <div className={styles.inputRow}>
        <textarea ref={composerRef} rows={2} value={draft} onChange={(event) => setDraft(event.target.value)} onKeyDown={onComposerKey} aria-label="Message Butler" placeholder={target === "reply" && openThreadSummary ? "Reply in this thread" : "Ask Butler anything — each question runs as its own thread"} />
        <button type="submit" className={styles.send} aria-label={target === "reply" && openThreadSummary ? "Send reply" : "Start thread"} title={target === "reply" && openThreadSummary ? "Send reply" : "Start thread"} disabled={!draft.trim() || starting || !api}><ArrowUp size={16} aria-hidden="true" /></button>
      </div>
      <div className={styles.targets}>
        <button type="button" className={styles.target} aria-pressed={target === "new"} onClick={() => setTarget("new")}>New thread</button>
        {openThreadSummary && <button type="button" className={styles.target} aria-pressed={target === "reply"} onClick={() => setTarget("reply")}>Reply in this thread</button>}
        {target === "new" && <label className={styles.projectPick}><span className={styles.muted}>in</span>
          <select value={newThreadProject ?? ""} onChange={(event) => { setNewThreadProject(event.target.value); if (about && about.project.id !== event.target.value) setAbout(undefined); }} aria-label="Project for the new thread">
            {visibleProjects.map((project) => <option key={project.id} value={project.id}>{project.name}</option>)}
          </select>
        </label>}
      </div>
    </form>
  </section>;

  return <div className={styles.mode} data-compact={compact ? "true" : "false"}>
    <header className={styles.topBar}>
      <h1>Butler mode</h1>
      <label className={styles.search}>
        <Search size={15} aria-hidden="true" />
        <input type="search" value={search} onChange={(event) => setSearch(event.target.value)} placeholder="Search chats and projects" aria-label="Search chats and projects" />
      </label>
      {counts && <div className={styles.pulse} aria-label="Chat status across projects">
        <span><i className={styles.dot} data-state="waiting" aria-hidden="true" />{counts.waiting} need you</span>
        <span><i className={styles.dot} data-state="working" aria-hidden="true" />{counts.working} working</span>
        {counts.failed > 0 && <span><i className={styles.dot} data-state="failed" aria-hidden="true" />{counts.failed} failed</span>}
      </div>}
      <button type="button" className={styles.iconButton} aria-label="Exit Butler mode" title="Exit Butler mode (Esc)" onClick={exit}><X size={17} aria-hidden="true" /></button>
    </header>
    {coreState !== "online" && <div className={styles.error} role="alert">Core is unavailable. Butler mode reconnects and reads the board again when Core answers.</div>}
    {compact ? <>
      <div className={styles.tabs} role="tablist" aria-label="Butler mode">
        <button type="button" role="tab" aria-selected={phoneTab === "board"} onClick={() => setPhoneTab("board")}>Board{counts?.waiting ? <span className={styles.tabCount} data-state="waiting">{counts.waiting} need you</span> : null}</button>
        <button type="button" role="tab" aria-selected={phoneTab === "threads"} onClick={() => setPhoneTab("threads")}>Threads{runningThreads ? <span className={styles.tabCount} data-state="working">{runningThreads} running</span> : null}</button>
      </div>
      <div className={styles.phoneBody}>{phoneTab === "board" ? boardView : openThreadId || about || draft ? conversation : <>{threadsRail}{conversation}</>}</div>
    </> : <div className={styles.body}>{threadsRail}{conversation}{boardView}</div>}
  </div>;
}

function BoardCard({ item, clock, expanded, approval, deciding, note, onToggle, onDecide, onStop, onAsk, onOpen, showProject }: {
  item: ButlerBoardItem;
  clock: number;
  expanded: boolean;
  approval?: { id: string; value?: Approval; error?: string };
  deciding: boolean;
  note?: string;
  onToggle: () => void;
  onDecide: (decision: "approve" | "reject") => void;
  onStop: () => void;
  onAsk: () => void;
  onOpen: () => void;
  showProject: boolean;
}) {
  const elapsed = boardElapsed(item.state === "idle" || item.state === "failed" ? item.updatedAt : item.turnStartedAt ?? item.updatedAt, clock);
  const runtime = [showProject ? item.project.name : undefined, item.model, item.isSubagent ? "Subagent" : undefined].filter(Boolean).join(" · ");
  const goal = item.goal ? `Goal ${Math.min(item.goal.currentStep + 1, Math.max(item.goal.planLength, 1))}/${Math.max(item.goal.planLength, 1)} · ${item.goal.status}` : undefined;
  const pendingApproval = item.attention?.kind === "approval" && approval?.id === item.attention.actionId ? approval : undefined;
  return <article className={styles.card} data-state={item.state} data-expanded={expanded ? "true" : "false"} aria-label={`${item.title}: ${STATE_LABEL[item.state]}`}>
    <button type="button" className={styles.cardSummary} aria-expanded={expanded} onClick={onToggle}>
      <span className={styles.cardHead}><i className={styles.dot} data-state={item.state} aria-hidden="true" /><span className={styles.cardTitle}>{item.title}</span>{elapsed && <span className={styles.muted}>{elapsed}</span>}</span>
      {runtime && <span className={styles.cardMeta}>{runtime}</span>}
      {item.now && <span className={styles.cardNow}>{item.now}</span>}
      {goal && !expanded && <span className={styles.cardMeta}>{goal}</span>}
    </button>
    {expanded && <div className={styles.cardDetail}>
      {item.goal && <p className={styles.cardMeta}>{goal} · {item.goal.objective}</p>}
      {item.attention?.kind === "approval" && <div className={styles.approval}>
        <strong>Approval needed</strong>
        {pendingApproval?.error && <span className={styles.errorText}>{pendingApproval.error}</span>}
        {pendingApproval?.value ? <pre className={styles.command}>{approvalDetail(pendingApproval.value)}</pre> : !pendingApproval?.error && <span className={styles.muted}>Reading the request…</span>}
        {pendingApproval?.value && <span className={styles.muted}>{pendingApproval.value.policy_rationale}</span>}
        <div className={styles.actions}>
          <button type="button" className={styles.primary} disabled={deciding || !pendingApproval?.value} onClick={() => onDecide("approve")}>Approve</button>
          <button type="button" className={styles.secondary} disabled={deciding || !pendingApproval?.value} onClick={() => onDecide("reject")}>Deny</button>
        </div>
      </div>}
      {item.attention && item.attention.kind !== "approval" && <p className={styles.note}>{item.attention.kind === "question" ? "The chat asked a question. Open it to answer." : "Open the chat to retry or stop the interrupted response."}</p>}
      {note && <p className={styles.note} role="status">{note}</p>}
      <div className={styles.actions}>
        <button type="button" className={styles.secondary} onClick={onOpen}>Open chat <ExternalLink size={13} aria-hidden="true" /></button>
        <button type="button" className={styles.secondary} onClick={onAsk}>Ask Butler about this</button>
        {item.state === "working" && item.turnId && <button type="button" className={styles.secondary} onClick={onStop}>Stop</button>}
      </div>
    </div>}
  </article>;
}
