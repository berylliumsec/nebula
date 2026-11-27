import { lazy, Suspense, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type FormEvent, type KeyboardEvent } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { ArrowDown, ArrowLeft, ArrowUp, ChevronDown, ChevronUp, Columns3, Square, ChevronRight, ExternalLink, ListTodo, MoreHorizontal, PanelLeft, PanelRight, Pause, Play, Plus, Search, SlidersHorizontal, Workflow, Send, X } from "lucide-react";
import type { ApiClient } from "../api/client";
import { butlerCaseStage } from "../butlerStage";
import { chatStageName } from "./workStages";
import { ButlerStageLabel } from "../components/ButlerStageLabel";
import { WorkStageLabel } from "../components/WorkStageLabel";
import { ALL_PROJECTS, NO_STAGE_FILTER, parseStageFilter, stageFilterActive, stageFilterOptions, stageFilterSummary, toggleStageFilter, type BoardStageFilter, SECTION_LABEL, VIEW_SECTION_LABEL, holdAllActive, holdBoardChats, isHoldable, releaseBoardHolds, type BoardViewSection, type PlacedHold, boardElapsed, boardSectionOpen, boardStatusLabel, cancelProposedGoalChat, isResumableIdle, listResumableIdle, resumeIdleGoals, goalChatProposalFromReceipt, parseBoardSectionState, sectionOfGoalStatus, startProposedGoalChat, toggleBoardSection, type BoardSectionOpenState, type GoalChatProposal, getButlerBoardIfChanged, groupButlerBoard, operatorActionCheckLabel, resolveOperatorAction, type OperatorActionRequest, listButlerThreadsIfChanged, type ButlerBoard, type ButlerBoardGroup, type ButlerBoardGrouping, type ButlerBoardItem, type ButlerBoardSectionGroup, type ButlerThread } from "../api/butlerBoard";
import type { ChatStreamEvent, PersistedChatMessage } from "../api/types";
import { ActivityLedger } from "../components/ActivityLedger";
import { activityLedgerFromNative } from "../components/activityLedgerModel";
import { activityFromEvent, activityFromPendingTurn, activityFromSavedResults, ledgerStatus, type ThreadActivity } from "./butlerThreadActivity";
import { startVisiblePoll } from "../api/visiblePoll";
import { useConfirmation } from "../components/DialogSystem";
import { logCaughtDiagnostic } from "../diagnostics";
import { ShellPushSettings } from "../components/ShellPushNotifications";
import { isNebulaShell, useCompactLayout } from "../hooks/useCompactLayout";
import { readStorage, writeStorage } from "../state/browserStorage";
import { useWorkspace } from "../state/WorkspaceContext";
import { askAboutTarget, butlerQuestionAbout, butlerThreadRequest, butlerThreadReservation, butlerThreadRuntime, butlerThreadTitle, filterBoardItems, projectPickerGroups } from "./butlerThreads";
import { ButlerProjectPicker } from "./ButlerProjectPicker";
import styles from "./ButlerModePage.module.css";
import { ButlerMarkdown } from "../components/ButlerMarkdown";
import { useButlerColumns } from "./useButlerColumns";
import { useChatQueue } from "./useChatQueue";
import { QueuedMessages } from "../components/QueuedMessages";
import { isSendNowShortcut, sendNowShortcutLabel } from "../components/composerShortcuts";
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
// Which board sections are open on this device ("<project or all>:<section>").
const SECTIONS_KEY = "nebula.butler-mode.sections";
const STAGE_FILTER_KEY = "nebula.butler-mode.stage-filter";
// Which desktop side columns the operator hid, leaving the Butler thread on its own.
const HIDDEN_PANELS_KEY = "nebula.butler-mode.hidden-panels";
type HiddenPanels = { rail: boolean; board: boolean };
function parseHiddenPanels(value: string | null): HiddenPanels {
  try {
    const parsed = JSON.parse(value ?? "{}") as Partial<HiddenPanels>;
    return { rail: parsed.rail === true, board: parsed.board === true };
  } catch {
    // diagnostic-expected: an unreadable saved layout shows both columns.
    return { rail: false, board: false };
  }
}
// How far above the newest message the thread may sit before Jump to latest appears.
const LATEST_SLACK_PX = 80;
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
  return { sessionId: brief.sessionId, title: brief.title, project: brief.project, section: sectionOfGoalStatus(brief.goal?.status), state: brief.state, backend: brief.backend, model: brief.model, isSubagent: Boolean(brief.parentSessionId), updatedAt: brief.updatedAt, now: brief.attention?.summary };
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
  const { api, coreState, engagement, engagements, providers, harnesses, selectEngagement, reconnect } = useWorkspace();
  const bridge = useButlerPanelBridge();
  const drafts = useOptionalWorkbenchDrafts();
  const butlerDrafts = drafts?.butlerDrafts ?? [];
  const navigate = useNavigate();
  const confirm = useConfirmation();
  // The iPhone app has no full Nebula behind Butler mode: nothing to exit to, and chats open as popups.
  const nativeShell = isNebulaShell();
  const compact = useCompactLayout();
  const columns = useButlerColumns(!compact, styles.columnHandle);
  const [hidden, setHiddenState] = useState<HiddenPanels>(() => parseHiddenPanels(readStorage(HIDDEN_PANELS_KEY)));
  const setHidden = useCallback((update: (prior: HiddenPanels) => HiddenPanels) => setHiddenState((prior) => {
    const next = update(prior);
    writeStorage(HIDDEN_PANELS_KEY, JSON.stringify(next));
    return next;
  }), []);
  const railHidden = !compact && hidden.rail;
  const boardHidden = !compact && hidden.board;
  const messagesRef = useRef<HTMLDivElement | null>(null);
  const [awayFromLatest, setAwayFromLatest] = useState(false);
  const measureLatest = useCallback(() => {
    const element = messagesRef.current;
    setAwayFromLatest(Boolean(element && element.scrollHeight - element.scrollTop - element.clientHeight > LATEST_SLACK_PX));
  }, []);
  const jumpToLatest = () => {
    const element = messagesRef.current;
    if (element) element.scrollTo({ top: element.scrollHeight });
  };
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
  const [grouping, setGrouping] = useState<ButlerBoardGrouping>(() => readStorage(GROUPING_KEY) === "attention" ? "attention" : "project");
  const [organizeOpen, setOrganizeOpen] = useState(false);
  const [stageFilter, setStageFilterState] = useState<BoardStageFilter>(() => parseStageFilter(readStorage(STAGE_FILTER_KEY)));
  const [stageOpen, setStageOpen] = useState(false);
  const setStageFilter = (next: BoardStageFilter) => { setStageFilterState(next); writeStorage(STAGE_FILTER_KEY, JSON.stringify(next)); };
  const filteringStages = stageFilterActive(stageFilter);
  // One key for effects: the poll and pages restart when the filter changes.
  const stageKey = JSON.stringify(stageFilter);
  const stages = useMemo(() => JSON.parse(stageKey) as BoardStageFilter, [stageKey]);
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set());
  const [sectionOpen, setSectionOpen] = useState<BoardSectionOpenState>(() => parseBoardSectionState(readStorage(SECTIONS_KEY)));
  // Completed goal chats load on request, by project ID or ALL_PROJECTS; ``count`` is Core's count they were read at.
  const [completed, setCompleted] = useState<Record<string, { items: ButlerBoardItem[]; nextOffset?: number; loading: boolean; error?: string; count: number }>>({});
  // Cards open the chat brief; the inline expansion stays for its approval controls.
  const [expanded] = useState<string>();
  const [approval] = useState<{ id: string; value?: Approval; error?: string }>();
  const [deciding, setDeciding] = useState<string>();
  const [actionNote, setActionNote] = useState<Record<string, string>>({});
  // Idle goals ticked for resuming, on this device only; Select all remembers what it ticked per group.
  const [selected, setSelected] = useState<Map<string, ButlerBoardItem>>(new Map());
  const [selectedAll, setSelectedAll] = useState<Record<string, string[]>>({});
  const [selecting, setSelecting] = useState<string>();
  const [resumeRun, setResumeRun] = useState<{ done: number; total: number }>();
  const [resumeSummary, setResumeSummary] = useState<{ text: string; failed: boolean }>();
  // Chats just held, for Undo; holding never asks first because release restores exactly what was held.
  const [heldUndo, setHeldUndo] = useState<{ placed: PlacedHold[]; count: number }>();
  const [holdBusy, setHoldBusy] = useState(false);
  const [resolvingRequest, setResolvingRequest] = useState<string>();
  const [requestError, setRequestError] = useState("");

  const [threads, setThreads] = useState<ButlerThread[]>([]);
  const [threadsError, setThreadsError] = useState("");
  const [messages, setMessages] = useState<Record<string, PersistedChatMessage[]>>({});
  // Each thread's live steps: the same Activity card a normal chat shows.
  const [activity, setActivity] = useState<Record<string, ThreadActivity>>({});
  const [threadError, setThreadError] = useState<Record<string, string>>({});
  const [live, setLive] = useState<Record<string, LiveTurn>>({});
  // Goal chats Butler proposed or started in a thread on this device; the board keeps them durably.
  const [threadProposals, setThreadProposals] = useState<Record<string, GoalChatProposal[]>>({});
  const [draft, setDraft] = useState("");
  const [target, setTarget] = useState<"new" | "reply">("new");
  const [about, setAbout] = useState<ButlerBoardItem>();
  // "Ask about" fell back to a new thread only because this open thread was
  // still answering; once it settles, the question goes to it instead.
  const [aboutWaitsFor, setAboutWaitsFor] = useState<string>();
  // A question just started this open thread: the composer starts another
  // thread while Butler answers, then replies in this one once it settles.
  const [startedWaitsFor, setStartedWaitsFor] = useState<string>();
  // The thread whose composer target was last chosen; a different ?thread=
  // (reload, deep link, Back/Forward) opens in Reply.
  const targetThread = useRef<string | undefined>(undefined);
  // The operator's pick for new threads; until then they start in the origin project.
  const [projectChoice, setNewThreadProject] = useState<string>();
  const newThreadProject = projectChoice ?? originProject;
  const [composerError, setComposerError] = useState("");
  const [starting, setStarting] = useState(false);
  // The message being sent while Core reads Butler's settings and reserves its thread: shown at once.
  const [pendingSend, setPendingSend] = useState<{ prompt: string; threadId?: string }>();
  const [stopping, setStopping] = useState(false);

  const boardEtag = useRef<string | undefined>(undefined);
  const threadsEtag = useRef<string | undefined>(undefined);
  const boardPoll = useRef<{ poke: () => void } | undefined>(undefined);
  const threadsPoll = useRef<{ poke: () => void } | undefined>(undefined);
  const streams = useRef(new Map<string, AbortController>());
  const previousThreadState = useRef(new Map<string, string>());
  const composerRef = useRef<HTMLTextAreaElement>(null);
  // The open thread's follow-ups: replies typed while Butler answers it. Core
  // sends them in order, even after this page closes.
  const threadQueue = useChatQueue(api, openThreadId && typeof api?.requestIfChanged === "function" ? openThreadId : "");
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
          const answer = await getButlerBoardIfChanged(api, { q: query, stages }, boardEtag.current, signal);
          setBoardError("");
          if (!answer) return "idle";
          boardEtag.current = answer.etag;
          setBoard(answer.board);
          return answer.board.states?.working || answer.board.states?.waiting ? "active" : "idle";
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
  }, [api, coreState, query, stages]);
  // A new stage filter drops the old board, so no card or bulk action from
  // the previous filter is shown or acted on until Core answers.
  const firstStageKey = useRef(stageKey);
  useEffect(() => { if (firstStageKey.current !== stageKey) { firstStageKey.current = stageKey; setBoard(undefined); } }, [stageKey]);

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

  // A thread working without this device's stream (a reload, another device,
  // or a turn Core resumed after a restart): read its steps from Core.
  const openThreadWorking = threads.some((item) => item.sessionId === openThreadId && item.state !== "idle");
  useEffect(() => {
    if (!api || !openThreadId || !openThreadWorking) return;
    const sessionId = openThreadId;
    const controller = new AbortController();
    let seenTurn: string | undefined;
    startVisiblePoll({
      intervalMs: 3000,
      maxIntervalMs: 15_000,
      signal: controller.signal,
      read: async (signal) => {
        if (streams.current.has(sessionId)) return "idle";
        try {
          const turn = await api.getPendingChatTurn(sessionId, signal);
          // Core started another response, such as a queued reply: show the
          // message it answers.
          if (turn && seenTurn !== undefined && turn.id !== seenTurn) void loadMessages(sessionId);
          if (turn) seenTurn = turn.id;
          setActivity((current) => {
            if (!turn) { if (!current[sessionId]) return current; const next = { ...current }; delete next[sessionId]; return next; }
            return { ...current, [sessionId]: activityFromPendingTurn(turn) };
          });
          return turn ? "active" : "stop";
        } catch (caught) {
          if (signal.aborted) return "stop";
          void logCaughtDiagnostic("interface.butler_mode.thread_activity_failed", "Butler mode could not read a thread's steps.", caught, "butler_mode");
          return "idle";
        }
      },
    });
    return () => controller.abort();
  }, [api, openThreadId, openThreadWorking, loadMessages]);

  // Core moved one of the open thread's queued replies (sent it, or settled
  // it): read the thread again. A quick answer can start and finish between
  // two reads of the thread list, so its state alone may never change.
  const queueSignature = threadQueue.queue?.items.map((item) => `${item.id}:${item.status}:${item.turn_id ?? ""}`).join("|") ?? "";
  const seenQueue = useRef({ sessionId: "", signature: "" });
  useEffect(() => {
    if (!openThreadId || !threadQueue.queue) return;
    const seen = seenQueue.current;
    seenQueue.current = { sessionId: openThreadId, signature: queueSignature };
    if (seen.sessionId !== openThreadId || seen.signature === queueSignature || streams.current.has(openThreadId)) return;
    void loadMessages(openThreadId);
    threadsEtag.current = undefined;
    threadsPoll.current?.poke();
  }, [openThreadId, queueSignature, threadQueue.queue, loadMessages]);

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
    targetThread.current = sessionId;
    setTarget(sessionId ? "reply" : "new");
    setAboutWaitsFor(undefined);
    setStartedWaitsFor(undefined);
  }, [setParams]);

  useEffect(() => {
    if (openThreadId === targetThread.current) return;
    targetThread.current = openThreadId;
    setTarget(openThreadId ? "reply" : "new");
    setAboutWaitsFor(undefined);
    setStartedWaitsFor(undefined);
  }, [openThreadId]);

  const setPhoneTab = (tab: "board" | "threads") => setParams((current) => { const next = new URLSearchParams(current); next.set("tab", tab); if (tab === "board") next.delete("thread"); return next; });

  const updateParams = useCallback((change: (next: URLSearchParams) => void) => setParams((current) => { const next = new URLSearchParams(current); change(next); return next; }), [setParams]);
  const setSection = (value: Section) => { setMoreOpen(false); if (value !== "code") setHidden((prior) => prior.board ? { ...prior, board: false } : prior); updateParams((next) => {
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
      if (stageOpen) { setStageOpen(false); return; }
      if (moreOpen) { setMoreOpen(false); return; }
      const active = document.activeElement;
      if (active instanceof HTMLElement && active.closest("[role=dialog]")) return;
      if (briefId) { closeBrief(); return; }
      if (!nativeShell) exit();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  });

  // The search filters loaded cards as the operator types; Core's search (debounced) then fills in the rest.
  const searchedItems = useMemo(() => filterBoardItems(board?.items ?? [], search), [board, search]);
  const searchedCompleted = useMemo(() => Object.fromEntries(Object.entries(completed).map(([key, page]) => [key, filterBoardItems(page.items, search)])), [completed, search]);
  // Core's counts hold once its search caught up with the box; until then the matched cards count.
  const coreCounts = search.trim() === query.trim();
  const groups = useMemo(() => groupButlerBoard(searchedItems, grouping, { projects: coreCounts ? board?.projects : [], completed: searchedCompleted, coreCounts }), [searchedItems, grouping, board, searchedCompleted, coreCounts]);
  const searching = search.trim().length > 0;
  const matchedChats = groups.reduce((sum, group) => sum + group.total, 0);
  const matchedProjects = coreCounts && board ? board.projects.length : new Set(searchedItems.map((item) => item.project.id)).size;
  const stageOptions = useMemo(() => stageFilterOptions(board, stageFilter), [board, stageFilter]);
  const stageTotal = board ? board.counts.active + board.counts.idle + board.counts.completed : 0;
  const openThreadSummary = threads.find((item) => item.sessionId === openThreadId);
  // Archive or delete the open thread. Core keeps an archived thread in the
  // project's archived chats; Butler mode lists only unarchived threads.
  const [confirmDelete, setConfirmDelete] = useState<string>();
  const [threadAction, setThreadAction] = useState<{ sessionId: string; busy?: "archive" | "delete"; error?: string }>();
  // Threads ticked in the list for Archive or Delete together.
  const [threadPicks, setThreadPicks] = useState<Set<string>>(() => new Set());
  const [bulkBusy, setBulkBusy] = useState<"archive" | "delete">();
  const [bulkConfirm, setBulkConfirm] = useState(false);
  const [bulkNote, setBulkNote] = useState<{ text: string; failed: boolean }>();
  useEffect(() => { setConfirmDelete(undefined); setThreadAction(undefined); }, [openThreadId]);
  // Archives or deletes one thread in Core and drops it from Butler mode; throws when Core refuses.
  const applyThreadRemoval = async (client: ApiClient, sessionId: string, action: "archive" | "delete") => {
    if (action === "archive") await client.setChatSessionArchived(sessionId, true);
    else await client.deleteChatSession(sessionId);
    setThreads((current) => current.filter((item) => item.sessionId !== sessionId));
    setMessages((current) => { const next = { ...current }; delete next[sessionId]; return next; });
    setThreadPicks((current) => { if (!current.has(sessionId)) return current; const next = new Set(current); next.delete(sessionId); return next; });
    if (openThreadId === sessionId) openThread(undefined);
  };
  const removeThread = async (sessionId: string, action: "archive" | "delete") => {
    if (!api) return;
    setThreadAction({ sessionId, busy: action });
    try {
      await applyThreadRemoval(api, sessionId, action);
      setConfirmDelete(undefined);
      setThreadAction(undefined);
      threadsEtag.current = undefined;
      threadsPoll.current?.poke();
    } catch (caught) {
      void logCaughtDiagnostic(action === "archive" ? "interface.butler_mode.thread_archive_failed" : "interface.butler_mode.thread_delete_failed", action === "archive" ? "Butler mode could not archive a thread." : "Butler mode could not delete a thread.", caught, "butler_mode");
      setThreadAction({ sessionId, error: `${caught instanceof Error ? caught.message : `Core could not ${action} this thread.`} The thread is unchanged.` });
    }
  };
  const runningThreads = threads.filter((item) => item.state === "working").length;
  const threadRunning = (thread: ButlerThread) => thread.state === "working" || streams.current.has(thread.sessionId) || Boolean(live[thread.sessionId] && !live[thread.sessionId].error);
  const pickableThreads = threads.filter((thread) => !threadRunning(thread));
  const pickedThreads = pickableThreads.filter((thread) => threadPicks.has(thread.sessionId));
  const toggleThreadPick = (sessionId: string) => {
    setBulkNote(undefined);
    setBulkConfirm(false);
    setThreadPicks((current) => { const next = new Set(current); if (next.has(sessionId)) next.delete(sessionId); else next.add(sessionId); return next; });
  };
  // One request at a time so each thread reports its own result; the rest still run when one fails.
  const removePickedThreads = async (action: "archive" | "delete") => {
    if (!api || bulkBusy || pickedThreads.length === 0) return;
    setBulkBusy(action);
    setBulkNote(undefined);
    let done = 0;
    const failures: string[] = [];
    for (const thread of pickedThreads) {
      try {
        await applyThreadRemoval(api, thread.sessionId, action);
        done += 1;
      } catch (caught) {
        void logCaughtDiagnostic(action === "archive" ? "interface.butler_mode.thread_archive_failed" : "interface.butler_mode.thread_delete_failed", action === "archive" ? "Butler mode could not archive a selected thread." : "Butler mode could not delete a selected thread.", caught, "butler_mode");
        failures.push(`${thread.title}: ${caught instanceof Error ? caught.message : "Core refused"}`);
      }
    }
    const verb = action === "archive" ? "Archived" : "Deleted";
    setBulkNote(failures.length
      ? { failed: true, text: `${verb} ${done} of ${done + failures.length}. Unchanged — ${failures.join("; ")}` }
      : { failed: false, text: `${verb} ${done} ${done === 1 ? "thread" : "threads"}.` });
    setBulkConfirm(false);
    setBulkBusy(undefined);
    threadsEtag.current = undefined;
    threadsPoll.current?.poke();
  };
  const visibleProjects = useMemo(() => engagements.filter((item) => item.status !== "archived"), [engagements]);

  const toggleGroup = (key: string) => setCollapsed((current) => { const next = new Set(current); if (next.has(key)) next.delete(key); else next.add(key); return next; });

  const toggleSection = (groupKey: string, value: BoardViewSection) => {
    const next = toggleBoardSection(sectionOpen, groupKey, value);
    setSectionOpen(next);
    writeStorage(SECTIONS_KEY, JSON.stringify(next));
  };

  // Core's exact Completed count for a project, or across projects.
  const coreCompleted = useCallback((scope: string) => scope === ALL_PROJECTS ? board?.counts.completed ?? 0 : board?.projects.find((item) => item.id === scope)?.counts.completed ?? 0, [board]);
  // Completed pages belong to one search and stage filter; a page for another is dropped.
  const readKey = `${query}\u0000${stageKey}`;
  const queryRef = useRef(readKey);
  queryRef.current = readKey;
  const loadCompleted = useCallback(async (scope: string, offset: number, count: number) => {
    if (!api) return;
    const asked = readKey;
    setCompleted((current) => ({ ...current, [scope]: { items: current[scope]?.items ?? [], nextOffset: current[scope]?.nextOffset, loading: true, count } }));
    try {
      const answer = await getButlerBoardIfChanged(api, { q: query, stages, section: "completed", projectId: scope === ALL_PROJECTS ? undefined : scope, offset, limit: 50 }, undefined);
      if (queryRef.current !== asked) return;
      const page = answer?.board;
      setCompleted((current) => ({ ...current, [scope]: { loading: false, count, items: offset ? [...(current[scope]?.items ?? []), ...(page?.items ?? [])] : page?.items ?? [], nextOffset: page?.nextOffset } }));
    } catch (caught) {
      if (queryRef.current !== asked) return;
      void logCaughtDiagnostic("interface.butler_mode.completed_failed", "Butler mode could not list completed goal chats.", caught, "butler_mode");
      setCompleted((current) => ({ ...current, [scope]: { items: current[scope]?.items ?? [], nextOffset: current[scope]?.nextOffset, loading: false, count, error: caught instanceof Error ? caught.message : "Core could not list completed goal chats." } }));
    }
  }, [api, query, stages, readKey]);
  useEffect(() => { setCompleted({}); }, [readKey]);
  // An open Completed section reads its first page, and again when Core's count changes.
  useEffect(() => {
    for (const group of groups) {
      if (!boardSectionOpen(sectionOpen, group.key, "completed")) continue;
      const count = coreCompleted(group.key);
      const page = completed[group.key];
      if (!count || page?.loading || page?.count === count) continue;
      void loadCompleted(group.key, 0, count);
    }
  }, [groups, sectionOpen, completed, coreCompleted, loadCompleted]);

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

  const toggleSelected = (item: ButlerBoardItem) => setSelected((current) => {
    const next = new Map(current);
    if (next.has(item.sessionId)) next.delete(item.sessionId); else next.set(item.sessionId, item);
    return next;
  });
  const clearSelected = () => { setSelected(new Map()); setSelectedAll({}); };
  const allSelectedIn = (groupKey: string) => (selectedAll[groupKey]?.length ?? 0) > 0 && selectedAll[groupKey].every((id) => selected.has(id));
  // Select all reads Core's full idle list for the group, not only the cards the poll returned.
  const toggleSelectAll = async (groupKey: string) => {
    if (!api) return;
    if (allSelectedIn(groupKey)) {
      const ids = new Set(selectedAll[groupKey]);
      setSelected((current) => new Map([...current].filter(([id]) => !ids.has(id))));
      setSelectedAll((current) => { const next = { ...current }; delete next[groupKey]; return next; });
      return;
    }
    setSelecting(groupKey);
    try {
      const items = await listResumableIdle(api, { q: query, stages, projectId: groupKey === ALL_PROJECTS ? undefined : groupKey });
      setSelected((current) => { const next = new Map(current); for (const item of items) next.set(item.sessionId, item); return next; });
      setSelectedAll((current) => ({ ...current, [groupKey]: items.map((item) => item.sessionId) }));
      if (!items.length) setResumeSummary({ text: "No paused or blocked goals to select here.", failed: false });
    } catch (caught) {
      void logCaughtDiagnostic("interface.butler_mode.resume_list_failed", "Butler mode could not list idle goal chats to select.", caught, "butler_mode");
      setResumeSummary({ text: `${caught instanceof Error ? caught.message : "Core could not list idle goal chats."} Retry Select all.`, failed: true });
    } finally {
      setSelecting(undefined);
    }
  };
  // Resume goals one Core action each; a goal Core refuses keeps its reason on its card.
  const runResume = async (items: ButlerBoardItem[]) => {
    if (!api || !items.length || resumeRun) return;
    setResumeSummary(undefined);
    setResumeRun({ done: 0, total: items.length });
    const outcomes = await resumeIdleGoals(api, items, (done) => setResumeRun({ done, total: items.length }));
    const failed = outcomes.filter((outcome) => outcome.error);
    const resumed = new Set(outcomes.filter((outcome) => !outcome.error).map((outcome) => outcome.sessionId));
    setActionNote((current) => {
      const next = { ...current };
      for (const id of resumed) delete next[id];
      for (const outcome of failed) next[outcome.sessionId] = `Not resumed: ${outcome.error}`;
      return next;
    });
    setSelected((current) => new Map([...current].filter(([id]) => !resumed.has(id))));
    if (failed.length) void logCaughtDiagnostic("interface.butler_mode.resume_refused", "Core did not resume some idle goal chats.", new Error(`${failed.length} of ${items.length} goals not resumed: ${failed[0].error}`), "butler_mode");
    setResumeSummary(failed.length
      ? { text: `Resumed ${resumed.size} of ${items.length}. ${failed.length} stayed idle; ${failed.length === 1 ? "its card says" : "their cards say"} why.`, failed: true }
      : { text: `Resumed ${resumed.size} ${resumed.size === 1 ? "goal" : "goals"}.`, failed: false });
    setResumeRun(undefined);
    boardEtag.current = undefined;
    boardPoll.current?.poke();
  };
  const resumeAllIdle = async () => {
    if (!api || resumeRun) return;
    setSelecting(ALL_PROJECTS);
    let items: ButlerBoardItem[];
    try {
      items = await listResumableIdle(api, { q: query, stages });
    } catch (caught) {
      void logCaughtDiagnostic("interface.butler_mode.resume_list_failed", "Butler mode could not list idle goal chats to resume.", caught, "butler_mode");
      setResumeSummary({ text: `${caught instanceof Error ? caught.message : "Core could not list idle goal chats."} Retry Resume all idle.`, failed: true });
      return;
    } finally {
      setSelecting(undefined);
    }
    if (!items.length) { setResumeSummary({ text: query.trim() || filteringStages ? `No paused or blocked goals match the ${query.trim() ? "search" : ""}${query.trim() && filteringStages ? " and " : ""}${filteringStages ? "stage filter" : ""}.` : "No paused or blocked goals to resume.", failed: false }); return; }
    const projectCount = new Set(items.map((item) => item.project.id)).size;
    const goals = `${items.length} paused or blocked ${items.length === 1 ? "goal" : "goals"}`;
    const approved = await confirm({
      title: `Resume ${items.length} idle ${items.length === 1 ? "goal" : "goals"}?`,
      message: `${goals}${query.trim() ? ` matching “${query.trim()}”` : ""}${filteringStages ? ` in stage ${stageFilterSummary(stageFilter)}` : ""}${projectCount > 1 ? ` in ${projectCount} projects` : ""} start working again and use their budgets.${filteringStages ? " Goals in other stages are not resumed." : ""} Proposed goals are not started.`,
      confirmLabel: `Resume ${items.length}`,
    });
    if (approved) await runResume(items);
  };
  const refreshBoard = () => { boardEtag.current = undefined; boardPoll.current?.poke(); };
  const resolveRequest = async (request: OperatorActionRequest, outcome: "done" | "cancelled") => {
    if (!api || resolvingRequest) return;
    setResolvingRequest(request.id);
    setRequestError("");
    try {
      await resolveOperatorAction(api, request, outcome);
      // Leave the card at once; the next poll confirms it.
      setBoard((current) => current && { ...current, operatorActions: current.operatorActions.filter((item) => item.id !== request.id) });
      refreshBoard();
    } catch (caught) {
      void logCaughtDiagnostic("interface.butler_mode.operator_action_failed", "Butler mode could not close an operator action request.", caught, "butler_mode");
      setRequestError(`${caught instanceof Error ? caught.message : "Core could not save that."} The request is still open.`);
    } finally {
      setResolvingRequest(undefined);
    }
  };
  const heldCount = (placed: PlacedHold[]) => placed.reduce((sum, entry) => sum + entry.sessionIds.length, 0);
  const finishHold = (placed: PlacedHold[], failed: Array<{ error: string }>) => {
    const count = heldCount(placed);
    const held = new Set(placed.flatMap((entry) => entry.sessionIds));
    setSelected((current) => new Map([...current].filter(([id]) => !held.has(id))));
    setHeldUndo(count ? { placed, count } : undefined);
    if (failed.length) {
      void logCaughtDiagnostic("interface.butler_mode.hold_refused", "Core did not hold some goal chats.", new Error(failed[0].error), "butler_mode");
      setResumeSummary({ text: `${count ? `Held ${count}. ` : ""}${failed[0].error}`, failed: true });
    } else if (!count) setResumeSummary({ text: "Nothing was running to hold.", failed: false });
    refreshBoard();
  };
  // Hold acts at once: Undo or Release resumes exactly the goals each hold paused.
  const runHold = async (items: ButlerBoardItem[]) => {
    const chosen = items.filter(isHoldable);
    if (!api || !chosen.length || holdBusy) return;
    setHoldBusy(true); setResumeSummary(undefined);
    try {
      const { placed, failed } = await holdBoardChats(api, chosen);
      finishHold(placed, failed);
    } finally { setHoldBusy(false); }
  };
  // With a stage filter, Hold acts only on the filtered Active chats Core
  // listed, named one by one; otherwise each project holds all its running work.
  const holdEveryActive = async () => {
    if (!api || !board || holdBusy) return;
    if (filteringStages) {
      const chosen = board.items.filter((item) => item.section === "active" && isHoldable(item));
      if (!chosen.length) { setResumeSummary({ text: "Nothing running in this stage filter to hold.", failed: false }); return; }
      setHoldBusy(true); setResumeSummary(undefined);
      try {
        const { placed, failed } = await holdBoardChats(api, chosen);
        finishHold(placed, failed);
      } finally { setHoldBusy(false); }
      return;
    }
    const projects = board.projects.filter((project) => project.counts.active > 0).map((project) => project.id);
    if (!projects.length) return;
    setHoldBusy(true); setResumeSummary(undefined);
    try {
      const { placed, failed } = await holdAllActive(api, projects);
      finishHold(placed, failed);
    } finally { setHoldBusy(false); }
  };
  const release = async (releases: Array<{ projectId: string; batchId?: string; sessionIds?: string[] }>, count: number) => {
    if (!api || holdBusy || !releases.length) return;
    setHoldBusy(true); setResumeSummary(undefined);
    try {
      await releaseBoardHolds(api, releases);
      setHeldUndo(undefined);
      setResumeSummary({ text: `Released ${count} ${count === 1 ? "chat" : "chats"}; ${count === 1 ? "its goal resumes" : "their goals resume"} from saved progress.`, failed: false });
    } catch (caught) {
      void logCaughtDiagnostic("interface.butler_mode.release_failed", "Butler mode could not release held goal chats.", caught, "butler_mode");
      setResumeSummary({ text: `${caught instanceof Error ? caught.message : "Core could not release the chats."} Retry Release.`, failed: true });
    } finally { setHoldBusy(false); refreshBoard(); }
  };
  const releaseItems = (items: ButlerBoardItem[]) => void release(
    [...new Map(items.map((item) => [`${item.project.id}:${item.hold?.batchId ?? item.sessionId}`, item.hold?.batchId
      ? { projectId: item.project.id, batchId: item.hold.batchId }
      : { projectId: item.project.id, sessionIds: [item.sessionId] }])).values()],
    items.length,
  );
  const undoHold = () => heldUndo && void release(heldUndo.placed.map((entry) => ({ projectId: entry.projectId, batchId: entry.batchId, sessionIds: entry.batchId ? undefined : entry.sessionIds })), heldUndo.count);
  useEffect(() => {
    if (!heldUndo) return;
    const timer = window.setTimeout(() => setHeldUndo(undefined), 15_000);
    return () => window.clearTimeout(timer);
  }, [heldUndo]);
  const setPriority = async (item: ButlerBoardItem, high: boolean) => {
    if (!api) return;
    try {
      await api.setChatSessionPriority(item.sessionId, { priority: high ? "high" : "normal" }, item.sessionRevision);
      setActionNote((current) => { const next = { ...current }; delete next[item.sessionId]; return next; });
    } catch (caught) {
      void logCaughtDiagnostic("interface.butler_mode.priority_failed", "Butler mode could not change a chat's priority.", caught, "butler_mode");
      setActionNote((current) => ({ ...current, [item.sessionId]: `${caught instanceof Error ? caught.message : "Core could not change the priority."} Retry.` }));
    }
    refreshBoard();
  };
  useEffect(() => {
    if (!selected.size) return;
    const onKey = (event: globalThis.KeyboardEvent) => { if (event.key === "Escape" && !event.defaultPrevented && !resumeRun) clearSelected(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [selected.size, resumeRun]);

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
    setStartedWaitsFor(undefined);
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

  useEffect(() => {
    if (!aboutWaitsFor) return;
    if (!about || openThreadId !== aboutWaitsFor) { setAboutWaitsFor(undefined); return; }
    const open = threads.find((thread) => thread.sessionId === aboutWaitsFor);
    if (!open) return;
    const answering = streams.current.has(open.sessionId) || Boolean(live[open.sessionId] && !live[open.sessionId].error);
    if (askAboutTarget(open, about, answering) !== "reply") return;
    setTarget("reply");
    setAboutWaitsFor(undefined);
  }, [about, aboutWaitsFor, live, openThreadId, threads]);

  useEffect(() => {
    if (!startedWaitsFor) return;
    // The address can trail the thread just opened by a render.
    if (targetThread.current !== startedWaitsFor) { setStartedWaitsFor(undefined); return; }
    if (openThreadId !== startedWaitsFor) return;
    const open = threads.find((thread) => thread.sessionId === startedWaitsFor);
    if (!open || open.state !== "idle" || streams.current.has(open.sessionId) || (live[open.sessionId] && !live[open.sessionId].error)) return;
    // A question typed meanwhile was meant for a new thread; leave it there.
    if (draft.trim()) return;
    setTarget("reply");
    setStartedWaitsFor(undefined);
  }, [draft, live, openThreadId, startedWaitsFor, threads]);

  const askAbout = (item: ButlerBoardItem) => {
    if (projectThreadId) updateParams((next) => next.delete("pthread"));
    setAbout(item);
    setStartedWaitsFor(undefined);
    // Quote into the open thread when it can take it; otherwise start one.
    const open = threads.find((thread) => thread.sessionId === openThreadId);
    const answering = Boolean(open && (streams.current.has(open.sessionId) || (live[open.sessionId] && !live[open.sessionId].error)));
    const next = askAboutTarget(open, item, answering);
    setTarget(next);
    setAboutWaitsFor(open && next === "new" && open.project.id === item.project.id ? open.sessionId : undefined);
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
        setActivity((current) => { const next = activityFromEvent(current[sessionId], event); return next === current[sessionId] || !next ? current : { ...current, [sessionId]: next }; });
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
      // The saved answer carries the finished steps.
      setActivity((current) => { if (!current[sessionId]) return current; const next = { ...current }; delete next[sessionId]; return next; });
      setLive((current) => { if (current[sessionId]?.error) return current; const next = { ...current }; delete next[sessionId]; return next; });
      threadsEtag.current = undefined;
      threadsPoll.current?.poke();
    }
  };

  /**
   * A reply to a thread Butler is still answering waits in Core's queue for
   * that thread. Send now puts it first and stops the current answer; Core
   * then sends it, even if this page has closed.
   */
  /** The runtime Butler answers a conversation with, from its saved settings. */
  const readThreadRuntime = async (sessionId: string | undefined) => {
    const config = sessionId && api ? (await api.getChatAssistantConfig(sessionId)).settings : undefined;
    return butlerThreadRuntime(config, providers, harnesses);
  };

  const queueReply = async (thread: ButlerThread, prompt: string, interrupt: boolean) => {
    if (!api) return;
    setStarting(true);
    const sentAbout = about;
    const sentDrafts = [...butlerDrafts];
    setDraft("");
    setAbout(undefined);
    const restore = (message: string) => {
      setComposerError(message);
      setDraft((typed) => typed.trim() ? `${prompt}\n\n${typed}` : prompt);
      setAbout((current) => current ?? sentAbout);
    };
    try {
      // A follow-up must match the thread's own runtime, so read the thread's settings.
      const runtime = await readThreadRuntime(thread.sessionId);
      if (!runtime.ok) { restore(runtime.error); return; }
      const content = withSelectedContext(butlerQuestionAbout(sentAbout?.project.id === thread.project.id ? sentAbout : undefined, prompt), sentDrafts);
      if (!await threadQueue.enqueue(butlerThreadRequest(thread.project.id, thread.sessionId, runtime, harnesses, content), { interrupt })) {
        restore("Your reply was not queued. It is back in the box; retry when Core answers.");
        return;
      }
      if (sentDrafts.length) drafts?.consumeButlerDrafts(sentDrafts);
      if (interrupt) await stopOpenThread();
    } catch (caught) {
      void logCaughtDiagnostic("interface.butler_mode.reply_queue_failed", "Butler mode could not queue a reply.", caught, "butler_mode");
      restore(`${caught instanceof Error ? caught.message : "Core could not queue this reply."} Your message is back in the box.`);
    } finally {
      setStarting(false);
    }
  };

  const send = async (event?: FormEvent, interrupt = false) => {
    event?.preventDefault();
    const prompt = draft.trim();
    if (!api || !prompt || starting) return;
    setComposerError("");
    const replyThread = target === "reply" ? openThreadSummary : undefined;
    if (target === "reply" && !replyThread) { setComposerError("Choose a thread to reply to, or start a new thread."); return; }
    if (replyThread && (replyThread.state !== "idle" || streams.current.has(replyThread.sessionId) || (live[replyThread.sessionId] && !live[replyThread.sessionId].error))) { await queueReply(replyThread, prompt, interrupt); return; }
    const projectId = replyThread?.project.id ?? newThreadProject ?? engagement?.id;
    if (!projectId) { setComposerError("Choose the project Butler should work in."); return; }
    setStarting(true);
    // Show the message now; Core's settings read and thread reservation follow.
    const sentAbout = about;
    let sent = false;
    setDraft("");
    setAbout(undefined);
    if (!replyThread && openThreadId) openThread(undefined);
    setPendingSend({ prompt, threadId: replyThread?.sessionId });
    const restore = (message: string) => {
      setComposerError(message);
      // Anything typed meanwhile stays after the returned message.
      setDraft((typed) => typed.trim() ? `${prompt}\n\n${typed}` : prompt);
      setAbout((current) => current ?? sentAbout);
    };
    try {
      const current = await api.getControlAssistantSession(projectId);
      const runtime = await readThreadRuntime(current?.id);
      if (!runtime.ok) { restore(runtime.error); return; }
      const sentDrafts = [...butlerDrafts];
      // The quoted chat goes with the question whether it starts a thread or replies in one.
      const content = withSelectedContext(butlerQuestionAbout(sentAbout?.project.id === projectId ? sentAbout : undefined, prompt), sentDrafts);
      const sessionId = replyThread?.sessionId ?? (await api.getOrCreateControlAssistantSession(butlerThreadReservation(projectId, runtime, butlerThreadTitle(prompt)))).id;
      sent = true;
      if (sentDrafts.length) drafts?.consumeButlerDrafts(sentDrafts);
      if (!replyThread) {
        const project = engagements.find((item) => item.id === projectId);
        setThreads((existing) => [{ sessionId, title: butlerThreadTitle(prompt), project: { id: projectId, name: project?.name ?? "" }, state: "working", updatedAt: new Date().toISOString() }, ...existing.filter((item) => item.sessionId !== sessionId)]);
        setMessages((existing) => ({ ...existing, [sessionId]: [] }));
        openThread(sessionId);
        setTarget("new");
        setStartedWaitsFor(sessionId);
      }
      void runTurn(projectId, sessionId, prompt, content, runtime);
    } catch (caught) {
      void logCaughtDiagnostic("interface.butler_mode.thread_start_failed", "Butler mode could not start a thread.", caught, "butler_mode");
      if (!sent) restore(`${caught instanceof Error ? caught.message : "Core could not start the thread."} Your message is back in the box; retry when Core answers.`);
      else setComposerError(caught instanceof Error ? caught.message : "Butler's thread could not open.");
    } finally {
      setPendingSend(undefined);
      setStarting(false);
    }
  };

  const onComposerKey = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (isSendNowShortcut(event)) { event.preventDefault(); void send(undefined, replyQueues); return; }
    if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) { event.preventDefault(); void send(); }
  };

  const states = board?.states;
  const selectedItems = [...selected.values()];
  const selectedResumable = selectedItems.filter(isResumableIdle);
  const selectedHoldable = selectedItems.filter(isHoldable);
  const card = (item: ButlerBoardItem) => <BoardCard key={item.sessionId} item={item} clock={clock} expanded={expanded === item.sessionId} approval={approval} deciding={deciding === item.sessionId} note={actionNote[item.sessionId]} onToggle={() => openBrief(item.sessionId)} onDecide={(decision) => void decide(item, decision)} onStop={() => void stopChat(item)} onAsk={() => askAbout(item)} onOpen={() => openPopup(item.sessionId, item.title, item.project.id)} showProject={grouping !== "project"} selectable={isResumableIdle(item) || isHoldable(item)} selected={selected.has(item.sessionId)} selectDisabled={Boolean(resumeRun) || holdBusy} onSelect={() => toggleSelected(item)} onRelease={() => releaseItems([item])} onPriority={(high) => void setPriority(item, high)} releaseDisabled={holdBusy} />;
  // One section of a project (or of every project, grouped by status): Completed starts collapsed.
  const sectionView = (group: ButlerBoardGroup, entry: ButlerBoardSectionGroup) => {
    const open = boardSectionOpen(sectionOpen, group.key, entry.section);
    const page = entry.section === "completed" ? completed[group.key] : undefined;
    const groupSelected = [...selected.values()].filter((item) => item.section === entry.section && !item.hold && (group.key === ALL_PROJECTS || item.project.id === group.key)).length;
    const hold = entry.section === "held" ? entry.items.find((item) => item.hold)?.hold : undefined;
    return <div key={entry.section} className={styles.boardSection} data-section={entry.section}>
      <div className={styles.sectionBar}>
        <button type="button" className={styles.sectionHead} aria-expanded={open} onClick={() => toggleSection(group.key, entry.section)}>
          {open ? <ChevronDown size={14} aria-hidden="true" /> : <ChevronRight size={14} aria-hidden="true" />}
          <i className={styles.sectionDot} data-section={entry.section} aria-hidden="true" />
          <span className={styles.sectionLabel}>{VIEW_SECTION_LABEL[entry.section]}</span>
          <span className={styles.muted}>{entry.count.toLocaleString()}</span>
          {groupSelected > 0 && <span className={styles.muted}>· {groupSelected.toLocaleString()} selected</span>}
          {hold && <span className={styles.muted}>· {hold.reason} · {boardElapsed(hold.createdAt, clock) ?? "just now"}</span>}
        </button>
        {entry.section === "held" && open && <button type="button" className={styles.secondary} disabled={holdBusy} onClick={() => releaseItems(entry.items)}>
          <Play size={13} aria-hidden="true" /> Release all
        </button>}
        {entry.section === "idle" && open && entry.count > 0 && <label className={styles.selectAll}>
          <input type="checkbox" checked={allSelectedIn(group.key)} disabled={selecting !== undefined || Boolean(resumeRun)} onChange={() => void toggleSelectAll(group.key)} aria-label={group.label ? `Select all paused or blocked goals in ${group.label}` : "Select all paused or blocked goals"} />
          {selecting === group.key ? "Selecting…" : "Select all"}
        </label>}
      </div>
      {open && <>
        {entry.items.length > 0 && <div className={styles.cards}>{entry.items.map(card)}</div>}
        {page?.error && <div className={styles.error} role="alert">{page.error} <button type="button" className={styles.link} onClick={() => void loadCompleted(group.key, page.items.length, coreCompleted(group.key))}>Retry</button></div>}
        {page?.loading && <p className={styles.muted}>Loading completed goal chats…</p>}
        {page?.nextOffset !== undefined && !page.loading && <button type="button" className={styles.link} onClick={() => void loadCompleted(group.key, page.nextOffset!, coreCompleted(group.key))}>Show more completed</button>}
      </>}
    </div>;
  };
  const boardView = <section className={styles.board} aria-label="Status board">
    <div className={styles.boardToolbar}>
      <div className={styles.organize}>
        <button type="button" className={styles.select} aria-haspopup="dialog" aria-expanded={organizeOpen} onClick={() => setOrganizeOpen((open) => !open)}>
          <SlidersHorizontal size={14} aria-hidden="true" /> Group: {grouping === "project" ? "Project" : "Status"} <ChevronDown size={13} aria-hidden="true" />
        </button>
        {organizeOpen && <div className={styles.popover} role="dialog" aria-label="Organize the board">
          <div className={styles.popoverHeading}>Group by</div>
          {(["project", "attention"] as const).map((value) => <label key={value} className={styles.option}>
            <input type="radio" name="butler-board-grouping" checked={grouping === value} onChange={() => { setGrouping(value); writeStorage(GROUPING_KEY, value); }} />
            {value === "attention" ? "Status" : "Project"}
          </label>)}
        </div>}
      </div>
      <div className={styles.stageControl}>
        <button type="button" className={styles.select} data-active={filteringStages ? "true" : undefined} aria-haspopup="dialog" aria-expanded={stageOpen} aria-label={`Filter by stage: ${stageFilterSummary(stageFilter)}`} onClick={() => setStageOpen((open) => !open)}>
          <Workflow size={14} aria-hidden="true" /> Stage: {stageFilterSummary(stageFilter)} <ChevronDown size={13} aria-hidden="true" />
        </button>
        {stageOpen && <div className={`${styles.popover} ${styles.stagePopover}`} role="dialog" aria-label="Filter by stage">
          <div className={styles.popoverHeading}>Show stages</div>
          {stageOptions.map((option) => <label key={option.label ?? "\u0000none"} className={styles.option}>
            <input type="checkbox" checked={option.selected} onChange={() => setStageFilter(toggleStageFilter(stageFilter, option.label))} />
            <span className={styles.optionLabel}>{option.name}</span>
            <span className={styles.optionCount}>{option.count.toLocaleString()}</span>
          </label>)}
          {filteringStages && <button type="button" className={styles.link} onClick={() => { setStageFilter(NO_STAGE_FILTER); setStageOpen(false); }}>Show all stages</button>}
        </div>}
      </div>
      {filteringStages && <button type="button" className={styles.iconButton} aria-label="Clear stage filter" title="Clear stage filter" onClick={() => setStageFilter(NO_STAGE_FILTER)}><X size={14} aria-hidden="true" /></button>}
      <label className={styles.boardSearch}>
        <Search size={15} aria-hidden="true" />
        <input type="search" value={search} onChange={(event) => setSearch(event.target.value)} onKeyDown={(event) => { if (event.key === "Escape" && search) { event.preventDefault(); setSearch(""); } }} placeholder="Search projects and chats" aria-label="Search projects and chats" />
        {searching && <button type="button" className={styles.clearSearch} aria-label="Clear search" title="Clear search" onClick={() => setSearch("")}><X size={14} aria-hidden="true" /></button>}
      </label>
      {(board?.counts.active ?? 0) > 0 && <button type="button" className={styles.secondary} disabled={holdBusy || Boolean(resumeRun)} onClick={() => void holdEveryActive()}>
        <Pause size={13} aria-hidden="true" /> {holdBusy ? "Holding…" : filteringStages ? `Hold active in ${stageFilterSummary(stageFilter)}` : "Hold all active"}
      </button>}
      {(board?.counts.idle ?? 0) > 0 && <button type="button" className={styles.secondary} disabled={selecting !== undefined || Boolean(resumeRun)} onClick={() => void resumeAllIdle()}>
        <Play size={13} aria-hidden="true" /> {selecting === ALL_PROJECTS ? "Reading idle goals…" : filteringStages ? `Resume idle in ${stageFilterSummary(stageFilter)}` : "Resume all idle"}
      </button>}
      {board?.truncated && <span className={styles.muted}>Showing the first {board.items.length} goal chats; search to narrow.</span>}
    </div>
    {board && board.operatorActions.length > 0 && <OperatorActionsPanel requests={board.operatorActions} busy={resolvingRequest} error={requestError} onResolve={(request, outcome) => void resolveRequest(request, outcome)} />}
    {resumeRun && <p className={styles.resumeStatus} role="status">Resuming {resumeRun.done.toLocaleString()} of {resumeRun.total.toLocaleString()}…</p>}
    {!resumeRun && resumeSummary && <p className={styles.resumeStatus} role="status" data-failed={resumeSummary.failed ? "true" : undefined}>{resumeSummary.text} <button type="button" className={styles.link} onClick={() => setResumeSummary(undefined)}>Dismiss</button></p>}
    {board && searching && groups.length > 0 && <p className={styles.searchCount} role="status">{matchedChats} {matchedChats === 1 ? "goal chat" : "goal chats"} in {matchedProjects} {matchedProjects === 1 ? "project" : "projects"} match{search !== query ? " · searching Core…" : ""}</p>}
    {boardError && <div className={styles.error} role="alert">{boardError} The board retries on its own.</div>}
    {!board && !boardError && <p className={styles.muted}>Reading every project's goal chats…</p>}
    {board && filteringStages && <p className={styles.searchCount} role="status">Showing stage {stageFilterSummary(stageFilter)} · {stageTotal.toLocaleString()} {stageTotal === 1 ? "goal chat" : "goal chats"}. Hold and Resume act only on these. <button type="button" className={styles.link} onClick={() => setStageFilter(NO_STAGE_FILTER)}>Show all stages</button></p>}
    {board && groups.length === 0 && <p className={styles.empty}>{searching ? `No goal chats match “${search.trim()}”${filteringStages ? ` in stage ${stageFilterSummary(stageFilter)}` : ""}.` : filteringStages ? `No goal chats in stage ${stageFilterSummary(stageFilter)}.` : "No goal chats yet."}</p>}
    {groups.map((group) => {
      const sections = group.sections.filter((entry) => entry.count > 0 || entry.items.length > 0);
      if (group.label === undefined) return <div key={group.key} className={styles.group}>{sections.map((entry) => sectionView(group, entry))}</div>;
      const open = !collapsed.has(group.key);
      return <div key={group.key} className={styles.group} data-project={group.key}>
        <button type="button" className={styles.groupHead} aria-expanded={open} onClick={() => toggleGroup(group.key)}>
          {open ? <ChevronDown size={14} aria-hidden="true" /> : <ChevronRight size={14} aria-hidden="true" />}
          <span className={styles.groupLabel}>{group.label}</span>
          <span className={styles.muted}>{group.total.toLocaleString()} {group.total === 1 ? "goal" : "goals"}</span>
          <span className={styles.groupCounts}>
            {sections.map((entry) => <span key={entry.section} data-section={entry.section}><i className={styles.sectionDot} data-section={entry.section} aria-hidden="true" />{entry.count.toLocaleString()} {VIEW_SECTION_LABEL[entry.section].toLowerCase()}</span>)}
          </span>
        </button>
        {open && sections.map((entry) => sectionView(group, entry))}
      </div>;
    })}
    {selected.size > 0 && <div className={styles.selectionBar} role="region" aria-label="Selected goal chats">
      <span className={styles.selectionCount}>{selected.size.toLocaleString()} selected</span>
      <button type="button" className={styles.link} disabled={Boolean(resumeRun) || holdBusy} onClick={clearSelected}>Clear</button>
      {selectedResumable.length > 0 && <button type="button" className={selectedHoldable.length > selectedResumable.length ? styles.secondary : styles.primary} disabled={Boolean(resumeRun) || holdBusy} onClick={() => void runResume(selectedResumable)}>
        <Play size={13} aria-hidden="true" /> {resumeRun ? `Resuming ${resumeRun.done} of ${resumeRun.total}…` : `Resume ${selectedResumable.length.toLocaleString()}`}
      </button>}
      {selectedHoldable.length > 0 && <button type="button" className={selectedHoldable.length > selectedResumable.length ? styles.primary : styles.secondary} disabled={Boolean(resumeRun) || holdBusy} onClick={() => void runHold(selectedHoldable)}>
        <Pause size={13} aria-hidden="true" /> {holdBusy ? "Holding…" : `Hold ${selectedHoldable.length.toLocaleString()}`}
      </button>}
    </div>}
    {!selected.size && heldUndo && <div className={styles.selectionBar} role="status" aria-label="Held chats">
      <span className={styles.selectionCount}>Held {heldUndo.count.toLocaleString()} {heldUndo.count === 1 ? "chat" : "chats"}</span>
      <button type="button" className={styles.link} disabled={holdBusy} onClick={undoHold}>Undo</button>
      <button type="button" className={styles.iconButton} aria-label="Dismiss" title="Dismiss" onClick={() => setHeldUndo(undefined)}><X size={14} aria-hidden="true" /></button>
    </div>}
  </section>;

  // Projects with chats that need the operator or are working come first; the
  // rest stay one keystroke away in the picker instead of a thousand rows.
  const projectGroups = useMemo(() => projectPickerGroups(visibleProjects, board?.items ?? []), [visibleProjects, board]);

  const threadsRail = <nav className={styles.rail} aria-label="Butler threads">
    <div className={styles.railHead}><h2>Projects</h2><span className={styles.muted}>one ongoing thread each</span></div>
    <ButlerProjectPicker groups={projectGroups} value={projectThreadId} onSelect={openProjectThread} />
    <div className={styles.railHead}>
      <h2>Threads</h2>
      {runningThreads > 0 && <span className={styles.muted}>{runningThreads} running</span>}
      <button type="button" className={styles.newThread} onClick={() => { openThread(undefined); setTarget("new"); composerRef.current?.focus(); }}><Plus size={14} aria-hidden="true" /> New</button>
    </div>
    {threadsError && <div className={styles.error} role="alert">{threadsError}</div>}
    {threads.length === 0 && !threadsError && <p className={styles.empty}>Ask Butler something below. Each question runs as its own thread, so you can ask the next one right away.</p>}
    {pickedThreads.length > 0 && <div className={styles.threadBulk} role="group" aria-label="Selected threads">
      <span className={styles.threadBulkCount}>{pickedThreads.length} selected</span>
      {pickedThreads.length < pickableThreads.length && <button type="button" className={styles.link} disabled={Boolean(bulkBusy)} onClick={() => setThreadPicks(new Set(pickableThreads.map((thread) => thread.sessionId)))}>Select all {pickableThreads.length}</button>}
      <span className={styles.threadActions}>
        <button type="button" className={styles.threadAction} disabled={Boolean(bulkBusy)} title="Hide the selected threads from Butler mode; they stay in their projects' archived chats" onClick={() => void removePickedThreads("archive")}>{bulkBusy === "archive" ? "Archiving…" : "Archive"}</button>
        <button type="button" className={`${styles.threadAction} ${styles.threadDelete}`} disabled={Boolean(bulkBusy)} aria-expanded={bulkConfirm} onClick={() => setBulkConfirm(true)}>Delete</button>
        <button type="button" className={styles.iconButton} disabled={Boolean(bulkBusy)} aria-label="Clear selection" title="Clear selection" onClick={() => { setThreadPicks(new Set()); setBulkConfirm(false); }}><X size={15} aria-hidden="true" /></button>
      </span>
      {bulkConfirm && <div className={styles.confirmDelete} role="group" aria-label="Confirm delete">
        <p>Delete {pickedThreads.length} {pickedThreads.length === 1 ? "thread" : "threads"} and their messages? This can't be undone.</p>
        <span className={styles.threadActions}>
          <button type="button" className={styles.dangerButton} disabled={Boolean(bulkBusy)} onClick={() => void removePickedThreads("delete")}>{bulkBusy === "delete" ? "Deleting…" : `Delete ${pickedThreads.length}`}</button>
          <button type="button" className={styles.threadAction} disabled={Boolean(bulkBusy)} onClick={() => setBulkConfirm(false)}>Cancel</button>
        </span>
      </div>}
    </div>}
    {bulkNote && <p className={bulkNote.failed ? styles.error : styles.muted} role={bulkNote.failed ? "alert" : "status"}>{bulkNote.text}</p>}
    <ul className={styles.threadList}>{threads.map((thread) => {
      const state = live[thread.sessionId] && !live[thread.sessionId].error ? "working" : thread.state;
      const running = threadRunning(thread);
      return <li key={thread.sessionId} className={styles.threadRow} data-picked={threadPicks.has(thread.sessionId) && !running ? "true" : undefined}>
        <label className={styles.threadPick} title={running ? "Butler is still answering this thread" : threadPicks.has(thread.sessionId) ? "Deselect" : "Select to archive or delete"}>
          <input type="checkbox" checked={threadPicks.has(thread.sessionId) && !running} disabled={running || Boolean(bulkBusy)} onChange={() => toggleThreadPick(thread.sessionId)} aria-label={`Select ${thread.title}`} />
        </label>
        <button type="button" className={styles.thread} aria-current={thread.sessionId === openThreadId ? "true" : undefined} onClick={() => openThread(thread.sessionId)}>
          <span className={styles.threadHead}><i className={styles.dot} data-state={state === "idle" ? "done" : state} aria-hidden="true" /><span className={styles.threadTitle}>{thread.title}</span><span className={styles.threadState} data-state={state}>{THREAD_STATE_LABEL[state]}</span></span>
          <span className={styles.muted}>{thread.project.name}</span>
        </button>
      </li>;
    })}</ul>
  </nav>;

  const openLive = openThreadId ? live[openThreadId] : undefined;
  const openActivity = openThreadId ? activity[openThreadId] : undefined;
  // Replying to a thread Butler is still answering: with nothing typed the
  // button stops it; a typed reply queues, or Send now interrupts.
  const openAnswering = Boolean(openThreadSummary && (openThreadSummary.state === "working" || streams.current.has(openThreadSummary.sessionId) || (openLive && !openLive.error)));
  const replyQueues = target === "reply" && openAnswering;
  const showStop = replyQueues && !draft.trim();
  const stopOpenThread = async () => {
    if (!api || !openThreadSummary || stopping) return;
    const sessionId = openThreadSummary.sessionId;
    setStopping(true);
    setComposerError("");
    try {
      const turnId = openActivity?.turnId ?? openThreadSummary.turnId ?? (await api.getPendingChatTurn(sessionId))?.id;
      if (turnId) await api.cancelChatTurn(turnId);
      threadsEtag.current = undefined;
      threadsPoll.current?.poke();
      await loadMessages(sessionId);
    } catch (caught) {
      void logCaughtDiagnostic("interface.butler_mode.thread_stop_failed", "Butler mode could not stop a thread's response.", caught, "butler_mode");
      setComposerError(`${caught instanceof Error ? caught.message : "Core could not stop this response."} Butler may still be working; try again.`);
    } finally {
      setStopping(false);
    }
  };
  const threadMessages = openThreadId ? messages[openThreadId] ?? [] : [];
  // Content grows while Butler answers, so recheck after each render whether the newest message is in view.
  useLayoutEffect(measureLatest);
  const railToggle = compact ? null : <button type="button" className={styles.iconButton} aria-label={railHidden ? "Show threads list" : "Hide threads list"} title={railHidden ? "Show threads list" : "Hide threads list"} aria-pressed={railHidden} onClick={() => setHidden((prior) => ({ ...prior, rail: !prior.rail }))}><PanelLeft size={16} aria-hidden="true" /></button>;
  const boardToggle = compact ? null : <button type="button" className={`${styles.iconButton} ${styles.panelToggleEnd}`} aria-label={boardHidden ? "Show board" : "Hide board"} title={boardHidden ? "Show board" : "Hide board"} aria-pressed={boardHidden} onClick={() => setHidden((prior) => ({ ...prior, board: !prior.board }))}><PanelRight size={16} aria-hidden="true" /></button>;
  const conversation = <section className={styles.conversation} aria-label={openThreadSummary ? `Thread: ${openThreadSummary.title}` : "New Butler thread"}>
    <div className={styles.threadBar}>
      {compact && <button type="button" className={styles.iconButton} aria-label="Back to threads" title="Back to threads" onClick={() => openThread(undefined)}><ArrowLeft size={16} aria-hidden="true" /></button>}
      {railToggle}
      <h2>{openThreadSummary?.title ?? "New thread"}</h2>
      {openThreadSummary && <span className={styles.muted}>{openThreadSummary.project.name}</span>}
      {openThreadSummary && (() => {
        const running = openThreadSummary.state === "working" || streams.current.has(openThreadSummary.sessionId) || Boolean(live[openThreadSummary.sessionId] && !live[openThreadSummary.sessionId].error);
        const busy = threadAction?.sessionId === openThreadSummary.sessionId ? threadAction.busy : undefined;
        const why = running ? "Butler is still answering this thread. Wait for it to finish first." : undefined;
        return <span className={styles.threadActions}>
          <button type="button" className={styles.threadAction} disabled={running || Boolean(busy)} title={why ?? "Hide this thread from Butler mode; it stays in the project's archived chats"} onClick={() => void removeThread(openThreadSummary.sessionId, "archive")}>{busy === "archive" ? "Archiving…" : "Archive"}</button>
          <button type="button" className={`${styles.threadAction} ${styles.threadDelete}`} disabled={running || Boolean(busy)} title={why ?? "Delete this thread and its messages"} aria-expanded={confirmDelete === openThreadSummary.sessionId} onClick={() => setConfirmDelete(openThreadSummary.sessionId)}>Delete</button>
        </span>;
      })()}
      {boardToggle}
    </div>
    {openThreadSummary && confirmDelete === openThreadSummary.sessionId && <div className={styles.confirmDelete} role="group" aria-label="Confirm delete">
      <p>Delete this thread and its messages? This can't be undone.</p>
      <span className={styles.threadActions}>
        <button type="button" className={styles.dangerButton} disabled={threadAction?.busy === "delete"} onClick={() => void removeThread(openThreadSummary.sessionId, "delete")}>{threadAction?.busy === "delete" ? "Deleting…" : "Delete thread"}</button>
        <button type="button" className={styles.threadAction} disabled={threadAction?.busy === "delete"} onClick={() => setConfirmDelete(undefined)}>Cancel</button>
      </span>
    </div>}
    {openThreadSummary && threadAction?.sessionId === openThreadSummary.sessionId && threadAction.error && <div className={styles.error} role="alert">{threadAction.error}</div>}
    <div className={styles.messagesWrap}>
    <div ref={messagesRef} className={styles.messages} role="log" aria-label="Butler thread messages" aria-live="polite" onScroll={measureLatest}>
      {!openThreadId && <p className={styles.empty}>Ask Butler about anything across your projects — what needs you, what's stuck, what to pause. Butler keeps working on each thread while you start the next.</p>}
      {openThreadId && threadError[openThreadId] && <div className={styles.error} role="alert">{threadError[openThreadId]} <button type="button" className={styles.link} onClick={() => void loadMessages(openThreadId)}>Retry</button></div>}
      {threadMessages.map((message, index) => {
        const steps = message.role === "assistant" ? activityFromSavedResults(message.id, message.toolResults) : [];
        return <div key={message.id ?? index} className={message.role === "user" ? styles.question : styles.answer}>
          {steps.length > 0 && <ActivityLedger model={activityLedgerFromNative("Butler's steps", "complete", steps)} />}
          {message.role === "user" ? message.content : <ButlerMarkdown content={message.content} />}
        </div>;
      })}
      {pendingSend && (pendingSend.threadId ? pendingSend.threadId === openThreadId : !openThreadId) && <>
        <div className={styles.question}>{pendingSend.prompt}</div>
        <p className={styles.working}><i className={styles.dot} data-state="working" aria-hidden="true" /> Starting Butler’s response…</p>
      </>}
      {openLive && !threadMessages.some((message) => message.role === "user" && message.content.endsWith(openLive.prompt)) && <div className={styles.question}>{openLive.prompt}</div>}
      {openThreadId && openActivity && <ActivityLedger model={activityLedgerFromNative("Butler's steps", ledgerStatus(openActivity), openActivity.cards)} liveStatus={openActivity.detail} />}
      {openLive && (openLive.text ? <div className={styles.answer}><ButlerMarkdown content={openLive.text} /></div> : !openLive.error && !openActivity && <p className={styles.working}><i className={styles.dot} data-state="working" aria-hidden="true" /> Butler is working on this…</p>)}
      {openThreadId && threadProposals[openThreadId]?.map((proposal) => <ProposalCard key={proposal.sessionId} proposal={proposal} note={actionNote[proposal.sessionId]} actions={proposal.kind === "goal_chat_proposal" ? proposalActions(proposal.sessionId, proposal.title) : null} onOpenBrief={() => openBrief(proposal.sessionId)} />)}
      {openLive?.browserAction && <p className={styles.note}>Butler asked this device to finish a step in the browser (for example a picker or setting). Open the Butler panel to complete it there.</p>}
      {openLive?.error && <div className={styles.error} role="alert">{openLive.error}</div>}
      {openThreadSummary && !openLive && !openActivity && openThreadSummary.state !== "idle" && <p className={styles.working}><i className={styles.dot} data-state={openThreadSummary.state} aria-hidden="true" /> {openThreadSummary.state === "waiting" ? "Butler's answer is waiting for you. Open its conversation to approve or answer." : "Butler is working on this thread…"}</p>}
    </div>
    {awayFromLatest && <button type="button" className={styles.jumpLatest} aria-label="Jump to latest message" title="Jump to latest message" onClick={jumpToLatest}><ArrowDown size={16} aria-hidden="true" /></button>}
    </div>
    <form className={styles.composer} onSubmit={(event) => void send(event)}>
      {butlerDrafts.length > 0 && <div className={styles.contextPack} role="group" aria-label="Selected context for Butler">
        {butlerDrafts.map((item, index) => <span key={`${item.source.kind}:${item.source.id ?? ""}:${index}`} className={styles.chip} title={item.text.slice(0, 400)}>{item.source.label}{item.truncated ? " · shortened" : ""}<button type="button" className={styles.chipClose} aria-label={`Remove ${item.source.label} from Butler context`} title="Remove" onClick={() => drafts?.removeButlerDraft(index)}><X size={12} aria-hidden="true" /></button></span>)}
        <span className={styles.muted}>Sent with your next message</span>
      </div>}
      {about && <div className={styles.about}><span className={styles.muted}>About</span><span className={styles.chip}><i className={styles.dot} data-state={about.state} aria-hidden="true" />{about.title}<button type="button" className={styles.chipClose} aria-label={`Stop asking about ${about.title}`} title="Remove" onClick={() => setAbout(undefined)}><X size={12} aria-hidden="true" /></button></span></div>}
      {openThreadId && <QueuedMessages queue={threadQueue} label="Thread queue" sentWhen="when Butler finishes this answer" className={styles.threadQueue} interrupts={openAnswering} onSendNow={(item) => void (async () => { if (await threadQueue.mutate({ action: "interrupt", item_id: item.id }) && openAnswering) await stopOpenThread(); })()} />}
      {composerError && <div className={styles.error} role="alert">{composerError}</div>}
      <div className={styles.inputRow}>
        <textarea ref={composerRef} rows={2} value={draft} onChange={(event) => setDraft(event.target.value)} onKeyDown={onComposerKey} aria-label="Message Butler" placeholder={replyQueues ? `Queue a reply, or ${sendNowShortcutLabel()} to send it now` : target === "reply" && openThreadSummary ? "Reply in this thread" : "Ask Butler anything — each question runs as its own thread"} />
        {replyQueues && draft.trim() && <button type="button" className={styles.sendNow} aria-label="Send now" aria-keyshortcuts={sendNowShortcutLabel() === "⌘↵" ? "Meta+Enter" : "Control+Enter"} title={`Stop Butler's answer and send this now (${sendNowShortcutLabel()})`} disabled={starting || stopping || !api} onClick={() => void send(undefined, true)}><Send size={14} aria-hidden="true" /><span>Send now</span></button>}
        {showStop
          ? <button type="button" className={`${styles.send} ${styles.stop}`} aria-label="Stop response" title="Stop Butler's response in this thread" disabled={stopping || !api} onClick={() => void stopOpenThread()}><Square size={14} aria-hidden="true" /></button>
          : replyQueues ? <button type="submit" className={styles.send} aria-label="Queue reply" title="Send after Butler's current answer (↵)" disabled={!draft.trim() || starting || !api}><ListTodo size={16} aria-hidden="true" /></button>
          : <button type="submit" className={styles.send} aria-label={target === "reply" && openThreadSummary ? "Send reply" : "Start thread"} title={target === "reply" && openThreadSummary ? "Send reply" : "Start thread"} disabled={!draft.trim() || starting || !api}><ArrowUp size={16} aria-hidden="true" /></button>}
      </div>
      <div className={styles.targets}>
        <button type="button" className={styles.target} aria-pressed={target === "new"} onClick={() => { setTarget("new"); setAboutWaitsFor(undefined); setStartedWaitsFor(undefined); }}>New thread</button>
        {openThreadSummary && <button type="button" className={styles.target} aria-pressed={target === "reply"} onClick={() => { setTarget("reply"); setAboutWaitsFor(undefined); setStartedWaitsFor(undefined); }}>Reply in this thread</button>}
        {target === "new" && <label className={styles.projectPick}><span className={styles.muted}>in</span>
          <select value={newThreadProject ?? ""} onChange={(event) => { setNewThreadProject(event.target.value); setAboutWaitsFor(undefined); if (about && about.project.id !== event.target.value) setAbout(undefined); }} aria-label="Project for the new thread">
            {visibleProjects.map((project) => <option key={project.id} value={project.id}>{project.name}</option>)}
          </select>
        </label>}
      </div>
    </form>
  </section>;

  // ↑ and ↓ step through the board's chats while a brief is open.
  const boardOrder = useMemo(() => groups.flatMap((group) => group.label !== undefined && collapsed.has(group.key) ? [] : group.sections
    .filter((entry) => boardSectionOpen(sectionOpen, group.key, entry.section))
    .flatMap((entry) => entry.items.map((item) => item.sessionId))), [groups, collapsed, sectionOpen]);
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

  const allItems = [...(board?.items ?? []), ...Object.values(completed).flatMap((page) => page.items)];
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
    workStage={briefItem?.workStage}
    butlerCase={briefItem?.butlerCase ? { stage: butlerCaseStage(briefItem.butlerCase.status),
      onOpen: briefItem.butlerCase.sessionId ? () => openPopup(briefItem.butlerCase!.sessionId!, "Butler check", briefItem.project.id) : undefined } : undefined}
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
      {nativeShell && api && <ShellPushSettings api={api} />}
      {scopeProject ? <div ref={setSettingsHost} className={styles.embedHost} /> : noProject}
      {!bridge && <p className={styles.muted}>Butler's settings are unavailable here.</p>}
    </section>
    : brief ?? boardView;

  const projectThread = projectThreadId ? <section className={styles.conversation} aria-label={`Project thread: ${visibleProjects.find((item) => item.id === projectThreadId)?.name ?? "project"}`}>
    <div className={styles.threadBar}>
      {compact && <button type="button" className={styles.iconButton} aria-label="Back to threads" title="Back to threads" onClick={() => updateParams((next) => next.delete("pthread"))}><ArrowLeft size={16} aria-hidden="true" /></button>}
      {railToggle}
      <h2>{visibleProjects.find((item) => item.id === projectThreadId)?.name ?? "Project"}</h2>
      <span className={styles.muted}>project thread</span>
      {boardToggle}
    </div>
    {engagement?.id === projectThreadId ? <div ref={setChatHost} className={styles.embedHost} /> : loading}
  </section> : null;

  const codeSection = api && scopeProject ? <Suspense fallback={loading}><ButlerCodeSection api={api} projectId={scopeProject} openPath={openFilePath}
    onOpenBrief={openBrief}
    onAskAbout={(path) => { setDraft((current) => current || `About ${path}: `); updateParams((next) => { next.delete("section"); next.delete("pthread"); next.set("tab", "threads"); }); window.setTimeout(() => composerRef.current?.focus(), 0); }} /></Suspense> : noProject;

  const sectionTabs = <nav className={styles.sections} aria-label="Butler sections">
    {SECTIONS.map((item) => <button key={item.id} type="button" className={styles.sectionTab} aria-current={section === item.id ? "page" : undefined} onClick={() => setSection(item.id)}>
      {item.label}
      {item.id === "board" && states?.waiting ? <span className={styles.badge} data-state="waiting">{states.waiting}</span> : null}
      {item.id === "oversight" && bridge?.attentionCount ? <span className={styles.badge} data-state="failed">{bridge.attentionCount}</span> : null}
    </button>)}
  </nav>;

  const leftColumn = projectThread ?? conversation;
  const phoneThreads = projectThread ?? (openThreadId || about || draft ? conversation : <>{threadsRail}{conversation}</>);

  return <div className={styles.mode} data-compact={compact ? "true" : "false"} data-selection-recipient="butler">
    <header className={styles.topBar}>
      <h1>Butler</h1>
      {!compact && sectionTabs}
      <span className={styles.spacer} />
      {states && !compact && <div className={styles.pulse} aria-label="Goal chat status across projects">
        <span><i className={styles.dot} data-state="waiting" aria-hidden="true" />{states.waiting} need you</span>
        <span><i className={styles.dot} data-state="working" aria-hidden="true" />{states.working} working</span>
        {states.failed > 0 && <span><i className={styles.dot} data-state="failed" aria-hidden="true" />{states.failed} failed</span>}
      </div>}
      {!compact && !nativeShell && <Link className={styles.projectsLink} to="/projects" title="Every project's stages and work"><Columns3 size={15} aria-hidden="true" />All projects</Link>}
      {bridge && <button type="button" className={styles.iconButton} aria-label="Start Butler Voice" title="Start Butler Voice" disabled={!bridge.voiceAvailable} onClick={bridge.startVoice}><MicrophoneIcon size={17} /></button>}
      {!nativeShell && <button type="button" className={styles.iconButton} aria-label="Exit Butler mode" title="Exit Butler mode (Esc)" onClick={exit}><X size={17} aria-hidden="true" /></button>}
    </header>
    {/* Butler mode covers the app's own connection banner, so it offers the retry itself. */}
    {coreState !== "online" && <div className={styles.error} role="alert"><span>Core is unavailable. Butler mode reconnects and reads the board again when Core answers.</span><button type="button" className="button quiet" onClick={reconnect}>Retry connection</button></div>}
    {compact ? <>
      <div className={styles.tabs} role="tablist" aria-label="Butler mode">
        <button type="button" role="tab" aria-selected={phoneTab === "board" && section === "board"} onClick={() => setSection("board")}>Board{states?.waiting ? <span className={styles.tabCount} data-state="waiting">{states.waiting}</span> : null}</button>
        <button type="button" role="tab" aria-selected={phoneTab === "threads"} onClick={() => setPhoneTab("threads")}>Threads{runningThreads ? <span className={styles.tabCount} data-state="working">{runningThreads}</span> : null}</button>
        <button type="button" role="tab" aria-selected={phoneTab === "board" && section === "oversight"} onClick={() => setSection("oversight")}>Oversight{bridge?.attentionCount ? <span className={styles.tabCount} data-state="failed">{bridge.attentionCount}</span> : null}</button>
        <button type="button" role="tab" aria-selected={phoneTab === "board" && (section === "code" || section === "memory" || section === "settings")} aria-haspopup="dialog" aria-expanded={moreOpen} onClick={() => setMoreOpen(true)}><MoreHorizontal size={16} aria-hidden="true" /> More</button>
      </div>
      <div className={styles.phoneBody}>{phoneTab === "threads" ? phoneThreads : section === "code" ? codeSection : workspace}</div>
      {moreOpen && <div className={styles.sheetLayer}>
        <button type="button" className={styles.sheetScrim} aria-label="Close" tabIndex={-1} onClick={() => setMoreOpen(false)} />
        <div className={styles.sheet} role="dialog" aria-label="More in Butler">
          {(["code", "memory", "settings"] as const).map((value) => <button key={value} type="button" className={styles.sheetRow} onClick={() => setSection(value)}>{SECTIONS.find((item) => item.id === value)?.label}<ChevronRight size={16} aria-hidden="true" /></button>)}
          {!nativeShell && <Link className={styles.sheetRow} to="/projects">All projects<ChevronRight size={16} aria-hidden="true" /></Link>}
          {!nativeShell && <button type="button" className={styles.sheetRow} onClick={exit}>Open full Nebula<ChevronRight size={16} aria-hidden="true" /></button>}
        </div>
      </div>}
    </> : section === "code" ? <div className={styles.codeBody}>{codeSection}</div> : <div className={styles.body} style={columns.style} data-resizing={columns.dragging ? "true" : undefined} data-rail={railHidden ? "hidden" : undefined} data-board={boardHidden ? "hidden" : undefined}>
      {!railHidden && <>{threadsRail}{columns.railHandle}</>}
      {leftColumn}
      {!boardHidden && <>{columns.middleHandle}{workspace}</>}
    </div>}
    {popup && api && <Suspense fallback={null}><ButlerChatPopup api={api} sessionId={popup.sessionId} title={popup.title}
      live={allItems.some((item) => item.sessionId === popup.sessionId && (item.state === "working" || item.state === "waiting"))}
      onClose={() => setPopup(undefined)}
      onOpenFull={nativeShell ? undefined : () => { const target = popup; setPopup(undefined); navigate(chatPath(target.projectId, target.sessionId)); }} /></Suspense>}
  </div>;
}

const URGENCY_LABEL: Record<OperatorActionRequest["urgency"], string> = { blocking: "Blocking", high: "High", normal: "Normal", low: "Low" };

/** Things only the operator can do, at the top of the board until done, cancelled or closed by their auto-check. */
function OperatorActionsPanel({ requests, busy, error, onResolve }: { requests: OperatorActionRequest[]; busy?: string; error: string; onResolve: (request: OperatorActionRequest, outcome: "done" | "cancelled") => void }) {
  return <section className={styles.operatorActions} aria-label="Needs you">
    <h2 className={styles.operatorActionsHead}>Needs you <span className={styles.muted}>{requests.length.toLocaleString()} {requests.length === 1 ? "request" : "requests"}</span></h2>
    {error && <div className={styles.error} role="alert">{error}</div>}
    <ul className={styles.operatorActionList}>{requests.map((request) => <li key={request.id} className={styles.operatorAction} data-urgency={request.urgency}>
      <div className={styles.operatorActionTitle}>
        <span className={styles.operatorUrgency} data-urgency={request.urgency}>{URGENCY_LABEL[request.urgency]}</span>
        <strong>{request.typed ? request.actionLabel : request.title}</strong>
        {request.deviceOrHost && <span className={styles.operatorDevice}>{request.deviceOrHost}</span>}
      </div>
      <p className={styles.cardNote}>{request.reason}</p>
      <div className={styles.operatorActionMeta}>
        <span>{request.project.name || "Untitled project"}</span>
        {request.goal && <Link to={chatPath(request.project.id, request.goal.sessionId)}>{request.goal.title || "Goal chat"}{request.goal.status ? ` · ${request.goal.status}` : ""}</Link>}
        <span data-check={request.lastCheck?.passed === true ? "passes" : undefined}>{operatorActionCheckLabel(request)}</span>
      </div>
      <div className={styles.operatorActionButtons}>
        <button type="button" className={styles.primary} disabled={busy !== undefined} aria-label={`Mark done: ${request.title}`} onClick={() => onResolve(request, "done")}>{busy === request.id ? "Saving…" : "Done"}</button>
        <button type="button" className={styles.secondary} disabled={busy !== undefined} aria-label={`Cancel request: ${request.title}`} onClick={() => onResolve(request, "cancelled")}>Cancel</button>
      </div>
    </li>)}</ul>
  </section>;
}

function BoardCard({ item, clock, expanded, approval, deciding, note, onToggle, onDecide, onStop, onAsk, onOpen, showProject, selectable, selected, selectDisabled, onSelect, onRelease, onPriority, releaseDisabled }: {
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
  selectable: boolean;
  selected: boolean;
  selectDisabled: boolean;
  onSelect: () => void;
  onRelease: () => void;
  onPriority: (high: boolean) => void;
  releaseDisabled: boolean;
}) {
  const elapsed = boardElapsed(item.section !== "active" || item.state === "idle" || item.state === "failed" ? item.updatedAt : item.turnStartedAt ?? item.updatedAt, clock);
  const runtime = [showProject ? item.project.name : undefined, item.model, item.isSubagent ? "Subagent" : undefined, item.proposedBy ? "Proposed by Butler" : item.startedBy ? "Started by Butler" : undefined].filter(Boolean).join(" · ");
  const goal = item.goal ? `Goal ${Math.min(item.goal.currentStep + 1, Math.max(item.goal.planLength, 1))}/${Math.max(item.goal.planLength, 1)} · ${item.goal.status}` : undefined;
  const pendingApproval = item.attention?.kind === "approval" && approval?.id === item.attention.actionId ? approval : undefined;
  // The section's name is in the label too: the dot's colour and shape are not the only cue.
  const high = item.priority === "high";
  const held = Boolean(item.hold);
  const resumable = isResumableIdle(item);
  const butlerStage = item.butlerCase ? butlerCaseStage(item.butlerCase.status) : undefined;
  const refusal = item.goal?.modelRefusal;
  return <article className={styles.card} data-section={held ? "held" : item.section} data-state={item.state} data-expanded={expanded ? "true" : "false"} data-selected={selected ? "true" : undefined} data-priority={high ? "high" : undefined} aria-label={`${item.title}: ${held ? "Held" : boardStatusLabel(item)}, ${held ? "Held" : SECTION_LABEL[item.section]}${high ? ", high priority" : ""}${item.workStage ? `, ${chatStageName(item.workStage)}` : ""}${butlerStage ? `, Butler ${butlerStage.label.toLowerCase()}` : ""}`}>
    <div className={styles.cardRow}>
    {selectable && <label className={styles.cardSelect} title={selected ? "Deselect" : resumable ? "Select to resume" : "Select to hold"}>
      <input type="checkbox" checked={selected} disabled={selectDisabled} onChange={onSelect} aria-label={`Select ${item.title} to ${resumable ? "resume" : "hold"}`} />
    </label>}
    <button type="button" className={styles.cardSummary} aria-expanded={expanded} onClick={onToggle}>
      <span className={styles.cardHead}><i className={styles.sectionDot} data-section={held ? "held" : item.section} data-goal={item.goal?.status} aria-hidden="true" />{high && <ChevronUp className={styles.priorityMark} size={14} aria-hidden="true" />}<span className={styles.cardTitle}>{item.title}</span>{elapsed && <span className={styles.muted}>{elapsed}</span>}</span>
      {runtime && <span className={styles.cardMeta}>{runtime}</span>}
      {item.now && <span className={styles.cardNow} data-state={item.state}>{item.now}</span>}
      {refusal && <span className={styles.refusalBadge} data-paused={refusal.paused ? "true" : undefined} title={refusal.reason ?? "Model refusals recorded on this goal"}>{refusal.paused ? "Model refusal" : "Model refusals"} · {refusal.count}</span>}
      {(item.workStage || butlerStage) && <span className={styles.cardStage}>{item.workStage && <WorkStageLabel stage={item.workStage} />}{butlerStage && <ButlerStageLabel stage={butlerStage} />}</span>}
      {goal && !expanded && <span className={styles.cardMeta}>{goal}</span>}
      {held && item.hold && <span className={styles.cardMeta}>On hold · {item.hold.reason}</span>}
      {note && !expanded && <span className={styles.cardNote} role="status" data-failed={note.startsWith("Not resumed") ? "true" : undefined}>{note}</span>}
    </button>
    {held && <button type="button" className={styles.iconButton} disabled={releaseDisabled} aria-label={`Release ${item.title}`} title="Release" onClick={onRelease}><Play size={14} aria-hidden="true" /></button>}
    </div>
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
        <button type="button" className={styles.secondary} aria-pressed={high} onClick={() => onPriority(!high)}><ChevronUp size={13} aria-hidden="true" /> {high ? "Clear high priority" : "High priority"}</button>
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
