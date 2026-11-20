import { lazy, Suspense, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type FormEvent, type KeyboardEvent } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { ArrowLeft, ArrowUp, ChevronDown, ChevronRight, ExternalLink, MoreHorizontal, Plus, Search, SlidersHorizontal, X } from "lucide-react";
import type { ApiClient } from "../api/client";
import { boardElapsed, cancelProposedGoalChat, goalChatProposalFromReceipt, startProposedGoalChat, type GoalChatProposal, getButlerBoardIfChanged, groupButlerBoard, listButlerThreadsIfChanged, type ButlerBoard, type ButlerBoardGrouping, type ButlerBoardItem, type ButlerThread } from "../api/butlerBoard";
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
// Already in the main bundle with the Butler panel.
import { MicrophoneIcon } from "../components/NebulaVoice";
import { ButlerOversight } from "../components/ButlerOversight";
import { ButlerMemory } from "../components/ButlerMemory";
import { useButlerPanelBridge } from "../state/ButlerPanelBridge";
import { useOptionalWorkbenchDrafts } from "../state/WorkbenchDraftContext";
import type { ButlerBrief } from "../api/butlerBrief";
// Loaded when first opened, so the board itself stays as light as before.
const ButlerChatBrief = lazy(() => import("./ButlerChatBrief").then((module) => ({ default: module.ButlerChatBrief })));
const ButlerCodeSection = lazy(() => import("./ButlerCodeSection").then((module) => ({ default: module.ButlerCodeSection })));
const ButlerChatPopup = lazy(() => import("./ButlerChatPopup").then((module) => ({ default: module.ButlerChatPopup })));

type Approval = Awaited<ReturnType<ApiClient["getApproval"]>>;
type LiveTurn = { prompt: string; text: string; browserAction?: boolean; error?: string; done?: boolean };

const GROUPING_KEY = "nebula.butler-mode.grouping";
const SUBAGENTS_KEY = "nebula.butler-mode.subagents";
const STATE_LABEL = { waiting: "Needs you", working: "Working", failed: "Failed", idle: "Idle" } as const;
const THREAD_STATE_LABEL = { waiting: "Needs you", working: "Working", idle: "Answered" } as const;

type Section = "board" | "code" | "oversight" | "memory" | "settings";
const SECTIONS: Array<{ id: Section; label: string }> = [
  { id: "board", label: "Board" },
  { id: "code", label: "Code & files" },
  { id: "oversight", label: "Oversight" },
  { id: "memory", label: "Memory" },
  { id: "settings", label: "Settings" },
];

function isSection(value: string | null): value is Section {
  return SECTIONS.some((item) => item.id === value);
}

/** A board item for a chat known only from its brief (an idle chat, say). */
function itemFromBrief(brief: ButlerBrief): ButlerBoardItem {
  return { sessionId: brief.sessionId, title: brief.title, project: brief.project, state: brief.state, backend: brief.backend, model: brief.model, isSubagent: Boolean(brief.parentSessionId), updatedAt: brief.updatedAt, now: brief.attention?.summary };
}

/** Selected text added to Butler, sent ahead of the operator's words. */
function withSelectedContext(prompt: string, drafts: Array<{ text: string; source: { label: string }; truncated?: boolean }>): string {
  if (!drafts.length) return prompt;
  const blocks = drafts.map((draft) => `Context from ${draft.source.label}${draft.truncated ? " (shortened)" : ""}:\n"""\n${draft.text}\n"""`);
  return `${blocks.join("\n\n")}\n\n${prompt}`;
}

function chatPath(projectId: string, sessionId: string, turnId?: string): string {
  return `/projects/${encodeURIComponent(projectId)}/workbench?view=chat&session=${encodeURIComponent(sessionId)}${turnId ? `&turn=${encodeURIComponent(turnId)}` : ""}`;
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
  const { api, coreState, engagement, engagements, providers, harnesses, selectEngagement } = useWorkspace();
  const bridge = useButlerPanelBridge();
  const drafts = useOptionalWorkbenchDrafts();
  const butlerDrafts = drafts?.butlerDrafts ?? [];
  const navigate = useNavigate();
  const confirm = useConfirmation();
  const compact = useCompactLayout();
  const [params, setParams] = useSearchParams();
  const openThreadId = params.get("thread") ?? undefined;
  const phoneTab = params.get("tab") === "threads" ? "threads" : "board";
  const sectionParam = params.get("section");
  const section: Section = isSection(sectionParam) ? sectionParam : "board";
  // The chat whose brief is open, a project's ongoing Butler thread, and a file to open in Code & files.
  const briefId = params.get("chat") ?? undefined;
  const projectThreadId = params.get("pthread") ?? undefined;
  const openFilePath = params.get("file") ?? undefined;
  const [moreOpen, setMoreOpen] = useState(false);
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
  // Cards open the chat brief; the inline expansion stays for its approval controls.
  const [expanded] = useState<string>();
  const [approval] = useState<{ id: string; value?: Approval; error?: string }>();
  const [deciding, setDeciding] = useState<string>();
  const [actionNote, setActionNote] = useState<Record<string, string>>({});

  const [threads, setThreads] = useState<ButlerThread[]>([]);
  const [threadsError, setThreadsError] = useState("");
  const [messages, setMessages] = useState<Record<string, ChatMessage[]>>({});
  const [threadError, setThreadError] = useState<Record<string, string>>({});
  const [live, setLive] = useState<Record<string, LiveTurn>>({});
  // Goal chats Butler proposed or started in a thread on this device; the board keeps them durably.
  const [threadProposals, setThreadProposals] = useState<Record<string, GoalChatProposal[]>>({});
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
      next.delete("pthread");
      return next;
    }, { replace: false });
    setTarget(sessionId ? "reply" : "new");
  }, [setParams]);

  const setPhoneTab = (tab: "board" | "threads") => setParams((current) => { const next = new URLSearchParams(current); next.set("tab", tab); if (tab === "board") next.delete("thread"); return next; });

  const updateParams = useCallback((change: (next: URLSearchParams) => void) => setParams((current) => { const next = new URLSearchParams(current); change(next); return next; }), [setParams]);
  const setSection = (value: Section) => { setMoreOpen(false); updateParams((next) => {
    if (value === "board") next.delete("section"); else next.set("section", value);
    next.delete("chat");
    if (value !== "code") next.delete("file");
    // Settings borrows the Butler panel, which can show one place at a time.
    if (value === "settings") next.delete("pthread");
    next.set("tab", "board");
  }); };
  const openBrief = useCallback((sessionId: string) => updateParams((next) => { next.set("chat", sessionId); next.delete("section"); next.delete("file"); next.set("tab", "board"); }), [updateParams]);
  const closeBrief = useCallback(() => updateParams((next) => next.delete("chat")), [updateParams]);
  const openProjectThread = (projectId: string) => {
    selectEngagement(projectId);
    updateParams((next) => { next.set("pthread", projectId); next.delete("thread"); if (next.get("section") === "settings") next.delete("section"); next.set("tab", "threads"); });
  };
  const openFile = (path: string, projectId: string) => {
    selectEngagement(projectId);
    updateParams((next) => { next.set("section", "code"); next.set("file", path); next.delete("chat"); next.set("tab", "board"); });
  };

  // A project thread and Butler's settings belong to the selected project.
  useEffect(() => { if (projectThreadId && engagement?.id !== projectThreadId && engagements.some((item) => item.id === projectThreadId)) selectEngagement(projectThreadId); }, [projectThreadId, engagement?.id, engagements, selectEngagement]);

  // Lend the Butler panel a place: Settings shows Butler's own settings; a project thread shows its conversation.
  const [chatHost, setChatHost] = useState<HTMLElement | null>(null);
  const [settingsHost, setSettingsHost] = useState<HTMLElement | null>(null);
  const embedPanel = bridge?.embed;
  useLayoutEffect(() => {
    if (!embedPanel) return;
    if (section === "settings" && settingsHost) embedPanel({ host: settingsHost, view: "more" });
    else if (projectThreadId && chatHost) embedPanel({ host: chatHost, view: "chat" });
    else embedPanel(undefined);
  }, [embedPanel, section, settingsHost, projectThreadId, chatHost]);
  useEffect(() => () => embedPanel?.(undefined), [embedPanel]);

  const exit = () => navigate(originProject ? `/projects/${encodeURIComponent(originProject)}/workbench` : "/");

  useEffect(() => {
    const onKey = (event: globalThis.KeyboardEvent) => {
      if (event.key !== "Escape" || event.defaultPrevented) return;
      if (organizeOpen) { setOrganizeOpen(false); return; }
      if (moreOpen) { setMoreOpen(false); return; }
      const active = document.activeElement;
      if (active instanceof HTMLElement && active.closest("[role=dialog]")) return;
      if (briefId) { closeBrief(); return; }
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

  // A chat's transcript opens over Butler mode; clicking outside returns to the board.
  const [popup, setPopup] = useState<{ sessionId: string; title: string; projectId: string }>();
  const openPopup = (sessionId: string, title: string, projectId: string) => setPopup({ sessionId, title, projectId });

  const decideAction = async (sessionId: string, actionId: string, decision: "approve" | "reject") => {
    if (!api) return;
    setDeciding(sessionId);
    try {
      await api.decideApproval(actionId, { decision });
      setActionNote((current) => ({ ...current, [sessionId]: decision === "approve" ? "Approval sent. Waiting for Core to resume the chat…" : "Denied. Waiting for Core to update the chat…" }));
      boardEtag.current = undefined;
      boardPoll.current?.poke();
    } catch (caught) {
      void logCaughtDiagnostic("interface.butler_mode.approval_failed", "Butler mode could not record an approval decision.", caught, "butler_mode");
      setActionNote((current) => ({ ...current, [sessionId]: `${caught instanceof Error ? caught.message : "Core could not record the decision."} Retry, or open the chat.` }));
    } finally {
      setDeciding(undefined);
    }
  };

  const [proposalBusy, setProposalBusy] = useState<string>();
  const startProposal = async (sessionId: string) => {
    if (!api) return;
    setProposalBusy(sessionId);
    try {
      await startProposedGoalChat(api, sessionId);
      setActionNote((current) => ({ ...current, [sessionId]: "Started. Butler watches it and brings it back when it needs you." }));
      setThreadProposals((current) => Object.fromEntries(Object.entries(current).map(([key, list]) => [key, list.map((item) => item.sessionId === sessionId ? { ...item, kind: "goal_chat_started" as const } : item)])));
    } catch (caught) {
      void logCaughtDiagnostic("interface.butler_mode.proposal_start_failed", "Butler mode could not start a proposed goal chat.", caught, "butler_mode");
      setActionNote((current) => ({ ...current, [sessionId]: `${caught instanceof Error ? caught.message : "Core could not start the chat."} Retry, or ask Butler to change it.` }));
    } finally {
      setProposalBusy(undefined);
      boardEtag.current = undefined;
      boardPoll.current?.poke();
    }
  };
  const cancelProposal = async (sessionId: string, title: string) => {
    if (!api) return;
    const approved = await confirm({ title: `Cancel "${title}"?`, message: "Core removes the proposed chat and its goal. Nothing has run yet.", confirmLabel: "Cancel proposal", tone: "danger" });
    if (!approved) return;
    setProposalBusy(sessionId);
    try {
      await cancelProposedGoalChat(api, sessionId, allItemsRef.current.find((item) => item.sessionId === sessionId)?.sessionRevision);
      setThreadProposals((current) => Object.fromEntries(Object.entries(current).map(([key, list]) => [key, list.filter((item) => item.sessionId !== sessionId)])));
      if (briefIdRef.current === sessionId) closeBrief();
    } catch (caught) {
      void logCaughtDiagnostic("interface.butler_mode.proposal_cancel_failed", "Butler mode could not cancel a proposed goal chat.", caught, "butler_mode");
      setActionNote((current) => ({ ...current, [sessionId]: `${caught instanceof Error ? caught.message : "Core could not cancel the proposal."} Reload the board and retry.` }));
    } finally {
      setProposalBusy(undefined);
      boardEtag.current = undefined;
      boardPoll.current?.poke();
    }
  };
  const editProposal = (title: string) => {
    if (projectThreadId) updateParams((next) => next.delete("pthread"));
    setTarget(openThreadId ? "reply" : "new");
    setDraft(`Change the proposed goal chat "${title}": `);
    if (compact) setPhoneTab("threads");
    window.setTimeout(() => composerRef.current?.focus(), 0);
  };
  const proposalActions = (sessionId: string, title: string) => <>
    <button type="button" className={styles.primary} disabled={proposalBusy === sessionId} onClick={() => void startProposal(sessionId)}>Start chat</button>
    <button type="button" className={styles.secondary} disabled={proposalBusy === sessionId} onClick={() => editProposal(title)}>Edit</button>
    <button type="button" className={styles.secondary} disabled={proposalBusy === sessionId} onClick={() => void cancelProposal(sessionId, title)}>Cancel</button>
  </>;
  const allItemsRef = useRef<ButlerBoardItem[]>([]);
  const briefIdRef = useRef(briefId);
  briefIdRef.current = briefId;

  const askAbout = (item: ButlerBoardItem) => {
    if (projectThreadId) updateParams((next) => next.delete("pthread"));
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
        const proposal = event.type === "tool_completed" ? goalChatProposalFromReceipt(event.receipt) : undefined;
        if (proposal) {
          setThreadProposals((current) => ({ ...current, [sessionId]: [...(current[sessionId] ?? []).filter((item) => item.sessionId !== proposal.sessionId), proposal] }));
          boardEtag.current = undefined;
          boardPoll.current?.poke();
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
      const sentDrafts = [...butlerDrafts];
      const content = withSelectedContext(replyThread ? prompt : butlerQuestionAbout(about?.project.id === projectId ? about : undefined, prompt), sentDrafts);
      const sessionId = replyThread?.sessionId ?? (await api.getOrCreateControlAssistantSession(butlerThreadReservation(projectId, runtime, butlerThreadTitle(prompt)))).id;
      setDraft("");
      setAbout(undefined);
      if (sentDrafts.length) drafts?.consumeButlerDrafts(sentDrafts);
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
        {open && <div className={styles.cards}>{group.items.map((item) => <BoardCard key={item.sessionId} item={item} clock={clock} expanded={expanded === item.sessionId} approval={approval} deciding={deciding === item.sessionId} note={actionNote[item.sessionId]} onToggle={() => openBrief(item.sessionId)} onDecide={(decision) => void decide(item, decision)} onStop={() => void stopChat(item)} onAsk={() => askAbout(item)} onOpen={() => openPopup(item.sessionId, item.title, item.project.id)} showProject={grouping !== "project"} />)}</div>}
      </div>;
    })}
    {counts && counts.idle > 0 && <div className={styles.group}>
      <button type="button" className={styles.groupHead} aria-expanded={idle.open} onClick={() => idle.open ? setIdle((current) => ({ ...current, open: false })) : void loadIdle(0)}>
        {idle.open ? <ChevronDown size={14} aria-hidden="true" /> : <ChevronRight size={14} aria-hidden="true" />}
        <span className={styles.groupLabel}>Idle</span><span className={styles.muted}>{counts.idle.toLocaleString()}{idle.open ? "" : " · hidden"}</span>
      </button>
      {idle.open && <>
        {idle.error && <div className={styles.error} role="alert">{idle.error} <button type="button" className={styles.link} onClick={() => void loadIdle(idle.items.length)}>Retry</button></div>}
        <div className={styles.cards}>{idle.items.map((item) => <BoardCard key={item.sessionId} item={item} clock={clock} expanded={expanded === item.sessionId} approval={approval} deciding={false} onToggle={() => openBrief(item.sessionId)} onDecide={() => undefined} onStop={() => undefined} onAsk={() => askAbout(item)} onOpen={() => openPopup(item.sessionId, item.title, item.project.id)} showProject />)}</div>
        {idle.loading && <p className={styles.muted}>Loading idle chats…</p>}
        {idle.nextOffset !== undefined && !idle.loading && <button type="button" className={styles.link} onClick={() => void loadIdle(idle.nextOffset!)}>Show more idle chats</button>}
      </>}
    </div>}
  </section>;

  const projectCounts = useMemo(() => {
    const counts = new Map<string, { waiting: number; working: number }>();
    for (const item of board?.items ?? []) {
      const entry = counts.get(item.project.id) ?? { waiting: 0, working: 0 };
      if (item.state === "waiting") entry.waiting += 1;
      if (item.state === "working") entry.working += 1;
      counts.set(item.project.id, entry);
    }
    return counts;
  }, [board]);

  const threadsRail = <nav className={styles.rail} aria-label="Butler threads">
    <div className={styles.railHead}><h2>Projects</h2><span className={styles.muted}>one ongoing thread each</span></div>
    <ul className={styles.threadList} aria-label="Project threads">{visibleProjects.map((project) => {
      const counts = projectCounts.get(project.id);
      const state = counts?.waiting ? "waiting" : counts?.working ? "working" : "done";
      return <li key={project.id}>
        <button type="button" className={styles.thread} aria-current={projectThreadId === project.id ? "true" : undefined} onClick={() => openProjectThread(project.id)}>
          <span className={styles.threadHead}><i className={styles.dot} data-state={state} aria-hidden="true" /><span className={styles.threadTitle}>{project.name}</span></span>
          <span className={styles.muted}>{counts?.waiting ? `${counts.waiting} need you` : counts?.working ? `${counts.working} working` : "Quiet"}</span>
        </button>
      </li>;
    })}</ul>
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
      {openThreadId && threadProposals[openThreadId]?.map((proposal) => <ProposalCard key={proposal.sessionId} proposal={proposal} note={actionNote[proposal.sessionId]} actions={proposal.kind === "goal_chat_proposal" ? proposalActions(proposal.sessionId, proposal.title) : null} onOpenBrief={() => openBrief(proposal.sessionId)} />)}
      {openLive?.browserAction && <p className={styles.note}>Butler asked this device to finish a step in the browser (for example a picker or setting). Open the Butler panel to complete it there.</p>}
      {openLive?.error && <div className={styles.error} role="alert">{openLive.error}</div>}
      {openThreadSummary && !openLive && openThreadSummary.state !== "idle" && <p className={styles.working}><i className={styles.dot} data-state={openThreadSummary.state} aria-hidden="true" /> {openThreadSummary.state === "waiting" ? "Butler's answer is waiting for you. Open its conversation to approve or answer." : "Butler is working on this thread…"}</p>}
    </div>
    <form className={styles.composer} onSubmit={(event) => void send(event)}>
      {butlerDrafts.length > 0 && <div className={styles.contextPack} role="group" aria-label="Selected context for Butler">
        {butlerDrafts.map((item, index) => <span key={`${item.source.kind}:${item.source.id ?? ""}:${index}`} className={styles.chip} title={item.text.slice(0, 400)}>{item.source.label}{item.truncated ? " · shortened" : ""}<button type="button" className={styles.chipClose} aria-label={`Remove ${item.source.label} from Butler context`} title="Remove" onClick={() => drafts?.removeButlerDraft(index)}><X size={12} aria-hidden="true" /></button></span>)}
        <span className={styles.muted}>Sent with your next message</span>
      </div>}
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

  // ↑ and ↓ step through the board's chats while a brief is open.
  const boardOrder = useMemo(() => [...groups.flatMap((group) => group.items), ...(idle.open ? idle.items : [])].map((item) => item.sessionId), [groups, idle]);
  useEffect(() => {
    if (!briefId) return;
    const onKey = (event: globalThis.KeyboardEvent) => {
      if ((event.key !== "ArrowUp" && event.key !== "ArrowDown") || event.defaultPrevented || popup) return;
      const target = event.target;
      if (target instanceof HTMLElement && (target.closest("input, textarea, select, [contenteditable=true], [role=log]"))) return;
      const index = boardOrder.indexOf(briefId);
      const next = boardOrder[index + (event.key === "ArrowDown" ? 1 : -1)];
      if (index < 0 || !next) return;
      event.preventDefault();
      updateParams((params) => params.set("chat", next));
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [briefId, boardOrder, popup, updateParams]);

  const allItems = [...(board?.items ?? []), ...idle.items];
  allItemsRef.current = allItems;
  const briefItem = briefId ? allItems.find((item) => item.sessionId === briefId) : undefined;
  const briefIndex = briefId ? boardOrder.indexOf(briefId) : -1;
  const scopeProject = engagement?.id ?? originProject;
  const loading = <p className={styles.muted} role="status">Loading…</p>;

  const brief = briefId && api ? <Suspense fallback={<section className={styles.board}>{loading}</section>}><ButlerChatBrief api={api} sessionId={briefId} turnId={briefItem?.turnId}
    position={briefIndex >= 0 ? `${briefIndex + 1} of ${boardOrder.length}` : undefined}
    onBack={closeBrief}
    onAsk={(value) => askAbout(briefItem ?? itemFromBrief(value))}
    onOpenChat={(projectId) => openPopup(briefId, briefItem?.title ?? "Chat", projectId)}
    onOpenTurn={(projectId) => openPopup(briefId, briefItem?.title ?? "Chat", projectId)}
    onOpenFile={openFile}
    onDecide={(actionId, decision) => void decideAction(briefId, actionId, decision)}
    deciding={deciding === briefId}
    note={actionNote[briefId]}
    onStop={briefItem?.turnId ? () => void stopChat(briefItem) : undefined}
    proposalActions={briefItem?.attention?.kind === "proposal" ? proposalActions(briefId, briefItem.title) : undefined} /></Suspense> : null;

  const projectPicker = <label className={styles.projectPick}><span className={styles.muted}>Project</span>
    <select value={scopeProject ?? ""} onChange={(event) => selectEngagement(event.target.value)} aria-label="Project">
      {!scopeProject && <option value="" disabled>Choose a project</option>}
      {visibleProjects.map((project) => <option key={project.id} value={project.id}>{project.name}</option>)}
    </select>
  </label>;
  const noProject = <p className={styles.empty}>Create or restore a project to use this.</p>;

  const workspace = section === "oversight" ? <section className={styles.pane} aria-label="Oversight">
      <div className={styles.paneHead}><h2>Oversight</h2>{projectPicker}</div>
      {scopeProject ? <ButlerOversight key={scopeProject} api={api} projectId={scopeProject} coreState={coreState} onNavigate={(path) => navigate(path)} /> : noProject}
    </section>
    : section === "memory" ? <section className={styles.pane} aria-label="Memory">
      <div className={styles.paneHead}><h2>Memory</h2>{projectPicker}</div>
      {scopeProject ? <ButlerMemory api={api} projectId={scopeProject} coreState={coreState} /> : noProject}
    </section>
    : section === "settings" ? <section className={styles.pane} aria-label="Butler settings for this project">
      <div className={styles.paneHead}><h2>Settings</h2>{projectPicker}</div>
      {scopeProject ? <div ref={setSettingsHost} className={styles.embedHost} /> : noProject}
      {!bridge && <p className={styles.muted}>Butler's settings are unavailable here.</p>}
    </section>
    : brief ?? boardView;

  const projectThread = projectThreadId ? <section className={styles.conversation} aria-label={`Project thread: ${visibleProjects.find((item) => item.id === projectThreadId)?.name ?? "project"}`}>
    <div className={styles.threadBar}>
      {compact && <button type="button" className={styles.iconButton} aria-label="Back to threads" title="Back to threads" onClick={() => updateParams((next) => next.delete("pthread"))}><ArrowLeft size={16} aria-hidden="true" /></button>}
      <h2>{visibleProjects.find((item) => item.id === projectThreadId)?.name ?? "Project"}</h2>
      <span className={styles.muted}>project thread</span>
    </div>
    {engagement?.id === projectThreadId ? <div ref={setChatHost} className={styles.embedHost} /> : loading}
  </section> : null;

  const codeSection = api && scopeProject ? <Suspense fallback={loading}><ButlerCodeSection api={api} projectId={scopeProject} openPath={openFilePath}
    onOpenBrief={openBrief}
    onAskAbout={(path) => { setDraft((current) => current || `About ${path}: `); updateParams((next) => { next.delete("section"); next.delete("pthread"); next.set("tab", "threads"); }); window.setTimeout(() => composerRef.current?.focus(), 0); }} /></Suspense> : noProject;

  const sectionTabs = <nav className={styles.sections} aria-label="Butler sections">
    {SECTIONS.map((item) => <button key={item.id} type="button" className={styles.sectionTab} aria-current={section === item.id ? "page" : undefined} onClick={() => setSection(item.id)}>
      {item.label}
      {item.id === "board" && counts?.waiting ? <span className={styles.badge} data-state="waiting">{counts.waiting}</span> : null}
      {item.id === "oversight" && bridge?.attentionCount ? <span className={styles.badge} data-state="failed">{bridge.attentionCount}</span> : null}
    </button>)}
  </nav>;

  const leftColumn = projectThread ?? conversation;
  const phoneThreads = projectThread ?? (openThreadId || about || draft ? conversation : <>{threadsRail}{conversation}</>);

  return <div className={styles.mode} data-compact={compact ? "true" : "false"} data-selection-recipient="butler">
    <header className={styles.topBar}>
      <h1>Butler</h1>
      {!compact && sectionTabs}
      {section === "board" && <label className={styles.search}>
        <Search size={15} aria-hidden="true" />
        <input type="search" value={search} onChange={(event) => setSearch(event.target.value)} placeholder="Search chats and projects" aria-label="Search chats and projects" />
      </label>}
      {section !== "board" && <span className={styles.spacer} />}
      {counts && !compact && <div className={styles.pulse} aria-label="Chat status across projects">
        <span><i className={styles.dot} data-state="waiting" aria-hidden="true" />{counts.waiting} need you</span>
        <span><i className={styles.dot} data-state="working" aria-hidden="true" />{counts.working} working</span>
        {counts.failed > 0 && <span><i className={styles.dot} data-state="failed" aria-hidden="true" />{counts.failed} failed</span>}
      </div>}
      {bridge && <button type="button" className={styles.iconButton} aria-label="Start Butler Voice" title="Start Butler Voice" disabled={!bridge.voiceAvailable} onClick={bridge.startVoice}><MicrophoneIcon size={17} /></button>}
      <button type="button" className={styles.iconButton} aria-label="Exit Butler mode" title="Exit Butler mode (Esc)" onClick={exit}><X size={17} aria-hidden="true" /></button>
    </header>
    {coreState !== "online" && <div className={styles.error} role="alert">Core is unavailable. Butler mode reconnects and reads the board again when Core answers.</div>}
    {compact ? <>
      <div className={styles.tabs} role="tablist" aria-label="Butler mode">
        <button type="button" role="tab" aria-selected={phoneTab === "board" && section === "board"} onClick={() => setSection("board")}>Board{counts?.waiting ? <span className={styles.tabCount} data-state="waiting">{counts.waiting}</span> : null}</button>
        <button type="button" role="tab" aria-selected={phoneTab === "threads"} onClick={() => setPhoneTab("threads")}>Threads{runningThreads ? <span className={styles.tabCount} data-state="working">{runningThreads}</span> : null}</button>
        <button type="button" role="tab" aria-selected={phoneTab === "board" && section === "oversight"} onClick={() => setSection("oversight")}>Oversight{bridge?.attentionCount ? <span className={styles.tabCount} data-state="failed">{bridge.attentionCount}</span> : null}</button>
        <button type="button" role="tab" aria-selected={phoneTab === "board" && (section === "code" || section === "memory" || section === "settings")} aria-haspopup="dialog" aria-expanded={moreOpen} onClick={() => setMoreOpen(true)}><MoreHorizontal size={16} aria-hidden="true" /> More</button>
      </div>
      <div className={styles.phoneBody}>{phoneTab === "threads" ? phoneThreads : section === "code" ? codeSection : workspace}</div>
      {moreOpen && <div className={styles.sheetLayer}>
        <button type="button" className={styles.sheetScrim} aria-label="Close" tabIndex={-1} onClick={() => setMoreOpen(false)} />
        <div className={styles.sheet} role="dialog" aria-label="More in Butler">
          {(["code", "memory", "settings"] as const).map((value) => <button key={value} type="button" className={styles.sheetRow} onClick={() => setSection(value)}>{SECTIONS.find((item) => item.id === value)?.label}<ChevronRight size={16} aria-hidden="true" /></button>)}
          <button type="button" className={styles.sheetRow} onClick={exit}>Open full Nebula<ChevronRight size={16} aria-hidden="true" /></button>
        </div>
      </div>}
    </> : section === "code" ? <div className={styles.codeBody}>{codeSection}</div> : <div className={styles.body}>{threadsRail}{leftColumn}{workspace}</div>}
    {popup && api && <Suspense fallback={null}><ButlerChatPopup api={api} sessionId={popup.sessionId} title={popup.title}
      live={allItems.some((item) => item.sessionId === popup.sessionId && (item.state === "working" || item.state === "waiting"))}
      onClose={() => setPopup(undefined)}
      onOpenFull={() => { const target = popup; setPopup(undefined); navigate(chatPath(target.projectId, target.sessionId)); }} /></Suspense>}
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
  const runtime = [showProject ? item.project.name : undefined, item.model, item.isSubagent ? "Subagent" : undefined, item.proposedBy ? "Proposed by Butler" : item.startedBy ? "Started by Butler" : undefined].filter(Boolean).join(" · ");
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

/** A goal chat Butler proposed (Start / Edit / Cancel) or started on its own authority. */
function ProposalCard({ proposal, note, actions, onOpenBrief }: { proposal: GoalChatProposal; note?: string; actions: React.ReactNode; onOpenBrief: () => void }) {
  const runtime = [proposal.runtime?.model, proposal.runtime?.reasoningEffort ? `${proposal.runtime.reasoningEffort} effort` : undefined].filter(Boolean).join(" · ");
  const limits = proposal.limits ? [proposal.limits.timeBudgetSeconds ? `${Math.round(proposal.limits.timeBudgetSeconds / 60)} min` : undefined, proposal.limits.tokenBudget ? `${proposal.limits.tokenBudget.toLocaleString()} tokens` : undefined, proposal.limits.stepBudget ? `${proposal.limits.stepBudget} steps` : undefined, proposal.limits.allowSubagents ? `up to ${proposal.limits.maxActiveSubagents ?? "unlimited"} subagents` : undefined].filter(Boolean).join(" · ") : "";
  const rows: Array<[string, string]> = [
    ["Goal", proposal.objective],
    ["Done when", proposal.completionCriteria.join("; ")],
    ["Runtime", runtime],
    ["Tools", proposal.tools.join(" · ")],
    ["Limits", limits],
    ["Reports to", proposal.reportsTo?.title ?? ""],
  ];
  return <article className={styles.proposal} data-kind={proposal.kind} aria-label={`${proposal.kind === "goal_chat_started" ? "Started" : "Proposed"} goal chat: ${proposal.title}`}>
    <header><i className={styles.dot} data-state={proposal.kind === "goal_chat_started" ? "working" : "waiting"} aria-hidden="true" /><strong>{proposal.title}</strong><span className={styles.muted}>{proposal.kind === "goal_chat_started" ? "Started by Butler" : "Proposed"}</span></header>
    <dl>{rows.filter(([, value]) => value).map(([label, value]) => <div key={label}><dt>{label}</dt><dd>{value}</dd></div>)}</dl>
    {proposal.startError && <p className={styles.errorText}>{proposal.startError}</p>}
    {note && <p className={styles.note} role="status">{note}</p>}
    <div className={styles.actions}>{actions}<button type="button" className={styles.link} onClick={onOpenBrief}>Open brief</button></div>
  </article>;
}
