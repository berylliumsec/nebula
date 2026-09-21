import { modelCatalogSummary, modelOptionLabel } from "../api/modelCatalog";
import { rememberToolSharing, sharesToolResultsAlways, type ToolSharingRuntime } from "../api/toolSharingConsent";
import { HarnessReasoningDetails } from "../components/HarnessReasoningDetails";
import { IconAction } from "../components/IconAction";
import { ManagedAssistantBrowser } from "../components/ManagedAssistantBrowser";
import { BrowserAssistantPanel, BROWSER_ASSISTANT_SHEET_QUERY } from "../components/BrowserAssistantPanel";
import { useResizableSidePanel } from "../components/useResizableSidePanel";
import "../browser-assistant.css";
import { useChatComposerAnchor } from "./useChatComposerAnchor";
import { ChatTurnDetails } from "../components/ChatTurnDetails";
import { ChatCatchUp } from "../components/ChatCatchUp";
import { ResolvedApprovalNotice } from "../components/ResolvedApprovalNotice";
import { isPendingRequest, pendingApprovalId, useSessionState } from "./useSessionState";
import { RefreshCw } from "lucide-react";
import { ChatEvidence } from "../components/ChatEvidence";
import { ChatDecisions, type DecisionSeed } from "../components/ChatDecisions";
import { useChatQueue } from "./useChatQueue";
import { ChatQueuePanel } from "../components/ChatQueuePanel";
import { estimateLiveTokens, ProviderGoalPanel, type ProviderGoalDraft } from "../components/ProviderGoalPanel";
import { ProviderSessionAdvanced } from "../components/ProviderSessionAdvanced";
import { isGuideLayerTarget, useGuideAction } from "../guides/guideActions";
import { ShowMeHow } from "../guides/ShowMeHow";
import { ChatWorkspaceDrawer } from "../components/ChatWorkspaceDrawer";
import { ChatAttachments } from "../components/ChatAttachments";
import { EnvironmentTargetPicker, environmentIdsForTarget, type EnvironmentTarget } from "../components/EnvironmentTargetPicker";
import { sshApprovalTarget } from "../sshTools";
import { ChatResults } from "../components/ChatResults";
import { ChatResultStream, useStructuredResults, useUnseenCount } from "../components/structured-result";
import { ChatSubagentPane, ChatSubagentRail, ChatSubagentResultCard, useChatSubagents } from "../components/chat-subagents";
import { ChatRecordedContext } from "../components/ChatRecordedContext";
import { useChatNavigation } from "./useChatNavigation";
import { ChatSearchPanel } from "../components/ChatSearchPanel";
import { AssistantApprovalDetails } from "../components/AssistantApprovalDetails";
import { AssistantSetupLinks } from "../components/AssistantSetupLinks";
import { useCompactLayout } from "../hooks/useCompactLayout";
import { MobileMorePanel } from "../components/MobileMorePanel";
import { MobileDrawerFooter, MobileDrawerProject } from "../components/MobileDrawerChrome";
import { MobileApprovals } from "../components/MobileApprovals";
import { lazy, Suspense, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type ChangeEvent, type ClipboardEvent as ReactClipboardEvent, type CSSProperties, type DragEvent as ReactDragEvent, type FormEvent, type KeyboardEvent } from "react";
import {useComposerAutosize} from "./useComposerAutosize";
import { createPortal } from "react-dom";
import {
  AssistantRuntimeProvider,
  ThreadPrimitive,
  useExternalStoreRuntime,
  type ThreadMessageLike,
} from "@assistant-ui/react";
import {
  Activity,
  Archive,
  ArchiveRestore,
  Bot,
  Bookmark,
  Boxes,
  Braces,
  Check,
  ChevronDown,
  Copy,
  Download,
  FileClock,
  Files,
  FolderOpen,
  Globe2,
  Gauge,
  GitFork,
  History,
  LoaderCircle,
  LayoutGrid,
  ListTodo,
  Maximize2,
  MessageSquare,
  MessageSquareQuote,
  Minimize2,
  MoreHorizontal,
  NotebookPen,
  PanelLeft,
  PanelLeftClose,
  PanelRight,
  Pencil,
  Plus,
  Search,
  Send,
  Settings2,
  Sparkles,
  ShieldCheck,
  Square,
  SquarePen,
  SquareTerminal,
  Trash2,
  X,
} from "lucide-react";
import { ApiError, type ApiClient } from "../api/client";
import { ChatPreviewCache } from "./chatPreviewCache";
import { Link, useSearchParams, type NavigateOptions } from "react-router-dom";
import { providerModelVerification } from "../api/providerCapabilities";
import { defaultModelRuntime, providerDefaultModel } from "../api/runtimeDefaults";
import type {
  ChatCompletionRequest,
  ChatGoal,
  ChatContentBlock,
  ChatSessionActivity,
  ChatSessionSummary,
  ChatStreamEvent,
  ChatTurn,
  ContextStatus,
  ExecutionCapabilities,
  ExecutionLanguage,
  EngagementScopePolicy,
  HarnessProfile,
  HarnessActivityEvent,
  HarnessInteraction,
  HarnessSessionActivity,
  HarnessSessionSummary,
  ExternalHarnessSessionSummary,
  HarnessSkillSummary,
  McpServerProfile,
  NativeHookDescriptor,
  NativeHookExecution,
  PersistedChatMessage,
  ToolArtifactReference,
  ToolOutputReadResult,
  ToolOutputSearchResult,
  ReasoningEffort,
} from "../api/types";
import { REASONING_EFFORTS } from "../api/types";
import { AssistantMarkdown, type FencedRunCandidate } from "../components/AssistantMarkdown";
import { ToolSuggestionChip } from "../components/ToolSuggestionChip";
import { ActivityLedger } from "../components/ActivityLedger";
import {
  activityLedgerFromHarness,
  activityLedgerFromNative,
  type ActivityLedgerEntry,
  type NativeActivitySource,
} from "../components/activityLedgerModel";
import { sha256 } from "../components/assistantCode";
import { ExecutionHistory } from "../components/ExecutionHistory";
import { ExecutionReviewDialog } from "../components/ExecutionReviewDialog";
import { NewMissionButton } from "../components/MissionControls";
import { NotesPanel } from "../components/NotesPanel";
import { PageHeader, PageHeaderAction } from "../components/PageHeader";
import { PostToolAssistant } from "../components/PostToolAssistant";
import { TerminalCommandHistoryPanel } from "../components/TerminalCommandHistoryPanel";
import { ModalSurface, useConfirmation } from "../components/DialogSystem";
import { copySelectionText, createHashedSelectionAttachment } from "../components/selection";
import { WorkspacePanel } from "../components/WorkspacePanel";
import { HarnessSkillAutocomplete, findHarnessSkillToken, type HarnessSkillTokenRange } from "../components/HarnessSkillAutocomplete";
import { HarnessThinking, ThinkingDisclosure } from "../components/HarnessThinking";
import { HarnessMarkdown } from "../components/HarnessMarkdown";
import { HarnessCommandHints, isHarnessCommand } from "../components/HarnessCommandHints";
import { HarnessStatusRail } from "../components/HarnessStatusRail";
import { WorkbenchBrowser } from "../components/WorkbenchBrowser";
import { TabBar, Toolbar } from "../components/SurfacePrimitives";
import { useWorkbenchDrafts } from "../state/WorkbenchDraftContext";
import { useWorkspace } from "../state/WorkspaceContext";
import { useChrome } from "../state/ChromeContext";
import { settingCatalogEntry } from "../settingsCatalog";
import { WebSearchResults } from "../components/WebSearchResults";
import { AgentsPage } from "./AgentsPage";
import {
  harnessCostLabel,
  isTimelineActivity,
  isSameHarnessSessionActivity,
  reduceHarnessActivity,
  shouldShowActivityItem,
  type HarnessActivityItem,
} from "./harnessActivity";
import { beginGuardedStream, detachChatStream } from "./chatStreamLifecycle";
import { elapsedDetail, elapsedSince, formatLiveElapsed, formatTurnElapsed } from "./turnElapsed";
import {
  cancelActiveAssistantMessage,
  cancelStreamingAssistantMessage,
  reconcileCompletedAssistantMessage,
  recoverHarnessHistory,
  type ReconciledConversationMessage,
} from "./chatMessageReconciliation";
import { DiagnosticErrorNotice, logCaughtDiagnostic } from "../diagnostics";
import { readConversationPanelOpen, writeConversationPanelOpen } from "./workbenchPreferences";
import { chatDraftStorageKey, clearChatDraft, readChatDraft, writeChatDraft } from "./chatDraftStorage";
import {
  chatFollowUpStorageKey,
  maxChatFollowUps,
  clearChatFollowUps,
  readChatFollowUps,
  writeChatFollowUps,
  type ChatFollowUp,
} from "./chatFollowUpStorage";
import { chatTranscriptFilename, formatChatTranscript } from "./chatTranscriptExport";

import { followsChatBottom, type ChatScrollGeometry } from "./chatScrollPosition";

const CHAT_TERMINAL_OPEN_KEY = "nebula.chat-terminal.open";
type SessionView = "chat" | "code" | "terminal" | "browser" | "missions" | "activity" | "workspace" | "notes";
const sessionViews = new Set<string>(["chat", "code", "terminal", "browser", "missions", "activity", "workspace", "notes"] satisfies SessionView[]);
const screenFitViews = new Set<SessionView>(["terminal", "code", "workspace", "browser"]);

function sessionViewFromParam(value: string | null): SessionView | undefined {
  const view = value === "executions" ? "activity" : value === "files" ? "workspace" : value;
  return view && sessionViews.has(view) ? view as SessionView : undefined;
}

// BrowserRouter commits locations inside a transition, so `useSearchParams()`
// can trail the address bar while urgent updates render. History updates the
// address bar synchronously, so writes compose from it instead of a snapshot.
function latestSearchParams(): URLSearchParams {
  return new URLSearchParams(window.location.search);
}
const readableContextStatuses = new Set<ContextStatus["status"]>(["not_needed", "ready", "stale", "failed", "runtime_managed"]);

function isReadableContextStatus(value: ContextStatus, expectedOwnerId: string): boolean {
  return value.ownerType === "chat_session"
    && value.ownerId === expectedOwnerId
    && readableContextStatuses.has(value.status)
    && Number.isFinite(value.estimatedInputTokens)
    && Number.isFinite(value.targetInputTokens)
    && Number.isFinite(value.compactedThrough);
}

interface ToolLifecycleCard extends NativeActivitySource {
  resultArtifactId?: string;
  artifacts: ToolArtifactReference[];
}

interface ChatScrollTraceWindow extends Window {
  __NEBULA_CHAT_SCROLL_TRACE__?: () => string;
}

function attachChatScrollTrace(viewport: HTMLDivElement) {
  const startedAt = performance.now();
  const entries: Array<Record<string, unknown>> = [];
  const traceWindow = window as ChatScrollTraceWindow;
  const describeElement = (value: EventTarget | Element | null) => {
    if (!(value instanceof Element)) return null;
    const classes = [...value.classList].slice(0, 4).join(".");
    return `${value.tagName.toLowerCase()}${value.id ? `#${value.id}` : ""}${classes ? `.${classes}` : ""}`;
  };
  const record = (kind: string, details: Record<string, unknown> = {}) => {
    entries.push({
      atMs: Math.round((performance.now() - startedAt) * 10) / 10,
      kind,
      scrollTop: Math.round(viewport.scrollTop * 10) / 10,
      scrollHeight: viewport.scrollHeight,
      clientHeight: viewport.clientHeight,
      maxScrollTop: Math.max(0, viewport.scrollHeight - viewport.clientHeight),
      activeElement: describeElement(document.activeElement),
      ...details,
    });
    if (entries.length > 2_000) entries.splice(0, entries.length - 2_000);
  };
  const onWheel = (event: WheelEvent) => record("wheel", {
    deltaY: Math.round(event.deltaY * 10) / 10,
    deltaMode: event.deltaMode,
    target: describeElement(event.target),
  });
  const onScroll = () => record("scroll");
  const onScrollEnd = () => record("scrollend");
  const onFocus = (event: FocusEvent) => record("focusin", { target: describeElement(event.target) });
  const resizeObserver = new ResizeObserver(() => record("resize"));
  const mutationObserver = new MutationObserver((mutations) => record("mutation", {
    addedNodes: mutations.reduce((count, mutation) => count + mutation.addedNodes.length, 0),
    removedNodes: mutations.reduce((count, mutation) => count + mutation.removedNodes.length, 0),
    attributes: mutations.filter((mutation) => mutation.type === "attributes").map((mutation) => mutation.attributeName),
  }));
  const nativeScrollTo = viewport.scrollTo.bind(viewport);
  const instrumentedViewport = viewport as HTMLDivElement & {
    scrollTo: (optionsOrX?: ScrollToOptions | number, y?: number) => void;
  };
  instrumentedViewport.scrollTo = (optionsOrX?: ScrollToOptions | number, y?: number) => {
    record("scrollTo", { arguments: typeof optionsOrX === "number" ? [optionsOrX, y] : optionsOrX });
    if (typeof optionsOrX === "number") nativeScrollTo(optionsOrX, y ?? 0);
    else nativeScrollTo(optionsOrX);
  };
  viewport.addEventListener("wheel", onWheel, { capture: true, passive: true });
  viewport.addEventListener("scroll", onScroll, { passive: true });
  viewport.addEventListener("scrollend", onScrollEnd, { passive: true });
  document.addEventListener("focusin", onFocus, true);
  resizeObserver.observe(viewport);
  mutationObserver.observe(viewport, { attributes: true, childList: true, subtree: true });
  traceWindow.__NEBULA_CHAT_SCROLL_TRACE__ = () => JSON.stringify({
    capturedAt: new Date().toISOString(),
    userAgent: navigator.userAgent,
    entries,
  }, null, 2);
  record("attached");
  return () => {
    viewport.removeEventListener("wheel", onWheel, true);
    viewport.removeEventListener("scroll", onScroll);
    viewport.removeEventListener("scrollend", onScrollEnd);
    document.removeEventListener("focusin", onFocus, true);
    resizeObserver.disconnect();
    mutationObserver.disconnect();
    Reflect.deleteProperty(viewport, "scrollTo");
    delete traceWindow.__NEBULA_CHAT_SCROLL_TRACE__;
  };
}

interface PendingChatResponse {
  turnId: string;
  assistantId: string;
  userId: string;
  request: ChatCompletionRequest;
  approval: Record<string, unknown>;
}

interface InterruptedChatRecovery {
  turn: ChatTurn;
  assistantId: string;
  request: ChatCompletionRequest;
}

interface FailedProviderRecovery {
  turnId: string;
  assistantId: string;
  request: ChatCompletionRequest;
}

interface HarnessProgress {
  phase: string;
  detail: string;
  sessionId?: string;
  turnId?: string;
  previousSessionId?: string;
}

const ContainerTerminalPanel = lazy(() => import("../components/ContainerTerminalPanel").then((module) => ({ default: module.ContainerTerminalPanel })));
const CodeEditorPanel = lazy(() => import("../components/CodeEditorPanel").then((module) => ({ default: module.CodeEditorPanel })));
const CHAT_COMPOSER_MAX_HEIGHT = 160;
// How often a running goal is read back while it works. Core owns its step,
// usage and revision; the panel only mirrors them.
const GOAL_REFRESH_MS = 5_000;

interface ConversationMessage extends ReconciledConversationMessage {}

interface PendingChatImage {
  block: ChatContentBlock;
  previewUrl: string;
  filename: string;
}

function AuthenticatedChatImage({ api, block }: { api: ApiClient; block: ChatContentBlock }) {
  const previewArtifactId = typeof block.metadata?.preview_artifact_id === "string"
    ? block.metadata.preview_artifact_id
    : block.artifactId;
  const [url, setUrl] = useState<string>();
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    if (!previewArtifactId) return;
    const controller = new AbortController();
    let objectUrl: string | undefined;
    void api.fetchChatImagePreview(previewArtifactId, controller.signal).then((blob) => {
      objectUrl = URL.createObjectURL(blob);
      setUrl(objectUrl);
    }).catch((error) => {
      if (!controller.signal.aborted) {
        logCaughtDiagnostic("interface.chat.image_preview_failed", "A chat image preview could not be loaded.", error, "chat-media");
        setFailed(true);
      }
    });
    return () => {
      controller.abort();
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [api, previewArtifactId]);
  if (failed) return <p role="status">Image preview unavailable. The original remains retained as an artifact.</p>;
  if (!url) return <div className="chat-thinking"><LoaderCircle className="spin" size={14} /> Loading image…</div>;
  return <a href={url} target="_blank" rel="noreferrer" title="Open image preview"><img className="chat-content-image" src={url} alt={block.alt ?? "Attached image"} /></a>;
}

function AssistantLedgerEntryDetails({ entry }: { entry: ActivityLedgerEntry }) {
  const item = entry.sourceItem;
  const tool = entry.sourceTool;
  if (tool) return <div className="activity-ledger-entry-body">
    {tool.summary && <p>{tool.summary}</p>}
    {tool.capability === "web.search" && <WebSearchResults receipt={tool.receipt} />}
    {tool.evidenceIds.length > 0 && <div className="scope-chip-list">{tool.evidenceIds.map((id) => <Link to={`/evidence?id=${encodeURIComponent(id)}`} key={id}>Evidence {id.slice(0, 8)}</Link>)}</div>}
    {Object.keys(tool.receipt ?? {}).length > 0 && <details className="activity-ledger-technical"><summary>Technical details</summary><pre tabIndex={0}>{JSON.stringify(tool.receipt, null, 2)}</pre></details>}
  </div>;
  if (!item) return null;
  return <div className="activity-ledger-entry-body">
    <HarnessReasoningDetails item={item} />
    {item.kind === "plan" && Array.isArray(item.payload.plan) && <ol className="harness-plan">{item.payload.plan.map((step, index) => {
      if (typeof step === "string") return <li key={index}>{step}</li>;
      if (!step || typeof step !== "object") return null;
      const planEntry = step as Record<string, unknown>;
      return <li className={`status-${String(planEntry.status ?? "pending")}`} key={String(planEntry.id ?? index)}><span className="status-dot" />{String(planEntry.title ?? planEntry.content ?? `Step ${index + 1}`)}<small>{String(planEntry.status ?? "pending").replaceAll("_", " ")}</small></li>;
    })}</ol>}
    {item.kind === "file_change" && Array.isArray(item.payload.files) && <ul className="harness-file-list">{item.payload.files.map((file, index) => <li key={index}><FileClock size={13} /> {typeof file === "string" ? file : JSON.stringify(file)}</li>)}</ul>}
    {entry.outputs.map((output, index) => <div className="harness-output" key={`${output.label}-${index}`}><small>{output.label}</small><pre tabIndex={0}>{output.content}</pre></div>)}
    {typeof item.payload.diff === "string" && item.payload.diff && <div className="harness-output diff"><small>Unified diff</small><pre tabIndex={0}>{item.payload.diff}</pre></div>}
    {item.kind && ["command", "tool", "web_search", "browser", "image", "skill", "hook", "review", "subagent"].includes(item.kind) && Object.keys(item.payload).length > 0 && <details className="activity-ledger-technical"><summary>Arguments and result details</summary><pre tabIndex={0}>{JSON.stringify(item.payload, null, 2)}</pre></details>}
    {item.usage && item.usage.totalTokens > 0 && <small>{item.usage.totalTokens.toLocaleString()} tokens{item.usage.reasoningTokens ? ` · ${item.usage.reasoningTokens.toLocaleString()} reasoning` : ""}{harnessCostLabel(item) ? ` · ${harnessCostLabel(item)}` : ""}{item.usage.durationMs ? ` · ${(item.usage.durationMs / 1000).toFixed(1)}s` : ""}</small>}
    {item.artifactIds.length > 0 && <div className="scope-chip-list">{item.artifactIds.map((id) => <span title={id} key={id}>Artifact {id.slice(0, 8)}</span>)}</div>}
  </div>;
}

function assistantMessageStatus(message: ConversationMessage): ThreadMessageLike["status"] {
  switch (message.state) {
    case "streaming":
      return { type: "running" };
    case "waiting_approval":
      return { type: "requires-action", reason: "interrupt" };
    case "cancelled":
      return { type: "incomplete", reason: "cancelled" };
    case "error":
      return { type: "incomplete", reason: "error", error: message.detail ?? "Chat response failed." };
    default:
      return { type: "complete", reason: "stop" };
  }
}

function convertConversationMessage(message: ConversationMessage): ThreadMessageLike {
  return {
    id: message.runtimeId ?? message.id,
    role: message.role,
    content: message.content,
    createdAt: new Date(message.createdAt),
    status: message.role === "assistant" ? assistantMessageStatus(message) : undefined,
    metadata: {
      custom: {
        durable: message.durable,
        sequence: message.sequence,
        state: message.state,
      },
    },
  };
}

function makeId(prefix: string): string {
  return `${prefix}-${globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random()}`}`;
}

function timeLabel(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "Now";
  return new Intl.DateTimeFormat(undefined, { hour: "numeric", minute: "2-digit" }).format(date);
}

function harnessPhaseLabel(phase: string): string {
  switch (phase) {
    case "ready": return "Harness session ready";
    case "status_unavailable": return "Harness status unavailable";
    case "queued": return "Harness request queued";
    case "connecting": return "Connecting to harness";
    case "parallel_session_created": return "Parallel harness session started";
    case "command_runtime_session_created": return "Command runtime updated";
    case "running": return "Harness is working";
    case "tool": return "Harness is using a tool";
    case "waiting_approval": return "Harness needs approval";
    case "decision_recorded": return "Decision recorded";
    case "finalizing": return "Saving harness response";
    case "complete": return "Harness turn complete";
    case "interrupted": return "Harness turn interrupted";
    case "failed": return "Harness turn failed";
    default: return "Harness status";
  }
}

interface ReplacedMessageGroup {
  id: string;
  at?: string;
  from: number;
  items: PersistedChatMessage[];
}

/** Replaced turns stay readable in place; the model only ever sees the live transcript. */
function ReplacedMessages({ group }: { group: ReplacedMessageGroup }) {
  const count = group.items.length;
  const label = `${count} replaced message${count === 1 ? "" : "s"}${group.at ? ` · ${timeLabel(group.at)}` : ""}`;
  return <details className="chat-replaced-group">
    <summary>{label}</summary>
    <p className="chat-replaced-note">Replaced by your edit and kept for reference. They are not sent to the model.</p>
    {group.items.map((item) => <article className="chat-replaced-message" key={item.id}>
      <strong>{`${item.role === "user" ? "You" : "Assistant"} · ${timeLabel(item.createdAt)}`}</strong>
      <p>{item.content}</p>
    </article>)}
  </details>;
}

/** Counts up in the slot the usage line takes once the turn lands. */
function LiveTurnElapsed({ startedAt }: { startedAt: string }) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 1_000);
    return () => window.clearInterval(timer);
  }, []);
  const elapsed = elapsedSince(startedAt, now);
  if (elapsed === undefined) return null;
  const label = formatLiveElapsed(elapsed);
  return <span className="chat-turn-elapsed" role="timer" aria-label={`Running for ${label}`}>{label}</span>;
}

function persistedMessage(message: PersistedChatMessage): ConversationMessage {
  return {
    id: message.id,
    role: message.role,
    content: message.content,
    reasoning: message.reasoning,
    contentBlocks: message.contentBlocks,
    createdAt: message.createdAt,
    citations: message.citations,
    usage: message.usage,
    elapsedMs: message.elapsedMs,
    approvalWaitMs: message.approvalWaitMs,
    state: "complete",
    durable: true,
    sequence: message.sequence,
    harnessTurnId: message.harnessTurnId,
    toolSuggestions: message.toolSuggestions,
  };
}

export function SessionsPage() {
  const confirm = useConfirmation();
  const { openSetting } = useChrome();
  const compact = useCompactLayout();
  const {
    assistantDraftNotice,
    assistantDrafts,
    clearAssistantDraftNotice,
    clearAssistantDrafts,
    clearExecutionDraft,
    clearNoteDraft,
    executionDraft,
    noteDraft,
    removeAssistantDraft,
    requestFindingDraft,
    requestNebulaDraft,
    requestChatContext,
    registerAssistantSnapshot,
  } = useWorkbenchDrafts();
  const [searchParams, setSearchParams] = useSearchParams();
  const requestedSessionId = searchParams.get("session") ?? "";
  // The URL is the only authority for the Workbench view, so every control on
  // screen was rendered from the location it writes back to. Deep links that
  // omit `view` (a conversation or mission) keep the view the URL last named.
  const requestedView = sessionViewFromParam(searchParams.get("view"));
  const [lastRequestedView, setLastRequestedView] = useState<SessionView>(requestedView ?? "terminal");
  if (requestedView && requestedView !== lastRequestedView) setLastRequestedView(requestedView);
  const view = requestedView ?? lastRequestedView;
  const updateSearchParams = useCallback((update: (params: URLSearchParams) => void, options?: NavigateOptions) => {
    const params = latestSearchParams();
    update(params);
    setSearchParams(params, options);
  }, [setSearchParams]);
  const setView = useCallback((next: SessionView) => {
    updateSearchParams(params => params.set("view", next), { replace: true });
  }, [updateSearchParams]);
  const openUnattachedChatView = () => updateSearchParams(params => {
    params.set("view", (sessionViewFromParam(params.get("view")) ?? view) === "browser" ? "browser" : "chat");
    params.delete("session");
  }, { replace: true });
  const consumedSelectionHandoff = useRef<string | null>(null);
  const clearSubmittedContext = () => {
    consumedSelectionHandoff.current = latestSearchParams().get("handoff");
    clearAssistantDrafts();
  };
  const openSessionChatView = (id: string, preserveSurface = false) => {
    // Stream callbacks outlive the render that submitted the message. Preserve
    // newer navigation and cleared handoffs instead of restoring that old URL.
    updateSearchParams(params => {
      if (!preserveSurface) params.set("view", params.get("view") === "browser" ? "browser" : "chat");
      if (params.get("handoff") === consumedSelectionHandoff.current) params.delete("handoff");
      params.set("session", id);
    }, { replace: true });
  };
  const [mobileListOpen, setMobileListOpen] = useState(false);
  const [mobileMoreOpen, setMobileMoreOpen] = useState(false);
  const [mobileConversationMenuOpen, setMobileConversationMenuOpen] = useState(false);
  const mobileConversationMenuRef = useRef<HTMLDivElement>(null);
  const mobileConversationActionsRef = useRef<HTMLButtonElement>(null);
  const [fullScreen, setFullScreen] = useState(false);
  const [conversationPanelOpen, setConversationPanelOpen] = useState(
    () => readConversationPanelOpen(localStorage),
  );
  const requestedDrawer = searchParams.get("drawer") ?? "";
  const drawerTab: "context" | "results" | "visuals" | "subagents" =
    requestedDrawer === "results" ? "results"
      : requestedDrawer === "visuals" ? "visuals"
      : requestedDrawer === "subagents" ? "subagents" : "context";
  const sessionInspectorOpen = ["context", "results", "visuals", "subagents"].includes(requestedDrawer);
  const setSessionInspectorOpen = (value: boolean | ((open: boolean) => boolean)) => {
    const open = typeof value === "function" ? value(sessionInspectorOpen) : value;
    updateSearchParams(next => {if(open) next.set("drawer", drawerTab); else next.delete("drawer");});
  };
  const openDrawerMessage = (id: string) => updateSearchParams(next => {next.set("message", id); if(matchMedia("(max-width: 1100px)").matches) next.delete("drawer");});
  const {
    api,
    activeOperator,
    approvals,
    assets,
    coreState,
    createObservation,
    deleteObservation,
    engagement,
    evidence,
    ingestKnowledgeUrlSource,
    knowledgeSources,
    libraryItems,
    providers,
    applyProviderToolSharing,
    refreshProvider,
    reverifyProvider,
    resolveApproval,
    updateProvider,
    setupStatus,
    startMission,
    uploadEvidence,
    updateObservation,
  } = useWorkspace();
  const [executionCapabilities, setExecutionCapabilities] = useState<ExecutionCapabilities>();
  const [browserScope, setBrowserScope] = useState<EngagementScopePolicy>();
  const [browserScopeLoading, setBrowserScopeLoading] = useState(false);
  const conversationMenuRef = useRef<HTMLDetailsElement>(null);
  const [runCandidate, setRunCandidate] = useState<FencedRunCandidate>();
  const [terminalCommandRequest, setTerminalCommandRequest] = useState<{ id: string; source: string }>();
  const [terminalAssistantOpen, setTerminalAssistantOpen] = useState(false);
  // The live shell can also sit beside the chat; the preference is per device.
  const [chatTerminalOpen, setChatTerminalOpenState] = useState(() => {
    try {
      return localStorage.getItem(CHAT_TERMINAL_OPEN_KEY) === "true";
    } catch {
      // diagnostic-expected: storage can be unavailable; the side terminal then starts closed
      return false;
    }
  });
  const setChatTerminalOpen = useCallback((open: boolean) => {
    setChatTerminalOpenState(open);
    try {
      localStorage.setItem(CHAT_TERMINAL_OPEN_KEY, String(open));
    } catch {
      // diagnostic-expected: storage can be unavailable; the choice then lasts for this page only
    }
  }, []);
  const [chatTerminalStacked, setChatTerminalStacked] = useState(() => window.matchMedia(BROWSER_ASSISTANT_SHEET_QUERY).matches);
  useEffect(() => {
    const query = window.matchMedia(BROWSER_ASSISTANT_SHEET_QUERY);
    const update = () => setChatTerminalStacked(query.matches);
    query.addEventListener?.("change", update);
    return () => query.removeEventListener?.("change", update);
  }, []);
  const [executionRefresh, setExecutionRefresh] = useState(0);
  const [sessions, setSessions] = useState<ChatSessionSummary[]>([]);
  const [sessionActivity, setSessionActivity] = useState<Record<string, ChatSessionActivity["state"]>>({});
  const activeEngagementIdRef = useRef(engagement?.id);
  activeEngagementIdRef.current = engagement?.id;
  const [sessionQuery, setSessionQuery] = useState("");
  const [exportingSessionId, setExportingSessionId] = useState<string>();
  const [deletingSessionId, setDeletingSessionId] = useState<string>();
  const [deletingAllSessions, setDeletingAllSessions] = useState(false);
  const [sessionActionsId, setSessionActionsId] = useState<string>();
  const [sessionActionsPosition, setSessionActionsPosition] = useState<{ left: number; openAbove: boolean; top: number }>();
  const [renamingSessionId, setRenamingSessionId] = useState<string>();
  const [renameDraft, setRenameDraft] = useState("");
  const [renameError, setRenameError] = useState<string>();
  const [renamingBusy, setRenamingBusy] = useState(false);
  const [archivingSessionId, setArchivingSessionId] = useState<string>();
  const [archivedGroupOpen, setArchivedGroupOpen] = useState(false);
  const [sessionId, setSessionId] = useState("");
  const [conversationOpen, setConversationOpen] = useState(Boolean(requestedSessionId));
  // Snapshots the assistant published for this conversation. The badge counts
  // what arrived while the operator was not looking; it never steals focus.
  const publishedResults = useStructuredResults(api, engagement?.id, {
    chatSessionId: sessionId,
    live: view === "chat" && Boolean(sessionId),
    limit: 50,
  });
  const { unseen: publishedUnseen } = useUnseenCount(
    publishedResults.items,
    sessionInspectorOpen && drawerTab === "visuals",
  );

  const [providerId, setProviderId] = useState("");
  const [runtimeKind, setRuntimeKind] = useState<"provider" | "harness">("provider");
  const [harnesses, setHarnesses] = useState<HarnessProfile[]>([]);
  // Delegation is opt-in per conversation; Core remembers the choice.
  const [allowSubagents, setAllowSubagents] = useState(false);
  const subagentState = useChatSubagents(api, sessionId, {
    enabled: runtimeKind === "provider" && Boolean(sessionId),
  });
  const subagentResultsByMessage = useMemo(
    () => new Map(
      subagentState.subagents
        .filter((item) => item.resultMessageId)
        .map((item) => [item.resultMessageId!, item]),
    ),
    [subagentState.subagents],
  );

  const [harnessesLoaded, setHarnessesLoaded] = useState(false);
  // Standing tool-sharing consent lives on the runtime profile; mirror the saved
  // answer locally so later turns in this session stop asking too.
  const rememberToolSharingRuntime = useCallback((saved: ToolSharingRuntime) => {
    if (saved.kind === "harness") {
      setHarnesses((current) => current.map((item) => item.id === saved.profile.id ? saved.profile : item));
      return;
    }
    applyProviderToolSharing(saved.profile);
  }, [applyProviderToolSharing]);
  const [harnessSessions, setHarnessSessions] = useState<HarnessSessionSummary[]>([]);
  const [externalHarnessSessions, setExternalHarnessSessions] = useState<ExternalHarnessSessionSummary[]>([]);
  const [externalSessionQuery, setExternalSessionQuery] = useState("");
  const [externalSessionsLoading, setExternalSessionsLoading] = useState(false);
  const [externalSessionsError, setExternalSessionsError] = useState<string>();
  const [harnessActivity, setHarnessActivity] = useState<HarnessSessionActivity>();
  const [harnessActivityError, setHarnessActivityError] = useState<string>();
  const [harnessProgress, setHarnessProgress] = useState<HarnessProgress>();
  const [mcpServers, setMcpServers] = useState<McpServerProfile[]>([]);
  const [harnessId, setHarnessId] = useState("");
  const [harnessSessionId, setHarnessSessionId] = useState("");
  const [harnessMode, setHarnessMode] = useState("");
  const [harnessReasoningEffort, setHarnessReasoningEffort] = useState("");
  // Provider-side reasoning level. "" means the model's own default, which is
  // what most turns want; Core remembers whatever the operator chose here.
  const [reasoningEffort, setReasoningEffort] = useState<ReasoningEffort | "">("");
  const [harnessServiceTier, setHarnessServiceTier] = useState("");
  const [harnessSkills, setHarnessSkills] = useState<HarnessSkillSummary[]>([]);
  const [harnessSkillPath, setHarnessSkillPath] = useState("");
  const [harnessSkillsLoading, setHarnessSkillsLoading] = useState(false);
  const [harnessSkillError, setHarnessSkillError] = useState<string>();
  const [skillToken, setSkillToken] = useState<HarnessSkillTokenRange>();
  const [skillMenuIndex, setSkillMenuIndex] = useState(0);
  const [selectedMcpIds, setSelectedMcpIds] = useState<string[]>([]);
  const [environmentTarget, setEnvironmentTarget] = useState<EnvironmentTarget>("auto");
  const [nativeHooks, setNativeHooks] = useState<NativeHookDescriptor[]>([]);
  // Hooks and skills are files the operator edits in Code or on the host; re-read them
  // when the chat or its settings come back into view instead of trusting the first load.
  const [projectCatalogKey, setProjectCatalogKey] = useState(0);
  const [selectedHookIds, setSelectedHookIds] = useState<string[]>([]);
  const [nativeHookError, setNativeHookError] = useState<string>();
  const [hookExecutions, setHookExecutions] = useState<NativeHookExecution[]>([]);
  const [model, setModel] = useState("");
  const [runtimeSwitchConfirmation, setRuntimeSwitchConfirmation] = useState<string>();
  const runtimeSwitchGenerationRef = useRef(0);
  const [providerModelQuery, setProviderModelQuery] = useState("");
  const [commandRuntimeReady, setCommandRuntimeReady] = useState(false);
  const [toolRuntimeReason, setToolRuntimeReason] = useState<string>();
  const [toolCards, setToolCards] = useState<ToolLifecycleCard[]>([]);
  const [activityItems, setActivityItems] = useState<HarnessActivityItem[]>([]);
  const [harnessInteractions, setHarnessInteractions] = useState<HarnessInteraction[]>([]);
  const [historicalActivityState, setHistoricalActivityState] = useState<Record<string, "loading" | "loaded" | "failed">>({});
  const [historicalActivityErrors, setHistoricalActivityErrors] = useState<Record<string, string>>({});
  const [interactionAnswers, setInteractionAnswers] = useState<Record<string, string>>({});
  const [harnessControlBusy, setHarnessControlBusy] = useState(false);
  const [artifactInspector, setArtifactInspector] = useState<ToolLifecycleCard>();
  const [artifactQuery, setArtifactQuery] = useState("");
  const [artifactSearch, setArtifactSearch] = useState<ToolOutputSearchResult>();
  const [artifactRead, setArtifactRead] = useState<ToolOutputReadResult>();
  const [artifactBusy, setArtifactBusy] = useState(false);
  const [artifactError, setArtifactError] = useState<string>();
  const [pendingResponse, setPendingResponse] = useState<PendingChatResponse>();
  const [waitingCallback, setWaitingCallback] = useState<{ turnId: string; assistantId: string; resultsUrl?: string; processId?: string; toolCallId: string; summary: string }>();
  const [interruptedRecovery, setInterruptedRecovery] = useState<InterruptedChatRecovery>();
  const [failedProviderRecovery, setFailedProviderRecovery] = useState<FailedProviderRecovery>();
  const [recoveryNote, setRecoveryNote] = useState("");
  const [recoveryBusy, setRecoveryBusy] = useState(false);
  useEffect(() => setRecoveryNote(""), [interruptedRecovery?.turn.id]);
  useEffect(() => {
    if (!api || !sessionId || !waitingCallback) return;
    const pollController = new AbortController();
    const selectionGeneration = sessionSelectionGenerationRef.current;
    const request: ChatCompletionRequest = { backend: "provider", sessionId, messages: [], toolsEnabled: true };
    const timer = window.setInterval(() => {
      void api.getPendingChatTurn(sessionId, pollController.signal).then((pending) => {
        if (pollController.signal.aborted || !pending || pending.status === "waiting_callback") return;
        window.clearInterval(timer);
        if (sessionSelectionGenerationRef.current !== selectionGeneration) return;
        // The follow owns the viewer transport (not the poll controller, which
        // this effect aborts as soon as the callback resolves), so switching
        // conversations detaches it and its replayed events stay out of the
        // next transcript.
        const stream = beginGuardedStream({ generation: sessionSelectionGenerationRef, abort: abortRef, backend: streamBackendRef }, "provider");
        setSending(true);
        void api.followChatTurn(
          waitingCallback.turnId,
          request,
          stream.guard((streamEvent) => applyChatEvent(streamEvent, waitingCallback.assistantId, "", request)),
          stream.controller.signal,
        ).catch((error) => {
          void logCaughtDiagnostic("interface.sessions_page.callback_follow_failed", "The response could not be followed after the command posted its results.", error, "sessions_page");
          if (stream.isCurrent() && !stream.controller.signal.aborted) setChatError(error instanceof Error ? error.message : "Could not follow the resumed response.");
        }).finally(() => {
          stream.release();
          if (stream.isCurrent()) setSending(false);
        });
      }).catch(() => { /* diagnostic-expected: callback wait retries until Core resumes. */ });
    }, 1500);
    return () => { pollController.abort(); window.clearInterval(timer); };
  }, [api, sessionId, waitingCallback?.turnId]);
  const [approvalDecisionBusy, setApprovalDecisionBusy] = useState(false);
  const [resolvedApproval, setResolvedApproval] = useState<{ id: string; status: string; turnId: string; harnessTurnId?: string }>();
  const {state: authoritativeState, error: stateSyncError, refresh: refreshSessionState} = useSessionState(api ?? undefined, sessionId, coreState === "online");
  const pendingResponseActive = Boolean(pendingResponse && isPendingRequest(authoritativeState, pendingResponse.approval.id));
  const approvalRestorationRef = useRef<string | undefined>(undefined);
  const [messages, setMessages] = useState<ConversationMessage[]>([]);
  const [replacedMessages, setReplacedMessages] = useState<PersistedChatMessage[]>([]);
  const [messageEdit, setMessageEdit] = useState<{messageId: string; sequence?: number; text: string; busy: boolean; error?: string}>();
  const [draft, setDraft] = useState("");
  const [queuedFollowUps, setQueuedFollowUps] = useState<ChatFollowUp[]>([]);
  const coreQueue = useChatQueue(api, sessionId);
  const [providerGoal, setProviderGoal] = useState<ChatGoal>();
  const [liveGoalTokenEstimate, setLiveGoalTokenEstimate] = useState(0);
  const [providerGoalLoading, setProviderGoalLoading] = useState(false);
  const [providerGoalError, setProviderGoalError] = useState<string>();
  const [decisionSeed, setDecisionSeed] = useState<DecisionSeed>();
  const [expandedContextIndex, setExpandedContextIndex] = useState<number>();
  const [contextStatus, setContextStatus] = useState<ContextStatus>();
  const [contextStatusError, setContextStatusError] = useState<string>();
  const [contextStatusLoading, setContextStatusLoading] = useState(false);
  const [contextRefreshKey, setContextRefreshKey] = useState(0);
  const [messageActionStatus, setMessageActionStatus] = useState<string>();
  const [pendingImages, setPendingImages] = useState<PendingChatImage[]>([]);
  const [uploadingImage, setUploadingImage] = useState(false);
  const [sending, setSending] = useState(false);
  const [loadingHistory, setLoadingHistory] = useState(false);
  const [sessionReadReady, setSessionReadReady] = useState(true);
  const chatPreviews = useMemo(() => new ChatPreviewCache<{
    messages: ConversationMessage[];
    toolCards: ToolLifecycleCard[];
    scrollTop: number;
    followBottom: boolean;
  }>(), [api, engagement?.id]);
  const previewOwnerRef = useRef("");
  const restoredScrollRef = useRef<{scrollTop: number; followBottom: boolean} | undefined>(undefined);
  const [reloadingConversation, setReloadingConversation] = useState(false);
  const [chatError, setChatError] = useState<string>();
  const [chatReconnecting, setChatReconnecting] = useState(false);
  const [assistantSettingsOpen, setAssistantSettingsOpen] = useState(false);
  useGuideAction("open-assistant-settings", () => setAssistantSettingsOpen(true));
  const [assistantSettingsStatus, setAssistantSettingsStatus] = useState("");
  const [assistantSettingsError, setAssistantSettingsError] = useState<string>();
  const [assistantSettingsBusy, setAssistantSettingsBusy] = useState(false);
  const [discoveringProviderId, setDiscoveringProviderId] = useState<string>();
  const abortRef = useRef<AbortController | undefined>(undefined);
  const streamBackendRef = useRef<ChatCompletionRequest["backend"] | undefined>(undefined);
  const activeProviderTurnIdRef = useRef<string | undefined>(undefined);
  useEffect(() => {
    if (runtimeKind !== "provider" || authoritativeState?.execution !== "cancelled") return;
    const activeTurnId = activeProviderTurnIdRef.current;
    if (activeTurnId && authoritativeState.turn_id && activeTurnId !== authoritativeState.turn_id) return;
    // Core is authoritative even when another tab stopped the turn or this
    // viewer missed the final stream event. Never leave a terminal turn's
    // temporary bubble presenting itself as active provider work.
    abortRef.current?.abort();
    setMessages((current) => cancelActiveAssistantMessage(current));
    setPendingResponse(undefined);
    setWaitingCallback(undefined);
    setChatReconnecting(false);
    setSending(false);
    activeProviderTurnIdRef.current = undefined;
  }, [runtimeKind, authoritativeState?.execution, authoritativeState?.revision, authoritativeState?.turn_id]);
  const detachedStreamsRef = useRef(new WeakSet<AbortController>());
  const harnessFollowDetachRef = useRef<(() => void) | undefined>(undefined);
  const composerRef = useRef<HTMLTextAreaElement>(null);
  useChatComposerAnchor(composerRef, (view === "chat" || view === "browser") && conversationOpen, view === "browser" && window.matchMedia(BROWSER_ASSISTANT_SHEET_QUERY).matches ? "browser-sheet" : view);
  const imageInputRef = useRef<HTMLInputElement>(null);
  const chatViewportRef = useRef<HTMLDivElement>(null);
  const chatNavigation = useChatNavigation(api ?? undefined, engagement?.id, sessionId);
  useEffect(() => {
    const messageId = searchParams.get("message");
    if (!messageId || loadingHistory) return;
    const element = document.getElementById(`chat-message-${messageId}`);
    if (element) { chatFollowBottomRef.current = false; element.scrollIntoView({block: "center"}); element.focus({preventScroll: true}); }
  }, [searchParams, loadingHistory, messages.length]);
  const chatFollowBottomRef = useRef(true);
  const chatScrollGeometryRef = useRef<ChatScrollGeometry | undefined>(undefined);
  const chatReadingPositionRef = useRef<{sessionId: string; scrollTop: number; followBottom: boolean}>({sessionId: "", scrollTop: 0, followBottom: true});
  const [hasNewerMessages, setHasNewerMessages] = useState(false);
  const chatTouchYRef = useRef<number | undefined>(undefined);
  const previousChatSendingRef = useRef(false);
  const providerGoalSettledRef = useRef(false);

  // Core advances a goal on every turn it dispatches — step, usage, elapsed
  // time and therefore its revision — so a goal read once at open is stale as
  // soon as the model works. Reading it again is what keeps the panel honest
  // and keeps an operator's Pause/Cancel from failing on a stale revision.
  const readProviderGoal = useCallback(async (signal?: AbortSignal): Promise<ChatGoal | undefined> => {
    if (!api || !sessionId || runtimeKind !== "provider") return undefined;
    try {
      const goal = await api.getChatGoal(sessionId, signal);
      setProviderGoal(goal);
      setProviderGoalError(undefined);
      return goal;
    } catch (caught) {
      if (signal?.aborted) return undefined; // diagnostic-expected: superseded by a newer session load
      if (caught instanceof ApiError && caught.status === 404) {
        setProviderGoal(undefined); // diagnostic-expected: this conversation has no goal yet
        return undefined;
      }
      void logCaughtDiagnostic("interface.sessions.goal_load_failed", "The conversation goal could not be loaded.", caught, "goal");
      setProviderGoalError(caught instanceof Error ? caught.message : "Goal could not be loaded.");
      return undefined;
    }
  }, [api, runtimeKind, sessionId]);

  useEffect(() => {
    if (!api || !sessionId || runtimeKind !== "provider") {
      setProviderGoal(undefined); setLiveGoalTokenEstimate(0); setProviderGoalError(undefined); setProviderGoalLoading(false); return;
    }
    const controller = new AbortController();
    setProviderGoalLoading(true); setProviderGoalError(undefined);
    void readProviderGoal(controller.signal).finally(() => { if (!controller.signal.aborted) setProviderGoalLoading(false); });
    return () => controller.abort();
  }, [api, readProviderGoal, runtimeKind, sessionId]);

  // A running goal changes while the model works, and again when the turn
  // ends. Both are read back so the panel's numbers and its revision track
  // Core rather than the moment the conversation was opened.
  useEffect(() => {
    if (!api || !sessionId || runtimeKind !== "provider") return;
    if (providerGoal?.status !== "running" && !sending) return;
    const controller = new AbortController();
    const timer = window.setInterval(() => void readProviderGoal(controller.signal), GOAL_REFRESH_MS);
    return () => { controller.abort(); window.clearInterval(timer); };
  }, [api, providerGoal?.status, readProviderGoal, runtimeKind, sending, sessionId]);

  useEffect(() => {
    if (!providerGoalSettledRef.current) { providerGoalSettledRef.current = true; return; }
    if (sending || !providerGoal) return;
    const controller = new AbortController();
    void readProviderGoal(controller.signal);
    return () => controller.abort();
    // A finished turn is the moment the goal's step and usage are final.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sending]);
  const lastModelDiscoveryProviderIdRef = useRef<string | undefined>(undefined);
  const attemptedToolVerificationRef = useRef(new Set<string>());
  const runtimeDefaultEngagementRef = useRef<string | undefined>(undefined);
  const explicitNewConversationRef = useRef(false);
  const pendingSessionNavigationRef = useRef<string | undefined>(undefined);
  const sessionSelectionGenerationRef = useRef(0);
  const reconciledTerminalHarnessTurnsRef = useRef(new Set<string>());
  const reconcilingTerminalHarnessTurnsRef = useRef(new Set<string>());
  const sessionLoadAbortRef = useRef<AbortController | undefined>(undefined);
  const historicalActivityAbortRef = useRef(new Map<string, AbortController>());
  const assistantSettingsButtonRef = useRef<HTMLButtonElement>(null);
  const assistantSettingsPanelRef = useRef<HTMLElement>(null);
  const sessionActionsButtonRef = useRef<HTMLButtonElement>(null);
  const sessionActionsMenuRef = useRef<HTMLDivElement>(null);
  const streamDeltaRef = useRef(new Map<string, string>());
  const streamReasoningRef = useRef(new Map<string, string>());
  const liveGoalStreamTextRef = useRef("");
  const streamFrameRef = useRef<number | undefined>(undefined);
  const draftStorageKeyRef = useRef("");
  const followUpStorageKeyRef = useRef("");
  const followUpAutoDrainRef = useRef(false);
  const followUpDrainIdRef = useRef<string | undefined>(undefined);
  const chatRuntimeStore = useMemo(() => ({
    messages,
    convertMessage: convertConversationMessage,
    isLoading: loadingHistory,
    isRunning: sending,
    onNew: async () => undefined,
  }), [loadingHistory, messages, sending]);
  const chatRuntime = useExternalStoreRuntime(chatRuntimeStore);
  useLayoutEffect(() => {
    chatScrollGeometryRef.current = undefined;
    const position = restoredScrollRef.current;
    chatFollowBottomRef.current = position?.followBottom ?? true;
    setHasNewerMessages(!chatFollowBottomRef.current);
    if (!position) return;
    const restore = () => {
      chatFollowBottomRef.current = position.followBottom;
      setHasNewerMessages(!position.followBottom);
      if (chatViewportRef.current && !position.followBottom) chatViewportRef.current.scrollTop = position.scrollTop;
    };
    restore();
    // The external thread store commits its message rows after the parent render.
    const frame = requestAnimationFrame(restore);
    return () => cancelAnimationFrame(frame);
  }, [conversationOpen, sessionId]);
  useLayoutEffect(() => {
    const runStarted = sending && !previousChatSendingRef.current;
    previousChatSendingRef.current = sending;
    if (runStarted) chatFollowBottomRef.current = true;
    if ((view !== "chat" && view !== "browser") || !conversationOpen || !chatFollowBottomRef.current) return;
    const viewport = chatViewportRef.current;
    if (!viewport) return;
    const scrollToLatest = () => {
      if (chatFollowBottomRef.current) viewport.scrollTop = viewport.scrollHeight;
    };
    scrollToLatest();
    // Message rows and rich content can finish laying out after the parent frame.
    const observer = new ResizeObserver(scrollToLatest);
    observer.observe(viewport);
    const observeRows = () => {
      observer.disconnect();
      observer.observe(viewport);
      for (const child of viewport.children) observer.observe(child);
      scrollToLatest();
    };
    const rowsObserver = new MutationObserver(observeRows);
    rowsObserver.observe(viewport, {childList: true});
    observeRows();
    const frame = globalThis.requestAnimationFrame?.(scrollToLatest);
    return () => {
      rowsObserver.disconnect();
      observer.disconnect();
      if (frame !== undefined) globalThis.cancelAnimationFrame?.(frame);
    };
  }, [conversationOpen, messages, sending, sessionId, view]);
  useEffect(() => {
    if (!import.meta.env.DEV || (view !== "chat" && view !== "browser") || !conversationOpen || !chatViewportRef.current) return;
    return attachChatScrollTrace(chatViewportRef.current);
  }, [conversationOpen, sessionId, view]);
  const messagesById = useMemo(
    () => new Map(messages.map((message) => [message.runtimeId ?? message.id, message])),
    [messages],
  );
  const activeDraftStorageKey = engagement
    ? chatDraftStorageKey(engagement.id, sessionId || undefined)
    : "";
  const activeFollowUpStorageKey = engagement
    ? chatFollowUpStorageKey(engagement.id, sessionId || undefined)
    : "";
  useEffect(() => {
    if (!activeDraftStorageKey || draftStorageKeyRef.current !== activeDraftStorageKey) return;
    writeChatDraft(sessionStorage, activeDraftStorageKey, draft);
  }, [activeDraftStorageKey, draft]);
  useEffect(() => {
    const previousKey = draftStorageKeyRef.current;
    if (previousKey && previousKey !== activeDraftStorageKey) {
      writeChatDraft(sessionStorage, previousKey, draft);
    }
    draftStorageKeyRef.current = activeDraftStorageKey;
    setDraft(activeDraftStorageKey ? readChatDraft(sessionStorage, activeDraftStorageKey) : "");
    setSkillToken(undefined);
    setHarnessSkillPath("");
  // The outgoing draft is intentionally captured at the identity boundary.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeDraftStorageKey]);
  useEffect(() => {
    if (!activeFollowUpStorageKey || followUpStorageKeyRef.current !== activeFollowUpStorageKey) return;
    writeChatFollowUps(sessionStorage, activeFollowUpStorageKey, queuedFollowUps);
  }, [activeFollowUpStorageKey, queuedFollowUps]);
  useEffect(() => {
    const previousKey = followUpStorageKeyRef.current;
    let recovered = activeFollowUpStorageKey ? readChatFollowUps(sessionStorage, activeFollowUpStorageKey) : [];
    const movingFromUnattachedConversation = Boolean(
      previousKey
      && activeFollowUpStorageKey
      && previousKey.endsWith(":new")
      && !activeFollowUpStorageKey.endsWith(":new"),
    );
    if (movingFromUnattachedConversation) {
      const unattached = readChatFollowUps(sessionStorage, previousKey);
      if (unattached.length) {
        recovered = [...recovered, ...unattached]
          .sort((left, right) => left.createdAt.localeCompare(right.createdAt))
          .slice(0, maxChatFollowUps());
        writeChatFollowUps(sessionStorage, activeFollowUpStorageKey, recovered);
        clearChatFollowUps(sessionStorage, previousKey);
      }
    }
    followUpStorageKeyRef.current = activeFollowUpStorageKey;
    setQueuedFollowUps(recovered);
    if (activeFollowUpStorageKey !== previousKey && !movingFromUnattachedConversation) {
      // Queued items recovered after navigation or reload require an explicit
      // operator action. Only items accepted during this mounted active turn
      // are drained automatically.
      followUpAutoDrainRef.current = false;
      followUpDrainIdRef.current = undefined;
    }
  }, [activeFollowUpStorageKey]);
  const visibleSessions = useMemo(() => {
    const query = sessionQuery.trim().toLocaleLowerCase();
    if (!query) return sessions;
    return sessions.filter((session) => [
      session.title,
      session.model,
      session.backend === "harness" ? "agent harness" : "provider",
    ].some((value) => value?.toLocaleLowerCase().includes(query)));
  }, [sessionQuery, sessions]);
  const groupedSessions = useMemo(() => {
    const startOfToday = new Date();
    startOfToday.setHours(0, 0, 0, 0);
    const weekAgo = Date.now() - 7 * 24 * 60 * 60 * 1_000;
    const groups = new Map<string, ChatSessionSummary[]>();
    for (const session of visibleSessions) {
      const activity = sessionActivity[session.id] ?? "idle";
      const updated = Date.parse(session.updatedAt);
      const label = session.archivedAt ? "Archived"
        : activity === "waiting" ? "Needs you"
        : activity === "working" ? "Working"
          : updated >= startOfToday.getTime() ? "Today"
            : updated >= weekAgo ? "Previous 7 days" : "Older";
      groups.set(label, [...(groups.get(label) ?? []), session]);
    }
    return ["Needs you", "Working", "Today", "Previous 7 days", "Older", "Archived"]
      .flatMap(label => groups.has(label) ? [{label, sessions: groups.get(label)!}] : []);
  }, [sessionActivity, visibleSessions]);
  const activeArchivedSession = sessionId ? sessions.find((item) => item.id === sessionId && item.archivedAt) : undefined;
  const activeChatSession = sessionId ? sessions.find((item) => item.id === sessionId) : undefined;
  useEffect(() => {
    if (!activeChatSession || activeChatSession.backend !== "provider") return;
    setSelectedMcpIds(activeChatSession.mcpServerIds);
    setSelectedHookIds(activeChatSession.hookIds);
    setReasoningEffort(activeChatSession.reasoningEffort ?? "");
    setAllowSubagents(activeChatSession.allowSubagents === true);
  }, [activeChatSession?.id, activeChatSession?.revision]);
  const activeContextStatus = contextStatus?.ownerId === sessionId ? contextStatus : undefined;
  const contextPercent = activeContextStatus && activeContextStatus.status !== "runtime_managed" && activeContextStatus.targetInputTokens > 0
    ? Math.min(100, Math.round((activeContextStatus.estimatedInputTokens / activeContextStatus.targetInputTokens) * 100))
    : undefined;
  const contextCapacityLabel = !activeContextStatus ? ""
    : activeContextStatus.routeLimitsRequired && activeContextStatus.routeLimitsVerified
      ? `${activeContextStatus.eligibleRouteCount ?? 0} compatible routes · ${(activeContextStatus.routeContextWindow ?? activeContextStatus.contextWindow).toLocaleString()} route minimum · ${(activeContextStatus.routeInputLimit ?? activeContextStatus.targetInputTokens).toLocaleString()} input ceiling`
      : activeContextStatus.routeLimitsRequired
        ? `route limits unverified · safe ${activeContextStatus.contextWindow.toLocaleString()}-token ceiling`
        : activeContextStatus.capacitySource === "model_catalog"
          ? "exact model catalog"
          : activeContextStatus.capacitySource === "known_model"
            ? "published model limits"
            : activeContextStatus.capacitySource === "configured"
              ? "configured estimate"
              : "safe fallback estimate";
  const enabledProviders = useMemo(() => providers.filter((provider) => provider.enabled), [providers]);
  const selectedProvider = enabledProviders.find((provider) => provider.id === providerId);
  const selectedHarness = harnesses.find((harness) => harness.id === harnessId);
  const runtimeReady = runtimeKind === "provider" ? Boolean(selectedProvider) : Boolean(selectedHarness);
  useEffect(() => {
    if (!engagement || !runtimeReady) return;
    registerAssistantSnapshot({
      engagementId: engagement.id, sessionId: sessionId || undefined,
      backend: runtimeKind, providerId: runtimeKind === "provider" ? providerId : undefined,
      harnessProfileId: runtimeKind === "harness" ? harnessId : undefined, model: model.trim(),
      includeKnowledge: false,
    });
  }, [engagement?.id, sessionId, runtimeKind, providerId, harnessId, model, runtimeReady, registerAssistantSnapshot]);
  const selectedHarnessSkill = harnessSkills.find((skill) => skill.path === harnessSkillPath);
  const matchingHarnessSkills = useMemo(() => {
    const query = skillToken?.query.toLocaleLowerCase() ?? "";
    return harnessSkills.filter((skill) => skill.name.toLocaleLowerCase().includes(query));
  }, [harnessSkills, skillToken?.query]);
  const selectedHarnessSession = harnessSessions.find((item) => item.id === harnessSessionId);
  const selectedHarnessModelOptions = (selectedHarness?.modelOptions ?? []).find((item) => item.model === model);
  const harnessModelOptions = [...new Set([
    ...(selectedHarness?.models ?? []),
    ...(selectedHarnessSession ? [selectedHarnessSession.model] : []),
    ...(runtimeKind === "harness" && model ? [model] : []),
  ])];
  const harnessReasoningEfforts = selectedHarnessModelOptions?.reasoningEfforts ?? [];
  const harnessServiceTiers = selectedHarnessModelOptions?.serviceTiers ?? [];
  const providerIsLocal = selectedProvider?.kind === "local" || selectedProvider?.privacy === "local_only";
  const imageInputEnabled = runtimeKind === "provider"
    ? Boolean(selectedProvider?.capabilities.includes("vision"))
    : Boolean(selectedHarnessModelOptions?.imageInput);
  const harnessIsLocal = selectedHarness?.localOnly === true;
  const runtimePermitsKnowledge = runtimeKind === "harness"
    ? harnessIsLocal || selectedHarness?.permitsSensitiveData === true
    : providerIsLocal || selectedProvider?.permitsSensitiveData === true;
  const knowledgeItemCount = knowledgeSources.length + libraryItems.length;
  const canUseKnowledge = Boolean(knowledgeItemCount > 0 && runtimePermitsKnowledge);
  const modelVerification = providerModelVerification(selectedProvider, model);
  const modelVerified = modelVerification?.status === "verified";
  const commandRuntimeAvailable = Boolean(modelVerified && commandRuntimeReady && !toolRuntimeReason);
  const defaultRuntime = useMemo(
    () => defaultModelRuntime(enabledProviders, harnesses),
    [enabledProviders, harnesses],
  );

  const applyDefaultRuntime = () => {
    if (!defaultRuntime) return false;
    setRuntimeKind(defaultRuntime.kind);
    setModel(defaultRuntime.model);
    setHarnessSessionId("");
    setSelectedMcpIds([]);
    setSelectedHookIds([]);
    if (defaultRuntime.kind === "harness") setHarnessId(defaultRuntime.id);
    else setProviderId(defaultRuntime.id);
    return true;
  };

  const detachActiveChatStream = () => {
    setChatReconnecting(false);
    const controller = abortRef.current;
    if (!detachChatStream(
      controller,
      streamBackendRef.current,
      detachedStreamsRef.current,
    )) return false;
    abortRef.current = undefined;
    streamBackendRef.current = undefined;
    activeProviderTurnIdRef.current = undefined;
    return true;
  };
  const commandRuntimeReason = !modelVerified
    ? model ? `Tool calling is unverified for ${model}.` : "Select a model to verify tool calling."
    : toolRuntimeReason ?? (!commandRuntimeReady ? "The command runtime is not ready." : undefined);
  const canUseTools = commandRuntimeAvailable;
  const commandRuntimeUnavailableReason = commandRuntimeReason;

  useEffect(() => {
    if (!fullScreen && !mobileMoreOpen) return;
    const exitFullScreen = (event: globalThis.KeyboardEvent) => {
      if (event.key !== "Escape") return;
      event.preventDefault();
      if (mobileMoreOpen) setMobileMoreOpen(false);
      else setFullScreen(false);
    };
    window.addEventListener("keydown", exitFullScreen);
    return () => window.removeEventListener("keydown", exitFullScreen);
  }, [fullScreen, mobileMoreOpen]);

  useEffect(() => {
    if (!mobileConversationMenuOpen && !mobileListOpen) return;
    const closeOnOutsidePointer = (event: PointerEvent) => {
      if (!mobileConversationMenuOpen || !(event.target instanceof Node) || mobileConversationMenuRef.current?.contains(event.target)) return;
      setMobileConversationMenuOpen(false);
    };
    const closeOnEscape = (event: globalThis.KeyboardEvent) => {
      if (event.key !== "Escape" || event.defaultPrevented) return;
      if (mobileConversationMenuOpen) {
        setMobileConversationMenuOpen(false);
        return;
      }
      // Rename fields, row menus and confirmations inside the drawer own their Escape.
      if (event.target instanceof Element && event.target.closest("input, textarea, [role=menu], [role=dialog]")) return;
      if (compact) setMobileListOpen(false);
    };
    document.addEventListener("pointerdown", closeOnOutsidePointer);
    document.addEventListener("keydown", closeOnEscape);
    return () => {
      document.removeEventListener("pointerdown", closeOnOutsidePointer);
      document.removeEventListener("keydown", closeOnEscape);
    };
  }, [compact, mobileConversationMenuOpen, mobileListOpen]);

  useLayoutEffect(() => {
    if (!window.matchMedia("(max-width: 760px)").matches) return;
    document.querySelector<HTMLElement>(`.session-tabs button[aria-selected="true"]`)
      ?.scrollIntoView({ block: "nearest", inline: "nearest" });
  }, [view]);

  useEffect(() => {
    if (runtimeKind !== "provider" || coreState !== "online" || (view !== "chat" && view !== "browser") || !selectedProvider || !model.trim() || modelVerification) return;
    const key = `${selectedProvider.id}:${model.trim()}`;
    if (attemptedToolVerificationRef.current.has(key)) return;
    attemptedToolVerificationRef.current.add(key);
    void reverifyProvider(selectedProvider.id, model).catch((caughtError) => { void logCaughtDiagnostic("interface.sessions_page.caught_failure_01", "A handled interface operation failed.", caughtError, "sessions_page"); return undefined; });
  }, [coreState, model, modelVerification, reverifyProvider, runtimeKind, selectedProvider, view]);

  useEffect(() => {
    if (!assistantDrafts.length) return;
    if (view === "terminal") setTerminalAssistantOpen(true);
    setExpandedContextIndex(undefined);
    setConversationOpen(true);
    globalThis.requestAnimationFrame?.(() => composerRef.current?.focus());
  }, [assistantDrafts, view]);

  useComposerAutosize(composerRef, draft, CHAT_COMPOSER_MAX_HEIGHT, `${view}:${conversationOpen}:${sessionId ?? "new"}`);

  useEffect(() => {
    if (!executionDraft) return;
    let active = true;
    void sha256(executionDraft.text).then((sourceSha256) => {
      if (!active) return;
      setRunCandidate({
        source: executionDraft.text,
        language: "bash",
        declaredLanguage: "bash",
        origin: {
          kind: "selection",
          sourceKind: executionDraft.source.kind,
          sourceId: executionDraft.source.id,
          sourceLabel: executionDraft.source.label,
          sourceSha256,
        },
      });
      clearExecutionDraft();
    });
    return () => { active = false; };
  }, [clearExecutionDraft, executionDraft]);

  useEffect(() => {
    if (!api || coreState !== "online" || !engagement) {
      setCommandRuntimeReady(false);
      setToolRuntimeReason("Command runtime configuration is unavailable.");
      return;
    }
    let active = true;
    void api.getAutomationRuntime(undefined, engagement?.id).then((runtime) => {
      if (!active) return;
      setCommandRuntimeReady(runtime.ready);
      setToolRuntimeReason(runtime.ready ? undefined : runtime.detail);
    }).catch((caughtError) => {
      void logCaughtDiagnostic("interface.sessions_page.caught_failure_02", "A handled interface operation failed.", caughtError, "sessions_page");
      if (!active) return;
      setCommandRuntimeReady(false);
      setToolRuntimeReason("Command runtime configuration is unavailable.");
    });
    return () => { active = false; };
  }, [api, coreState, engagement]);

  useEffect(() => {
    const controller = new AbortController();
    if (!api || !engagement || !assistantSettingsOpen || runtimeKind !== "harness" || !harnessId) {
      setExternalHarnessSessions([]);
      setExternalSessionsError(undefined);
      setExternalSessionsLoading(false);
      return () => controller.abort();
    }
    setExternalSessionsLoading(true);
    setExternalSessionsError(undefined);
    void api.listExternalHarnessSessions(harnessId, engagement.id, controller.signal)
      .then(items => {
        setExternalHarnessSessions(items);
        const namesByInternalId = new Map(items
          .filter(item => item.internalSessionId)
          .map(item => [item.internalSessionId as string, item.displayName]));
        setHarnessSessions(current => current.map(item => {
          const reconciledName = namesByInternalId.get(item.id);
          return reconciledName ? {...item, displayName: reconciledName} : item;
        }));
      })
      .catch(error => {
        if (controller.signal.aborted) return;
        void logCaughtDiagnostic("interface.sessions_page.external_sessions_load_failed", "External harness sessions could not be loaded.", error, "sessions_page");
        setExternalHarnessSessions([]);
        setExternalSessionsError(error instanceof Error ? error.message : "External sessions could not be loaded.");
      })
      .finally(() => { if (!controller.signal.aborted) setExternalSessionsLoading(false); });
    return () => controller.abort();
  }, [api, engagement, assistantSettingsOpen, runtimeKind, harnessId]);

  const selectHarnessSession = async (value: string) => {
    if (!value.startsWith("external:")) {
      setHarnessSessionId(value);
      return;
    }
    if (!api || !engagement || !harnessId) return;
    const externalId = value.slice("external:".length);
    const external = externalHarnessSessions.find(item => item.externalSessionId === externalId);
    if (!external) return;
    setExternalSessionsLoading(true);
    setExternalSessionsError(undefined);
    try {
      const imported = await api.importExternalHarnessSession(harnessId, {
        engagementId: engagement.id,
        externalSessionId: external.externalSessionId,
        displayName: external.displayName,
        model: model || external.model,
      });
      setHarnessSessions(current => [imported, ...current.filter(item => item.id !== imported.id)]);
      setExternalHarnessSessions(current => current.map(item => item.externalSessionId === externalId
        ? {...item, internalSessionId: imported.id}
        : item));
      setHarnessSessionId(imported.id);
      setAssistantSettingsStatus(`Ready to resume ${external.displayName}.`);
    } catch (error) {
      void logCaughtDiagnostic("interface.sessions_page.external_session_import_failed", "An external harness session could not be imported.", error, "sessions_page");
      setExternalSessionsError(error instanceof Error ? error.message : "The external session could not be imported.");
    } finally {
      setExternalSessionsLoading(false);
    }
  };

  useEffect(() => {
    if (!api || coreState !== "online" || !engagement || view !== "browser") {
      setBrowserScope(undefined);
      setBrowserScopeLoading(false);
      return;
    }
    let active = true;
    setBrowserScope(undefined);
    setBrowserScopeLoading(true);
    void api.getEngagementScope(engagement.id).then((next) => {
      if (active) setBrowserScope(next);
    }).catch((caughtError) => {
      void logCaughtDiagnostic("interface.workbench_browser.scope_load_failed", "Project scope could not be loaded for the Browser.", caughtError, "workbench_browser");
      if (active) setBrowserScope(undefined);
    }).finally(() => {
      if (active) setBrowserScopeLoading(false);
    });
    return () => { active = false; };
  }, [api, coreState, engagement?.id, view]);

  useEffect(() => {
    if (!api || coreState !== "online" || runtimeKind !== "harness" || !harnessSessionId) {
      setHarnessActivity(undefined);
      setHarnessActivityError(undefined);
      return;
    }
    let active = true;
    const controller = new AbortController();
    const refresh = async () => {
      try {
        const next = await api.getHarnessSessionActivity(harnessSessionId, controller.signal);
        if (!active) return;
        setHarnessActivity((current) => isSameHarnessSessionActivity(current, next) ? current : next);
        setHarnessActivityError(undefined);
      } catch (error) {
        if (!active || controller.signal.aborted) return;
        void logCaughtDiagnostic("interface.sessions_page.harness_activity", "Harness session activity could not be refreshed.", error, "sessions_page");
        setHarnessActivityError(error instanceof Error ? error.message : "Harness activity is unavailable.");
      }
    };
    void refresh();
    const interval = globalThis.setInterval(() => void refresh(), 2_000);
    return () => {
      active = false;
      controller.abort();
      globalThis.clearInterval(interval);
    };
  }, [api, coreState, harnessSessionId, runtimeKind]);

  useEffect(() => {
    if (!api || !sessionId || !harnessActivity?.lastTurnId
      || harnessActivity.lastTurnOrigin !== "chat" || harnessActivity.lastTurnStatus !== "complete") return;
    const turnId = harnessActivity.lastTurnId;
    if (sending && harnessProgress?.turnId !== turnId) return;
    const key = `${sessionId}:${turnId}`;
    if (reconciledTerminalHarnessTurnsRef.current.has(key)
      || reconcilingTerminalHarnessTurnsRef.current.has(key)) return;
    const generation = sessionSelectionGenerationRef.current;
    reconcilingTerminalHarnessTurnsRef.current.add(key);
    void api.listChatMessages(sessionId).then(async authoritative => {
      if (!authoritative.some(message => message.role === "assistant"
        && message.harnessTurnId === turnId)) return;
      const recovered = await recoverHarnessHistory(
        authoritative.map(persistedMessage), turnId => api.getHarnessTurn(turnId),
      );
      if (sessionSelectionGenerationRef.current !== generation) return;
      setMessages(recovered);
      if (harnessProgress?.turnId === turnId) {
        setSending(false);
        setChatReconnecting(false);
      }
      reconciledTerminalHarnessTurnsRef.current.add(key);
    }).catch(error => {
      void logCaughtDiagnostic("interface.sessions_page.terminal_harness_reconcile_failed",
        "A completed harness turn could not be read from Core.", error, "sessions_page");
    }).finally(() => reconcilingTerminalHarnessTurnsRef.current.delete(key));
  }, [api, sessionId, harnessActivity, sending, harnessProgress?.turnId]);

  useEffect(() => {
    if (!harnessActivity) return;
    if (resolvedApproval?.harnessTurnId === harnessActivity.turnId && harnessActivity.turnStatus
      && ["running", "finalizing", "complete", "failed", "cancelled", "interrupted"].includes(harnessActivity.turnStatus)) {
      setResolvedApproval(undefined);
      setHarnessProgress(current => current?.phase === "decision_recorded" ? undefined : current);
    }
    if (harnessActivity.live) return;
    const terminal = harnessActivity.turnStatus
      ? ["complete", "failed", "cancelled", "interrupted"].includes(harnessActivity.turnStatus)
      : !harnessActivity.busy;
    if (!terminal) return;
    setResolvedApproval(undefined);
    detachActiveChatStream();
    harnessFollowDetachRef.current?.();
    harnessFollowDetachRef.current = undefined;
    setHarnessProgress(undefined);
    setPendingResponse(undefined);
    setInterruptedRecovery(undefined);
    setSending(false);
  }, [harnessActivity, resolvedApproval]);

  useEffect(() => {
    let active = true;
    if (!api || coreState !== "online" || !engagement) {
      setHarnesses([]);
      setHarnessesLoaded(false);
      setHarnessSessions([]);
      setMcpServers([]);
      return () => { active = false; };
    }
    setHarnessesLoaded(false);
    void api.listHarnesses().then((nextHarnesses) => {
      if (!active) return;
      const enabled = nextHarnesses.filter((item) => item.enabled);
      setHarnesses(enabled);
      setHarnessesLoaded(true);
      setHarnessId((current) => enabled.some((item) => item.id === current) ? current : enabled[0]?.id ?? "");
    }).catch((caughtError) => {
      void logCaughtDiagnostic("interface.sessions_page.caught_failure_03", "A handled interface operation failed.", caughtError, "sessions_page");
      if (active) { setHarnesses([]); setHarnessesLoaded(true); }
    });
    void api.listHarnessSessions(engagement.id).then((nextSessions) => {
      if (active) setHarnessSessions(nextSessions.filter((item) => item.status !== "closed"));
    }).catch((caughtError) => {
      void logCaughtDiagnostic("interface.sessions_page.harness_sessions", "Harness sessions could not be loaded.", caughtError, "sessions_page");
      if (active) setHarnessSessions([]);
    });
    void api.listMcpServers().then((nextServers) => {
      if (active) setMcpServers(nextServers.filter((item) => item.enabled));
    }).catch((caughtError) => {
      void logCaughtDiagnostic("interface.sessions_page.mcp_servers", "MCP servers could not be loaded.", caughtError, "sessions_page");
      if (active) setMcpServers([]);
    });
    return () => { active = false; };
  }, [api, coreState, engagement]);

  useEffect(() => {
    if (
      !harnessesLoaded
      || !engagement
      || sessionId
      || requestedSessionId
      || runtimeDefaultEngagementRef.current === engagement.id
    ) return;
    if (applyDefaultRuntime()) runtimeDefaultEngagementRef.current = engagement.id;
  }, [defaultRuntime, engagement, harnessesLoaded, requestedSessionId, sessionId]);

  useEffect(() => {
    if (runtimeKind !== "harness" || sessionId) return;
    const attached = harnessSessions.find((item) => item.id === harnessSessionId);
    const profile = harnesses.find((item) => item.id === (attached?.harnessProfileId ?? harnessId));
    if (attached) setHarnessId(attached.harnessProfileId);
    setModel(attached?.model ?? (profile?.defaultModel?.trim() || profile?.models[0]) ?? "");
  }, [harnessId, harnessSessionId, harnessSessions, harnesses, runtimeKind, sessionId]);
  useEffect(() => {
    if (selectedHarnessSession) setSelectedMcpIds(selectedHarnessSession.mcpServerIds);
  }, [selectedHarnessSession?.id]);
  useEffect(() => {
    if (runtimeKind !== "harness") return;
    setHarnessReasoningEffort(
      (selectedHarnessSession?.harnessProfileId === harnessId && selectedHarnessSession?.model === model ? selectedHarnessSession.reasoningEffort : undefined)
      ?? selectedHarnessModelOptions?.defaultReasoningEffort
      ?? "",
    );
    setHarnessServiceTier(
      (selectedHarnessSession?.harnessProfileId === harnessId && selectedHarnessSession?.model === model ? selectedHarnessSession.serviceTier : undefined)
      ?? selectedHarnessModelOptions?.defaultServiceTier
      ?? "",
    );
  }, [
    model,
    runtimeKind,
    selectedHarness?.id,
    selectedHarnessModelOptions,
    selectedHarnessSession?.id,
    selectedHarnessSession?.reasoningEffort,
    selectedHarnessSession?.serviceTier,
  ]);
  const harnessModesKey = (selectedHarness?.capabilities?.modes ?? []).join("\n");
  useEffect(() => {
    // A different runtime, harness or project invalidates the operator's skill choice.
    // Catalog refreshes (projectCatalogKey) must not: they would erase a skill being typed.
    const modes = harnessModesKey ? harnessModesKey.split("\n") : [];
    setHarnessMode((current) => modes.includes(current) ? current : "");
    setHarnessSkillPath("");
    setSkillToken(undefined);
  }, [engagement?.id, harnessModesKey, runtimeKind, selectedHarness?.id]);
  useEffect(() => {
    setHarnessSkillError(undefined);
    const harnessSkillsAvailable = runtimeKind === "harness"
      && Boolean(selectedHarness?.capabilities?.skillInvocation)
      && Boolean(selectedHarness?.nativeCapabilities.skills);
    if (!api || !engagement || (runtimeKind === "harness" && !harnessSkillsAvailable)) {
      setHarnessSkills([]);
      setHarnessSkillsLoading(false);
      return;
    }
    const controller = new AbortController();
    setHarnessSkillsLoading(true);
    const request = runtimeKind === "provider"
      ? api.listSkills(engagement.id, controller.signal)
      : api.listHarnessSkills(selectedHarness!.id, engagement.id, controller.signal);
    void request
      .then((skills) => {
        setHarnessSkills(skills);
        setHarnessSkillError(undefined);
      })
      .catch((error) => {
        if (controller.signal.aborted) return;
        logCaughtDiagnostic("interface.chat.skills_list_failed", "Skills could not be discovered.", error, "chat-skills");
        setHarnessSkills([]);
        setHarnessSkillError(error instanceof Error ? error.message : "Skills could not be discovered.");
      })
      .finally(() => {
        if (!controller.signal.aborted) setHarnessSkillsLoading(false);
      });
    return () => controller.abort();
  }, [api, engagement, runtimeKind, selectedHarness, projectCatalogKey]);
  useEffect(() => {
    if (!api || !engagement || runtimeKind !== "provider") {
      setNativeHooks([]);
      setSelectedHookIds([]);
      setNativeHookError(undefined);
      return;
    }
    const controller = new AbortController();
    void api.listNativeHooks(engagement.id, controller.signal).then(items => {
      setNativeHooks(items);
      setSelectedHookIds(current => current.filter(id => items.some(item => item.id === id)));
      setNativeHookError(undefined);
    }).catch(error => {
      if (controller.signal.aborted) return; // diagnostic-expected: superseded by a newer project load
      void logCaughtDiagnostic("interface.sessions.hooks_load_failed", "Project lifecycle hooks could not be discovered.", error, "hooks");
      setNativeHooks([]);
      setNativeHookError(error instanceof Error ? error.message : "Hooks could not be discovered.");
    });
    return () => controller.abort();
  }, [api, engagement, runtimeKind, projectCatalogKey]);
  useEffect(() => {
    if (assistantSettingsOpen || view === "chat") setProjectCatalogKey(key => key + 1);
  }, [assistantSettingsOpen, view]);
  useEffect(() => {
    const refresh = () => setProjectCatalogKey(key => key + 1);
    window.addEventListener("focus", refresh);
    return () => window.removeEventListener("focus", refresh);
  }, []);
  useEffect(() => {
    if (runtimeKind !== "provider") return;
    if (!enabledProviders.length) {
      setProviderId("");
      setModel("");
      return;
    }
    if (enabledProviders.some((provider) => provider.id === providerId)) return;
    const provider = enabledProviders.find(
      (item) => item.state === "healthy" || item.state === "unchecked",
    );
    setProviderId(provider?.id ?? "");
    setModel(providerDefaultModel(provider));
  }, [enabledProviders, providerId, runtimeKind]);

  useEffect(() => {
    if (runtimeKind !== "provider" || !selectedProvider || sessionId) return;
    const models = selectedProvider.models;
    if (!models.length) {
      if (!discoveringProviderId && model) setModel("");
      return;
    }
    if (model && models.includes(model)) return;
    setModel(providerDefaultModel(selectedProvider));
  }, [discoveringProviderId, model, runtimeKind, selectedProvider, sessionId]);

  useEffect(() => {
    if (runtimeKind !== "provider" || !providerId) {
      lastModelDiscoveryProviderIdRef.current = undefined;
      return;
    }
    // Existing conversations discover models when the operator opens Assistant settings.
    if (coreState !== "online" || (sessionId && !assistantSettingsOpen)) return;
    if (lastModelDiscoveryProviderIdRef.current === providerId) return;
    lastModelDiscoveryProviderIdRef.current = providerId;
    setDiscoveringProviderId(providerId);
    void refreshProvider(providerId).finally(() => {
      setDiscoveringProviderId((current) => current === providerId ? undefined : current);
    });
  }, [assistantSettingsOpen, coreState, providerId, refreshProvider, runtimeKind, sessionId]);

  useEffect(() => {
    sessionLoadAbortRef.current?.abort();
    sessionSelectionGenerationRef.current += 1;
    previewOwnerRef.current = "";
    restoredScrollRef.current = undefined;
    setSessionReadReady(true);
    historicalActivityAbortRef.current.forEach((controller) => controller.abort());
    historicalActivityAbortRef.current.clear();
    detachActiveChatStream();
    harnessFollowDetachRef.current?.();
    harnessFollowDetachRef.current = undefined;
    setSending(false);
    setSessions([]);
    pendingSessionNavigationRef.current = undefined;
    setSessionId("");
    runtimeSwitchGenerationRef.current += 1;
    setRuntimeSwitchConfirmation(undefined);
    setConversationOpen(Boolean(assistantDrafts.length || requestedSessionId));
    setHarnessSessionId("");
    setHarnessMode("");
    setHarnessSkillPath("");
    setSkillToken(undefined);
    setHarnessActivity(undefined);
    setHarnessActivityError(undefined);
    setHarnessProgress(undefined);
    setMessages([]);
    setReplacedMessages([]);
    setChatError(undefined);
    setFailedProviderRecovery(undefined);
    setRunCandidate(undefined);
    setToolCards([]);
    setActivityItems([]);
    setHarnessInteractions([]);
    setHistoricalActivityState({});
    setHistoricalActivityErrors({});
    setPendingResponse(undefined);
    setInterruptedRecovery(undefined);
  }, [engagement?.id]);

  useEffect(() => {
    if (!api || coreState !== "online" || !sessionId || sending) {
      if (!sessionId) {
        setContextStatus(undefined);
        setContextStatusError(undefined);
      }
      return;
    }
    const controller = new AbortController();
    setContextStatusLoading(true);
    setContextStatusError(undefined);
    void api.getChatContext(sessionId, controller.signal).then((status) => {
      if (!isReadableContextStatus(status, sessionId)) throw new Error("Core returned an unreadable context status.");
      setContextStatus(status);
    }).catch((error) => {
      if (controller.signal.aborted) return;
      void logCaughtDiagnostic("interface.sessions_page.context_status", "Assistant context status could not be loaded.", error, "sessions_page");
      setContextStatusError(error instanceof Error ? error.message : "Context status is unavailable.");
    }).finally(() => {
      if (!controller.signal.aborted) setContextStatusLoading(false);
    });
    return () => controller.abort();
  }, [api, contextRefreshKey, coreState, sending, sessionId]);

  useEffect(() => {
    if (!api || coreState !== "online" || !engagement) {
      setExecutionCapabilities(undefined);
      return;
    }
    const controller = new AbortController();
    void api.executionCapabilities(engagement.id, controller.signal)
      .then(setExecutionCapabilities)
      .catch((caughtError) => { void logCaughtDiagnostic("interface.sessions_page.caught_failure_04", "A handled interface operation failed.", caughtError, "sessions_page"); return setExecutionCapabilities(undefined); });
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
        void logCaughtDiagnostic("interface.sessions_page.caught_failure_05", "A handled interface operation failed.", error, "sessions_page");
        if (!controller.signal.aborted) setChatError(error instanceof Error ? error.message : "Could not load conversations.");
      });
    return () => controller.abort();
  }, [api, coreState, engagement]);

  useEffect(() => {
    return () => {
      sessionLoadAbortRef.current?.abort();
      historicalActivityAbortRef.current.forEach((controller) => controller.abort());
      historicalActivityAbortRef.current.clear();
      detachActiveChatStream();
    };
  }, []);

  useEffect(() => {
    return () => harnessFollowDetachRef.current?.();
  }, []);

  useEffect(() => () => {
    if (streamFrameRef.current !== undefined) cancelAnimationFrame(streamFrameRef.current);
  }, []);

  useEffect(() => {
    if (!messageActionStatus) return;
    const timeout = window.setTimeout(() => setMessageActionStatus(undefined), 3_000);
    return () => window.clearTimeout(timeout);
  }, [messageActionStatus]);

  const flushStreamDeltas = () => {
    streamFrameRef.current = undefined;
    const pending = streamDeltaRef.current;
    const pendingReasoning = streamReasoningRef.current;
    if (!pending.size && !pendingReasoning.size) return;
    streamDeltaRef.current = new Map();
    streamReasoningRef.current = new Map();
    setMessages((current) => current.map((message) => {
      const delta = pending.get(message.id);
      const reasoningDelta = pendingReasoning.get(message.id);
      if (!delta && !reasoningDelta) return message;
      return {
        ...message,
        content: delta ? message.content + delta : message.content,
        reasoning: reasoningDelta ? (message.reasoning ?? "") + reasoningDelta : message.reasoning,
      };
    }));
  };

  const queueStreamDelta = (assistantId: string, delta: string) => {
    streamDeltaRef.current.set(assistantId, (streamDeltaRef.current.get(assistantId) ?? "") + delta);
    if (streamFrameRef.current === undefined) {
      streamFrameRef.current = requestAnimationFrame(flushStreamDeltas);
    }
  };

  const queueStreamReasoning = (assistantId: string, delta: string) => {
    streamReasoningRef.current.set(assistantId, (streamReasoningRef.current.get(assistantId) ?? "") + delta);
    if (streamFrameRef.current === undefined) {
      streamFrameRef.current = requestAnimationFrame(flushStreamDeltas);
    }
  };

  const refreshSessions = async (selectedId?: string) => {
    if (!api || !engagement) return;
    const requestedEngagementId = engagement.id;
    const selectionGeneration = sessionSelectionGenerationRef.current;
    const page = await api.listChatSessions(requestedEngagementId);
    if (activeEngagementIdRef.current !== requestedEngagementId) return;
    setSessions(page.items.sort((left, right) => right.updatedAt.localeCompare(left.updatedAt)));
    if (selectedId && sessionSelectionGenerationRef.current === selectionGeneration) {
      setSessionId(selectedId);
      openSessionChatView(selectedId, true);
    }
  };

  const createGoalConversation = async (draft: ProviderGoalDraft): Promise<ChatGoal> => {
    if (!api || !engagement) throw new Error("Select a project before adding a goal.");
    if (!providerId || !model.trim()) throw new Error("Choose a provider and model before adding a goal.");
    const created = await api.createChatGoalConversation({
      engagementId: engagement.id,
      providerId,
      model: model.trim(),
      toolsEnabled: canUseTools,
      mcpServerIds: selectedMcpIds,
      hookIds: selectedHookIds,
      ...draft,
    });
    setProviderGoal(created.goal);
    await refreshSessions(created.session.id);
    return created.goal;
  };

  const refreshSessionActivity = useCallback(async () => {
    if (!api || !engagement) return;
    const requestedEngagementId = engagement.id;
    try {
      const activity = await api.listChatSessionActivity(requestedEngagementId);
      if (activeEngagementIdRef.current !== requestedEngagementId) return;
      setSessionActivity(Object.fromEntries(activity.map(item => [item.sessionId, item.state])));
    } catch (error) {
      void logCaughtDiagnostic("interface.sessions_page.activity_status", "Conversation activity status could not be refreshed.", error, "sessions_page");
    }
  }, [api, engagement]);

  useEffect(() => {
    if ((!conversationPanelOpen && !mobileListOpen) || view !== "chat") return;
    void refreshSessionActivity();
    const timer = window.setInterval(() => void refreshSessionActivity(), 5_000);
    const onFocus = () => void refreshSessionActivity();
    window.addEventListener("focus", onFocus);
    return () => { window.clearInterval(timer); window.removeEventListener("focus", onFocus); };
  }, [conversationPanelOpen, mobileListOpen, refreshSessionActivity, view]);

  const resetConversation = (open: boolean, options: { discardDraft?: boolean } = {}) => {
    previewOwnerRef.current = "";
    restoredScrollRef.current = undefined;
    setSessionReadReady(true);
    sessionSelectionGenerationRef.current += 1;
    setApprovalDecisionBusy(false);
    pendingSessionNavigationRef.current = undefined;
    sessionLoadAbortRef.current?.abort();
    sessionLoadAbortRef.current = undefined;
    historicalActivityAbortRef.current.forEach((controller) => controller.abort());
    historicalActivityAbortRef.current.clear();
    followUpAutoDrainRef.current = false;
    followUpDrainIdRef.current = undefined;
    detachActiveChatStream();
    harnessFollowDetachRef.current?.();
    harnessFollowDetachRef.current = undefined;
    setSending(false);
    setLoadingHistory(false);
    setAssistantSettingsOpen(false);
    setAssistantSettingsError(undefined);
    setSessionId("");
    runtimeSwitchGenerationRef.current += 1;
    setRuntimeSwitchConfirmation(undefined);
    setConversationOpen(open);
    setHarnessSessionId("");
    setHarnessActivity(undefined);
    setHarnessActivityError(undefined);
    setHarnessProgress(undefined);
    setMessages([]);
    setReplacedMessages([]);
    // The draft-key effect flushes the outgoing composer text under the
    // previous conversation's key when the key changes, so the composer is
    // only cleared here when that conversation was deleted; a batched empty
    // draft would otherwise erase the unsent text of the chat just left.
    if (options.discardDraft) setDraft("");
    setChatError(undefined);
    setMobileListOpen(false);
    setToolCards([]);
    setActivityItems([]);
    setHarnessInteractions([]);
    setHistoricalActivityState({});
    setHistoricalActivityErrors({});
    setPendingResponse(undefined);
  };

  const newConversation = () => {
    setResolvedApproval(undefined);
    // URL navigation and state updates are committed on separate React turns.
    // Suppress the old URL session during that gap so it cannot immediately
    // re-select the conversation the operator just detached from.
    explicitNewConversationRef.current = true;
    resetConversation(true);
    applyDefaultRuntime();
    if (engagement && defaultRuntime) runtimeDefaultEngagementRef.current = engagement.id;
    openUnattachedChatView();
  };

  const deleteConversation = async (session: ChatSessionSummary) => {
    if (!api || deletingSessionId || deletingAllSessions) return;
    const approved = await confirm({
      title: `Delete ${session.title}?`,
      message: "This permanently deletes the conversation, its messages, and its saved working memory.",
      confirmLabel: "Delete conversation",
      tone: "danger",
    });
    if (!approved) return;
    setDeletingSessionId(session.id);
    setChatError(undefined);
    try {
      await api.deleteChatSession(session.id);
      chatPreviews.delete(session.id);
      if (engagement) {
        clearChatDraft(sessionStorage, chatDraftStorageKey(engagement.id, session.id));
        clearChatFollowUps(sessionStorage, chatFollowUpStorageKey(engagement.id, session.id));
      }
      setSessions((current) => current.filter((item) => item.id !== session.id));
      if (sessionId === session.id) {
        resetConversation(false, { discardDraft: true });
        openUnattachedChatView();
      }
    } catch (error) {
      void logCaughtDiagnostic("interface.sessions_page.caught_failure_06", "A handled interface operation failed.", error, "sessions_page");
      setChatError(error instanceof Error ? error.message : "Could not delete the conversation.");
    } finally {
      setDeletingSessionId(undefined);
    }
  };

  const deleteAllConversations = async () => {
    if (!api || deletingSessionId || deletingAllSessions || !sessions.length || sending || pendingResponse) return;
    const targets = [...sessions];
    const approved = await confirm({
      title: "Delete all conversations?",
      message: `This permanently deletes all ${targets.length} saved conversation${targets.length === 1 ? "" : "s"}, including every message and saved working memory.`,
      confirmLabel: "Delete all conversations",
      tone: "danger",
    });
    if (!approved) return;
    setDeletingAllSessions(true);
    setChatError(undefined);
    const results = await Promise.allSettled(targets.map((session) => api.deleteChatSession(session.id)));
    const deletedIds = new Set(targets.filter((_, index) => results[index]?.status === "fulfilled").map((session) => session.id));
    deletedIds.forEach(id => chatPreviews.delete(id));
    if (engagement) {
      for (const deletedId of deletedIds) {
        clearChatDraft(sessionStorage, chatDraftStorageKey(engagement.id, deletedId));
        clearChatFollowUps(sessionStorage, chatFollowUpStorageKey(engagement.id, deletedId));
      }
    }
    const failures = results.filter((result) => result.status === "rejected");
    setSessions((current) => current.filter((session) => !deletedIds.has(session.id)));
    if (deletedIds.has(sessionId)) {
      resetConversation(false, { discardDraft: true });
      openUnattachedChatView();
    }
    if (failures.length) {
      for (const failure of failures) {
        if (failure.status === "rejected") {
          void logCaughtDiagnostic("interface.sessions_page.caught_failure_21", "One conversation could not be deleted during a bulk delete.", failure.reason, "sessions_page");
        }
      }
      setChatError(`${failures.length} of ${targets.length} conversations could not be deleted. A conversation with an active response must finish before it can be deleted.`);
    }
    setDeletingAllSessions(false);
  };

  const setConversationArchived = async (session: ChatSessionSummary, archived: boolean) => {
    if (!api || archivingSessionId) return;
    setSessionActionsId(undefined);
    setArchivingSessionId(session.id);
    setChatError(undefined);
    try {
      const updated = await api.setChatSessionArchived(session.id, archived, session.revision);
      setSessions((current) => current.map((item) => item.id === updated.id ? updated : item));
    } catch (error) {
      void logCaughtDiagnostic("interface.sessions_page.caught_failure_22", "A conversation could not be archived or restored.", error, "sessions_page");
      setChatError(error instanceof Error ? error.message : `Could not ${archived ? "archive" : "unarchive"} the conversation.`);
    } finally {
      setArchivingSessionId(undefined);
    }
  };

  const toggleConversationPanel = () => {
    setConversationPanelOpen((current) => {
      const next = !current;
      writeConversationPanelOpen(localStorage, next);
      return next;
    });
  };

  const closeConversationPanel = () => {
    setMobileListOpen(false);
    if (conversationPanelOpen) {
      setConversationPanelOpen(false);
      writeConversationPanelOpen(localStorage, false);
    }
  };

  const startRenamingConversation = (session: ChatSessionSummary) => {
    setSessionActionsId(undefined);
    setRenamingSessionId(session.id);
    setRenameDraft(session.title);
    setRenameError(undefined);
  };

  const cancelRenamingConversation = () => {
    const cancelledSessionId = renamingSessionId;
    setRenamingSessionId(undefined);
    setRenameDraft("");
    setRenameError(undefined);
    if (cancelledSessionId) requestAnimationFrame(() => document.getElementById(`conversation-actions-trigger-${cancelledSessionId}`)?.focus());
  };

  const renameConversation = async (event: FormEvent, session: ChatSessionSummary) => {
    event.preventDefault();
    // A second submit while the first is in flight would reuse the same
    // expected revision and surface a false conflict.
    if (!api || renamingBusy || renamingSessionId !== session.id) return;
    const title = renameDraft.trim();
    if (!title) return;
    if (title === session.title) {
      cancelRenamingConversation();
      return;
    }
    setRenameError(undefined);
    setRenamingBusy(true);
    try {
      const updated = await api.renameChatSession(session.id, {
        title,
        expectedRevision: session.revision,
      });
      setSessions((current) => current.map((item) => item.id === updated.id ? updated : item));
      setRenamingSessionId(undefined);
      setRenameDraft("");
    } catch (error) {
      void logCaughtDiagnostic("interface.sessions_page.caught_failure_07", "A handled interface operation failed.", error, "sessions_page");
      setRenameError(error instanceof Error ? error.message : "Could not rename the conversation.");
    } finally {
      setRenamingBusy(false);
    }
  };

  const exportConversation = async (session: ChatSessionSummary) => {
    if (!api || !engagement || exportingSessionId) return;
    setSessionActionsId(undefined);
    const approved = await confirm({
      title: `Export ${session.title}?`,
      message: "The Markdown transcript can contain sensitive prompts, responses, citations, and selected-context hashes. Tool artifacts and raw evidence stay in the project evidence bundle.",
      confirmLabel: "Export transcript",
    });
    if (!approved) return;
    setExportingSessionId(session.id);
    setChatError(undefined);
    try {
      const authoritative = await api.listChatMessages(session.id);
      const blob = new Blob([formatChatTranscript({
        session,
        engagementName: engagement.name,
        messages: authoritative,
      })], { type: "text/markdown;charset=utf-8" });
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = chatTranscriptFilename(session);
      anchor.click();
      globalThis.setTimeout(() => URL.revokeObjectURL(url), 0);
    } catch (error) {
      void logCaughtDiagnostic("interface.sessions_page.transcript_export", "A saved assistant transcript could not be exported.", error, "sessions_page");
      setChatError(error instanceof Error ? error.message : "Could not export the saved transcript.");
    } finally {
      setExportingSessionId(undefined);
    }
  };

  const copyConversationLink = async (session: ChatSessionSummary) => {
    setSessionActionsId(undefined);
    const url = new URL(window.location.origin + window.location.pathname);
    url.searchParams.set("view", "chat");
    url.searchParams.set("session", session.id);
    try {
      await copySelectionText(url.toString());
      setMessageActionStatus("Conversation link copied without authentication material.");
    } catch (error) {
      void logCaughtDiagnostic("interface.sessions_page.link_copy", "A conversation link could not be copied.", error, "sessions_page");
      setChatError(error instanceof Error ? error.message : "Could not copy the conversation link.");
    }
  };

  const selectProvider = (id: string) => {
    const provider = enabledProviders.find((item) => item.id === id);
    setProviderId(id);
    setModel(providerDefaultModel(provider));
    setRuntimeSwitchConfirmation(undefined);
  };

  const saveProviderAssistantSelections = async (
    nextMcpServerIds: string[],
    nextHookIds: string[],
    nextReasoningEffort: ReasoningEffort | "" = reasoningEffort,
  ) => {
    setSelectedMcpIds(nextMcpServerIds);
    setSelectedHookIds(nextHookIds);
    setAssistantSettingsError(undefined);
    if (!api || !sessionId || runtimeKind !== "provider") return;
    const current = sessions.find((item) => item.id === sessionId);
    if (!current || assistantSettingsBusy) return;
    setAssistantSettingsStatus("Saving assistant settings…");
    setAssistantSettingsBusy(true);
    try {
      const updated = await api.updateChatSessionAssistantSettings(sessionId, {
        mcpServerIds: nextMcpServerIds,
        hookIds: nextHookIds,
        ...(nextReasoningEffort ? { reasoningEffort: nextReasoningEffort } : { useModelReasoningDefault: true }),
        expectedRevision: current.revision,
      });
      setSessions((items) => items.map((item) => item.id === updated.id ? updated : item));
      setSelectedMcpIds(updated.mcpServerIds);
      setSelectedHookIds(updated.hookIds);
      setReasoningEffort(updated.reasoningEffort ?? "");
      setAssistantSettingsStatus("Assistant settings saved.");
    } catch (error) {
      void logCaughtDiagnostic("interface.sessions.assistant_settings_save_failed", "Assistant settings could not be saved.", error, "assistant_settings");
      setSelectedMcpIds(current.mcpServerIds);
      setSelectedHookIds(current.hookIds);
      setReasoningEffort(current.reasoningEffort ?? "");
      setAssistantSettingsStatus("");
      setAssistantSettingsError(error instanceof Error ? error.message : "Could not save assistant settings.");
    } finally {
      setAssistantSettingsBusy(false);
    }
  };

  const proposeProviderRuntime = async (nextProviderId: string, nextModel: string) => {
    const generation = ++runtimeSwitchGenerationRef.current;
    const activeSession = sessions.find((item) => item.id === sessionId);
    if (!api || !activeSession || activeSession.backend !== "provider") {
      setProviderId(nextProviderId);
      setModel(nextModel);
      setRuntimeSwitchConfirmation(undefined);
      setAssistantSettingsStatus("Model updated. Applies to your next message.");
      return;
    }
    if (activeSession.providerId === nextProviderId && activeSession.model === nextModel) {
      setProviderId(nextProviderId);
      setModel(nextModel);
      setRuntimeSwitchConfirmation(undefined);
      setAssistantSettingsStatus("Using the conversation's saved model.");
      return;
    }
    setAssistantSettingsStatus("Checking the selected model against this conversation…");
    try {
      const toolsEnabled = Boolean(canUseTools || selectedMcpIds.length || browserControlEnabled || selectedHarnessSkill);
      const check = () => api.preflightChatRuntimeSwitch(activeSession.id, {
        providerId: nextProviderId,
        model: nextModel,
        toolsEnabled,
        expectedSessionRevision: activeSession.revision,
      });
      let preflight = await check();
      if (generation !== runtimeSwitchGenerationRef.current) return;
      // A model nobody has run tools against yet is refused until it is
      // verified, which for a new conversation happens on selection. Do the
      // same here rather than leaving the operator on the old model.
      if (!preflight.compatible && preflight.reasonCode === "model_not_tool_verified") {
        setAssistantSettingsStatus(`Verifying ${nextModel} for tool use…`);
        try {
          await reverifyProvider(nextProviderId, nextModel);
        } catch (error) {
          void logCaughtDiagnostic("interface.sessions.runtime_switch_verification_failed", "The model could not be verified for tool use.", error, "assistant_settings");
          setAssistantSettingsStatus(error instanceof Error ? error.message : `${nextModel} could not be verified for tool use.`);
          return;
        }
        if (generation !== runtimeSwitchGenerationRef.current) return;
        preflight = await check();
        if (generation !== runtimeSwitchGenerationRef.current) return;
      }
      if (!preflight.compatible) {
        setAssistantSettingsStatus(preflight.reason ?? "This model cannot serve the current conversation.");
        return;
      }
      if (preflight.requiresCompactionConfirmation) {
        const approved = await confirm({
          title: "Switch model and compact context?",
          message: `${nextModel} has room for ${preflight.targetInputTokens?.toLocaleString() ?? "fewer"} active input tokens, while this conversation currently uses about ${preflight.estimatedActiveInputTokens.toLocaleString()}. Nebula will preserve the full transcript and compact only the active provider context before the next message.`,
          confirmLabel: "Switch and compact",
        });
        if (generation !== runtimeSwitchGenerationRef.current) return;
        if (!approved) {
          setAssistantSettingsStatus("Model switch cancelled. The saved model remains selected.");
          return;
        }
      }
      setProviderId(nextProviderId);
      setModel(nextModel);
      setRuntimeSwitchConfirmation(preflight.confirmationToken);
      setAssistantSettingsStatus(preflight.requiresCompactionConfirmation
        ? "Compaction approved. The switch applies to your next message."
        : "Model updated. Applies to your next message.");
    } catch (error) {
      void logCaughtDiagnostic("interface.sessions.runtime_switch_failed", "The model switch could not be verified.", error, "assistant_settings");
      setAssistantSettingsStatus(error instanceof Error ? error.message : "Could not verify the model switch.");
    }
  };

  const modelDiscoveryInProgress = discoveringProviderId === providerId;
  const selectedModelIsUnavailable = Boolean(model && selectedProvider && !selectedProvider.models.includes(model));
  const selectedModelSummary = modelCatalogSummary(model, selectedProvider?.modelDescriptors);
  const normalizedProviderModelQuery = providerModelQuery.trim().toLocaleLowerCase();
  const matchesProviderModelQuery = (item: string) => {
    if (!normalizedProviderModelQuery || item === model) return true;
    const descriptor = selectedProvider?.modelDescriptors?.find((candidate) => candidate.id === item);
    return [item, descriptor?.name, descriptor?.description]
      .some((value) => value?.toLocaleLowerCase().includes(normalizedProviderModelQuery));
  };
  const filteredProviderModels = selectedProvider?.models.filter(matchesProviderModelQuery) ?? [];
  // Discovered models outside the provider's allowlist; choosing one adds it to the allowlist.
  const unlistedProviderModels = selectedProvider?.modelAllowlist.length
    ? (selectedProvider.availableModels ?? []).filter((item) => !selectedProvider.models.includes(item))
    : [];
  const filteredUnlistedProviderModels = unlistedProviderModels.filter((item) => item !== model && matchesProviderModelQuery(item));
  const chooseProviderModel = async (nextModel: string) => {
    const provider = selectedProvider;
    if (provider && nextModel && provider.modelAllowlist.length && !provider.modelAllowlist.includes(nextModel)) {
      setAssistantSettingsStatus(`Adding ${nextModel} to ${provider.name}'s allowed models…`);
      try {
        await updateProvider(provider.id, {
          name: provider.name,
          providerType: provider.providerType,
          endpoint: provider.endpoint,
          local: provider.local,
          defaultModel: provider.defaultModel,
          modelAllowlist: [...provider.modelAllowlist, nextModel],
          credentialEnv: provider.credentialRef ? undefined : provider.credentialEnv,
          credentialRef: provider.credentialRef,
          permitsSensitiveData: provider.permitsSensitiveData,
          autoShareToolResults: provider.autoShareToolResults,
          retention: provider.retention,
          residency: provider.residency,
          options: provider.options,
          metadata: provider.metadata,
          expectedRevision: provider.revision,
        });
      } catch (error) {
        void logCaughtDiagnostic("interface.sessions.model_allowlist_update_failed", "The model could not be added to the provider's allowed models.", error, "assistant_settings");
        setAssistantSettingsStatus(error instanceof Error ? error.message : `Could not add ${nextModel} to ${provider.name}'s allowed models.`);
        return;
      }
    }
    await proposeProviderRuntime(providerId, nextModel);
  };
  const modelPlaceholder = modelDiscoveryInProgress
    ? "Discovering models…"
    : selectedProvider?.models.length || unlistedProviderModels.length
      ? "Select model"
      : selectedProvider
        ? "No models discovered"
        : "Select provider first";

  const loadHistoricalHarnessActivity = async (message: ConversationMessage) => {
    const turnId = message.harnessTurnId;
    if (!api || !turnId || historicalActivityAbortRef.current.has(turnId) || historicalActivityState[turnId] === "loaded") return;
    const selectionGeneration = sessionSelectionGenerationRef.current;
    const controller = new AbortController();
    historicalActivityAbortRef.current.set(turnId, controller);
    setHistoricalActivityState((current) => ({ ...current, [turnId]: "loading" }));
    setHistoricalActivityErrors((current) => {
      const next = { ...current };
      delete next[turnId];
      return next;
    });
    try {
      const [page, interactions] = await Promise.all([
        api.getHarnessTurnEvents(turnId, 0, controller.signal),
        api.listHarnessInteractions(turnId, controller.signal),
      ]);
      if (controller.signal.aborted || sessionSelectionGenerationRef.current !== selectionGeneration) return;
      if (message.recoveredHarnessTurn) {
        const partial = page.events.filter(event => event.type === "message_delta").map(event => event.delta ?? "").join("");
        setMessages(current => current.map(row => row.id === message.id && row.recoveredHarnessTurn ? { ...row, content: partial } : row));
      }
      const restored = page.events.reduce(
        (items, event) => isTimelineActivity(event)
          ? reduceHarnessActivity(items, event, message.id)
          : items,
        [] as HarnessActivityItem[],
      );
      setActivityItems((current) => [
        ...current.filter((item) => item.turnId !== turnId),
        ...restored,
      ]);
      setHarnessInteractions((current) => [
        ...current.filter((interaction) => interaction.harnessTurnId !== turnId),
        ...interactions,
      ]);
      setHistoricalActivityState((current) => ({ ...current, [turnId]: "loaded" }));
    } catch (error) {
      if (controller.signal.aborted || sessionSelectionGenerationRef.current !== selectionGeneration) return;
      void logCaughtDiagnostic("interface.sessions_page.historical_activity", "Historical harness activity could not be loaded.", error, "sessions_page");
      setHistoricalActivityState((current) => ({ ...current, [turnId]: "failed" }));
      setHistoricalActivityErrors((current) => ({
        ...current,
        [turnId]: error instanceof Error ? error.message : "Could not load this turn's work details.",
      }));
    } finally {
      if (historicalActivityAbortRef.current.get(turnId) === controller) historicalActivityAbortRef.current.delete(turnId);
    }
  };

  useEffect(() => {
    // Read recent durable work so empty greetings do not retain placeholder cards.
    // Older turns remain explicitly discoverable through Inspect saved work.
    if (!api || loadingHistory || coreState !== "online") return;
    for (const message of messages.slice(-20)) {
      if (message.role === "assistant" && (message.durable || message.recoveredHarnessTurn) && message.harnessTurnId && !historicalActivityState[message.harnessTurnId]) void loadHistoricalHarnessActivity(message);
    }
  }, [api, sessionId, messages.length, loadingHistory, coreState]);

  const selectSession = async (id: string, updateUrl = true, preserveTranscript = false) => {
    explicitNewConversationRef.current = false;
    if (!id) {
      newConversation();
      return;
    }
    if (!api) return;
    if (previewOwnerRef.current === sessionId && !loadingHistory && sessionReadReady) {
      const durable = messages.filter(message => message.durable);
      const durableIds = new Set(durable.map(message => message.id));
      // Mobile hides the transcript while Conversations is open; its DOM offset
      // is then zero. Retain the last visible reading position instead.
      const viewport = chatViewportRef.current;
      const lastPosition = chatReadingPositionRef.current.sessionId === sessionId ? chatReadingPositionRef.current : undefined;
      chatPreviews.set(sessionId, {
        messages: durable,
        toolCards: toolCards.filter(card => durableIds.has(card.assistantId)),
        scrollTop: viewport?.clientHeight ? viewport.scrollTop : lastPosition?.scrollTop ?? 0,
        followBottom: viewport?.clientHeight ? chatFollowBottomRef.current : lastPosition?.followBottom ?? true,
      });
    }
    const preview = chatPreviews.get(id);
    restoredScrollRef.current = preview;
    previewOwnerRef.current = "";
    // Cached durable messages remain usable while Core reconciles authoritative
    // history and actionable state in the background.
    setSessionReadReady(Boolean(preview));
    const selectionGeneration = sessionSelectionGenerationRef.current + 1;
    sessionSelectionGenerationRef.current = selectionGeneration;
    setApprovalDecisionBusy(false);
    const selectionIsCurrent = () => sessionSelectionGenerationRef.current === selectionGeneration;
    sessionLoadAbortRef.current?.abort();
    historicalActivityAbortRef.current.forEach((controller) => controller.abort());
    historicalActivityAbortRef.current.clear();
    const loadController = new AbortController();
    sessionLoadAbortRef.current = loadController;
    followUpAutoDrainRef.current = false;
    followUpDrainIdRef.current = undefined;
    detachActiveChatStream();
    setSending(false);
    setSessionId(id);
    runtimeSwitchGenerationRef.current += 1;
    setRuntimeSwitchConfirmation(undefined);
    setResolvedApproval(undefined);
    setConversationOpen(true);
    if (updateUrl) {
      pendingSessionNavigationRef.current = id;
      openSessionChatView(id);
    }
    setLoadingHistory(true);
    setChatError(undefined);
    setFailedProviderRecovery(undefined);
    setHarnessProgress(undefined);
    setHarnessActivity(undefined);
    if (!preserveTranscript) { setMessages(preview?.messages ?? []); setReplacedMessages([]); }
    setToolCards(preview?.toolCards ?? []);
    setMobileListOpen(false);
    setActivityItems([]);
    setHarnessInteractions([]);
    setPendingResponse(undefined);
    setInterruptedRecovery(undefined);
    setWaitingCallback(undefined);
    setHookExecutions([]);
    setHistoricalActivityState({});
    setHistoricalActivityErrors({});
    harnessFollowDetachRef.current?.();
    harnessFollowDetachRef.current = undefined;
    const summary = sessions.find((session) => session.id === id);
    if (summary) {
      setRuntimeKind(summary.backend);
      setProviderId(summary.providerId ?? "");
      setHarnessId(summary.harnessProfileId ?? "");
      setHarnessSessionId(summary.harnessSessionId ?? "");
      setModel(summary.model ?? "");
      if (summary.backend === "provider") {
        setSelectedMcpIds(summary.mcpServerIds);
        setSelectedHookIds(summary.hookIds);
      }
    }
    try {
      const [history, pendingTurn] = await Promise.all([
        api.listChatMessages(id, loadController.signal, {includeReplaced: true}),
        api.getPendingChatTurn(id, loadController.signal).catch((caughtError) => {
          if (loadController.signal.aborted) return undefined;
          void logCaughtDiagnostic("interface.sessions_page.caught_failure_08", "A handled interface operation failed.", caughtError, "sessions_page");
          throw caughtError;
        }),
      ]);
      if (!selectionIsCurrent()) return;
      const replacedHistory = history.filter((message) => message.replacedAt);
      const activeHistory = history.filter((message) => !message.replacedAt);
      setReplacedMessages(replacedHistory);
      const recoveredHistory = await recoverHarnessHistory(activeHistory.map(persistedMessage), turnId => api.getHarnessTurn(turnId, loadController.signal));
      if (!selectionIsCurrent()) return;
      setMessages(recoveredHistory);
      const restoredToolCards: ToolLifecycleCard[] = activeHistory.flatMap((message) => message.role === "assistant"
        ? (message.toolResults ?? []).map((result) => ({
            assistantId: message.id,
            toolCallId: result.toolCallId,
            capability: result.capability,
            displayName: result.displayName,
            status: result.status,
            summary: result.summary,
            evidenceIds: result.evidenceIds,
            resultArtifactId: result.resultArtifactId,
            artifacts: [],
            receipt: result.receipt,
          }))
        : []);
      setToolCards(restoredToolCards);
      // Restore the exact durable request, independently of the workspace catalog cache.
      // useSessionState owns the authoritative snapshot. Avoid issuing a second
      // state read here; the durable turn already carries the approval identity
      // needed for immediate restoration.
      const restoredApprovalId = pendingTurn?.approvalId;
      const approvalRecord = restoredApprovalId
        ? await api.getApproval(restoredApprovalId, loadController.signal)
        : undefined;
      if (!selectionIsCurrent()) return;
      const approval = approvalRecord?.status === "pending" ? approvalRecord : undefined;
      const decisionRecorded = approvalRecord && !approval && pendingTurn?.status === "waiting_approval";
      if (decisionRecorded) setResolvedApproval({ id: approvalRecord.id, status: approvalRecord.status, turnId: pendingTurn.id, harnessTurnId: pendingTurn.harnessTurnId });
      setLoadingHistory(false);
      if (summary?.backend === "provider") {
        const hookLoader = pendingTurn
          ? api.listChatHookExecutions(pendingTurn.id, loadController.signal)
          : api.listSessionHookExecutions(id, loadController.signal);
        void hookLoader
          .then(items => { if (selectionIsCurrent()) setHookExecutions(items); })
          .catch(error => {
            if (!loadController.signal.aborted) void logCaughtDiagnostic("interface.chat.hook_outcomes_failed", "Hook outcomes could not be loaded.", error, "chat-hooks");
          });
      }
      if (pendingTurn && summary?.backend === "provider") {
        const assistantId = makeId("assistant-pending");
        const resumeRequest: ChatCompletionRequest = {
          backend: "provider",
          providerId: summary.providerId,
          engagementId: engagement?.id,
          sessionId: id,
          model: summary.model,
          messages: [],
          toolsEnabled: true,
        };
        setMessages((current) => [...current, {
          id: assistantId,
          role: "assistant",
          content: "",
          createdAt: pendingTurn.startedAt ?? new Date().toISOString(),
          citations: [],
          state: pendingTurn.status === "waiting_approval" ? "waiting_approval" : ["interrupted", "failed"].includes(pendingTurn.status) ? "error" : "streaming",
          detail: ["interrupted", "failed"].includes(pendingTurn.status) ? pendingTurn.error : undefined,
          durable: false,
        }]);
        setToolCards([...restoredToolCards, ...pendingTurn.toolCallIds.map((toolCallId) => ({
          assistantId,
          toolCallId,
          capability: "Command runtime",
          status: pendingTurn.status === "waiting_approval" ? "waiting_approval" : ["interrupted", "failed"].includes(pendingTurn.status) ? "failed" : "running",
          evidenceIds: [],
          artifacts: [],
        }))]);
        if (pendingTurn.status === "failed") {
          setPendingResponse(undefined);
          setChatError(pendingTurn.error ?? "The provider did not return a final answer.");
          setFailedProviderRecovery({ turnId: pendingTurn.id, assistantId, request: resumeRequest });
        } else if (pendingTurn.status === "interrupted") {
          setPendingResponse(undefined);
          setInterruptedRecovery({ turn: pendingTurn, assistantId, request: resumeRequest });
        } else if (decisionRecorded) {
          activeProviderTurnIdRef.current = pendingTurn.id;
          setSending(true);
        } else if (pendingTurn.status === "waiting_approval") {
          setPendingResponse({
            turnId: pendingTurn.id,
            assistantId,
            userId: "",
            request: resumeRequest,
            approval: approval ?? { id: pendingTurn.approvalId },
          });
        } else if (pendingTurn.status === "waiting_callback") {
          setWaitingCallback({
            turnId: pendingTurn.id,
            assistantId,
            resultsUrl: pendingTurn.resultsUrl,
            processId: pendingTurn.processId,
            toolCallId: pendingTurn.toolCallIds[0] ?? "",
            summary: "Waiting for the command to POST results.",
          });
        } else {
          setPendingResponse(undefined);
          setSending(true);
          const restoreController = new AbortController();
          abortRef.current = restoreController;
          streamBackendRef.current = "provider";
          void api.followChatTurn(
            pendingTurn.id,
            resumeRequest,
            (streamEvent) => { if (selectionIsCurrent() && !restoreController.signal.aborted) applyChatEvent(streamEvent, assistantId, "", resumeRequest); },
            restoreController.signal,
          ).then(async (response) => {
            if (selectionIsCurrent() && response?.sessionId) await refreshSessions(response.sessionId);
          }).catch((error) => {
            void logCaughtDiagnostic("interface.sessions_page.caught_failure_09", "A handled interface operation failed.", error, "sessions_page");
            if (selectionIsCurrent() && !restoreController.signal.aborted) setChatError(error instanceof Error ? error.message : "Could not restore the pending response.");
          }).finally(() => { if (selectionIsCurrent()) { setSending(false); setChatReconnecting(false); } });
        }
      } else if (pendingTurn?.harnessTurnId && summary?.backend === "harness") {
        const assistantId = makeId("assistant-harness-pending");
        const turnId = pendingTurn.harnessTurnId;
        const page = await api.getHarnessTurnEvents(turnId, 0, loadController.signal);
        if (!selectionIsCurrent()) return;
        setActivityItems((current) => page.events.reduce(
          (restored, event) => isTimelineActivity(event)
            ? reduceHarnessActivity(restored, event, assistantId)
            : restored,
          current,
        ));
        const turnInteractions = await api.listHarnessInteractions(turnId, loadController.signal);
        if (!selectionIsCurrent()) return;
        setHarnessInteractions(turnInteractions);
        setMessages((current) => [...current, {
          id: assistantId,
          role: "assistant",
          content: page.events.filter((event) => event.type === "message_delta").map((event) => event.delta ?? "").join(""),
          createdAt: new Date().toISOString(),
          citations: [],
          state: pendingTurn.status === "waiting_approval" ? "waiting_approval" : "streaming",
          durable: false,
          harnessTurnId: turnId,
        }]);
        setHarnessProgress({
          phase: decisionRecorded ? "decision_recorded" : pendingTurn.status === "waiting_approval" ? "waiting_approval" : "running",
          detail: decisionRecorded ? "Decision recorded; waiting for the harness to continue." : pendingTurn.status === "waiting_approval" ? "Harness input or approval is required." : "Reconnected to the active harness turn.",
          sessionId: summary.harnessSessionId,
          turnId,
        });
        setSending(true);
        harnessFollowDetachRef.current = api.followHarnessTurnEvents(
          turnId,
          page.nextSequence,
          (event) => {
            if (!selectionIsCurrent()) return;
            if (event.type === "approval_required") {
              void selectSession(id, false);
              return;
            }
            if (event.type === "message_delta" && event.delta) {
              queueStreamDelta(assistantId, event.delta);
            }
            if (isTimelineActivity(event)) {
              setActivityItems((current) => reduceHarnessActivity(current, event, assistantId));
            }
            if (event.type === "interaction") {
              void api.listHarnessInteractions(turnId).then(items => { if (selectionIsCurrent()) setHarnessInteractions(items); })
                .catch((caughtError) => void logCaughtDiagnostic("interface.sessions_page.interaction_follow", "Harness interactions could not be refreshed.", caughtError, "sessions_page"));
              if (event.itemStatus && event.itemStatus !== "waiting_input") {
                setMessages((current) => current.map((message) => message.id === assistantId ? { ...message, state: "streaming" } : message));
              }
            }
          },
          () => {
            if (!selectionIsCurrent()) return;
            setResolvedApproval(undefined);
            harnessFollowDetachRef.current = undefined;
            void api.listChatMessages(id).then(async (authoritative) => {
              const recovered = await recoverHarnessHistory(authoritative.map(persistedMessage), turnId => api.getHarnessTurn(turnId));
              if (!selectionIsCurrent()) return;
              setMessages(recovered);
              const completedOwner = authoritative.find((message) => message.role === "assistant" && message.harnessTurnId === turnId);
              if (completedOwner) await loadHistoricalHarnessActivity(persistedMessage(completedOwner));
              if (selectionIsCurrent()) await refreshSessions(id);
            }).catch((error) => {
              void logCaughtDiagnostic("interface.sessions_page.harness_follow_complete", "A completed harness turn could not be restored.", error, "sessions_page");
              if (selectionIsCurrent()) setChatError(error instanceof Error ? error.message : "Could not restore the completed harness turn.");
            })
              .finally(() => { if (selectionIsCurrent()) setSending(false); });
          },
          (error) => {
            if (!selectionIsCurrent()) return;
            harnessFollowDetachRef.current?.();
            harnessFollowDetachRef.current = undefined;
            setHarnessProgress(undefined);
            setPendingResponse(undefined);
            setSending(false);
            setChatReconnecting(false);
            setChatError(`${error.message} Reload this conversation to read its authoritative saved state.`);
          },
          state => { if (selectionIsCurrent()) setChatReconnecting(state === "reconnecting"); },
        );
        setPendingResponse(approval ? {
          turnId: pendingTurn.id,
          assistantId,
          userId: "",
          request: {
            backend: "harness",
            harnessProfileId: summary.harnessProfileId,
            harnessSessionId: summary.harnessSessionId,
            engagementId: engagement?.id,
            sessionId: id,
            model: summary.model,
            messages: [],
          },
          approval: { ...approval },
        } : undefined);
        setToolCards([]);
      } else {
        setPendingResponse(undefined);

      }
      if (!selectionIsCurrent()) return;
      previewOwnerRef.current = id;
      setSessionReadReady(true);
      // Selection already closed the phone drawer; a late load must not close it
      // again after the operator reopened it.
    } catch (error) {
      if (!selectionIsCurrent() || loadController.signal.aborted) return;
      setSessionReadReady(false);
      if (error instanceof ApiError && [401, 403, 404].includes(error.status)) {
        chatPreviews.delete(id);
        setMessages([]);
        setToolCards([]);
      }
      void logCaughtDiagnostic("interface.sessions_page.caught_failure_10", "A handled interface operation failed.", error, "sessions_page");
      setChatError(error instanceof Error ? error.message : "Could not load the selected conversation.");
    } finally {
      if (selectionIsCurrent()) {
        if (sessionLoadAbortRef.current === loadController) sessionLoadAbortRef.current = undefined;
        setLoadingHistory(false);
      }
    }
  };

  const observedQueueRef = useRef("");
  const queueTurnSignature = `${sessionId}:${coreQueue.queue?.items.filter(item => item.turn_id).map(item => `${item.turn_id}:${item.status}`).join("|") ?? ""}`;
  useEffect(() => {
    if (!coreQueue.queue?.items.some(item => item.turn_id) || sending || pendingResponse || loadingHistory || observedQueueRef.current === queueTurnSignature) return;
    observedQueueRef.current = queueTurnSignature;
    void selectSession(sessionId, false, true);
  }, [queueTurnSignature, sending, pendingResponse, loadingHistory]);

  const forkConversation = async (message: ConversationMessage) => {
    if (!api || !sessionId || !message.durable || sending) return;
    setChatError(undefined);
    try {
      const fork = await api.forkChatSession(sessionId, message.id);
      setSessions((current) => [fork, ...current.filter((item) => item.id !== fork.id)]);
      await selectSession(fork.id);
    } catch (error) {
      void logCaughtDiagnostic("interface.sessions_page.fork", "Conversation fork failed.", error, "sessions_page");
      setChatError(error instanceof Error ? error.message : "Could not fork the conversation.");
    }
  };

  const beginMessageEdit = (message: ConversationMessage) => {
    if (!sessionId || !message.durable || message.role !== "user" || sending) return;
    setChatError(undefined);
    setMessageEdit({messageId: message.id, sequence: message.sequence, text: message.content, busy: false});
  };

  const cancelMessageEdit = () => {
    setMessageEdit(undefined);
    composerRef.current?.focus();
  };

  // Editing rewinds this conversation instead of branching: Core retracts the
  // edited message and everything it produced, then the edited wording is sent
  // as the next message of the same conversation.
  const resendEditedMessage = async () => {
    const editing = messageEdit;
    if (!api || !sessionId || !editing || editing.busy || sending) return;
    const text = editing.text.trim();
    if (!text) return;
    const original = messages.find((item) => item.id === editing.messageId);
    setMessageEdit({...editing, busy: true, error: undefined});
    let rewound;
    try {
      rewound = await api.rewindChatSession(sessionId, editing.messageId);
    } catch (error) {
      void logCaughtDiagnostic("interface.assistant_chat.edit_rewind_failed", "The edited message could not replace the saved conversation.", error, "assistant_chat");
      setMessageEdit({...editing, busy: false, error: error instanceof Error ? error.message : "Could not replace this message. The conversation is unchanged."});
      return;
    }
    setMessages(rewound.messages.map(persistedMessage));
    setReplacedMessages((current) => [
      ...current.filter((item) => !rewound.replaced.some((entry) => entry.id === item.id)),
      ...rewound.replaced,
    ]);
    setSessions((current) => current.map((item) => item.id === rewound.session.id ? rewound.session : item));
    if (rewound.session.harnessSessionId) setHarnessSessionId(rewound.session.harnessSessionId);
    setMessageEdit(undefined);
    await submit(undefined, undefined, undefined, {
      text,
      contentBlocks: [
        {type: "text" as const, text},
        ...(original?.contentBlocks ?? []).filter((block) => block.type === "image"),
      ],
    });
  };

  const replacedGroups = useMemo(() => {
    const grouped = new Map<string, PersistedChatMessage[]>();
    for (const message of replacedMessages) {
      const key = message.replacedGroupId ?? message.id;
      grouped.set(key, [...(grouped.get(key) ?? []), message]);
    }
    return [...grouped.entries()]
      .map(([id, items]) => {
        const ordered = [...items].sort((left, right) => left.sequence - right.sequence);
        return {id, at: ordered[0]?.replacedAt, from: ordered[0]?.sequence ?? 0, items: ordered};
      })
      .sort((left, right) => left.from - right.from);
  }, [replacedMessages]);
  const {anchoredReplacements, leadingReplacements} = useMemo(() => {
    // Each replaced group keeps its place: it renders under the last message
    // still in the conversation, or above the transcript when the first
    // message was the one edited.
    const anchored = new Map<string, ReplacedMessageGroup[]>();
    const leading: ReplacedMessageGroup[] = [];
    const durable = messages.filter((item) => item.durable && typeof item.sequence === "number");
    for (const group of replacedGroups) {
      const anchor = [...durable].reverse().find((item) => (item.sequence ?? 0) < group.from);
      if (!anchor) { leading.push(group); continue; }
      anchored.set(anchor.id, [...(anchored.get(anchor.id) ?? []), group]);
    }
    return {anchoredReplacements: anchored, leadingReplacements: leading};
  }, [replacedGroups, messages]);

  const pendingApprovalToRestore = pendingApprovalId(authoritativeState, authoritativeState?.turn_id ?? undefined);
  useEffect(() => {
    if (!pendingApprovalToRestore) { approvalRestorationRef.current = undefined; return; }
    if (runtimeKind !== "harness" || !sessionId || loadingHistory || approvalDecisionBusy
      || pendingResponse?.approval.id === pendingApprovalToRestore) return;
    const key = `${sessionId}:${authoritativeState?.revision}:${pendingApprovalToRestore}`;
    if (approvalRestorationRef.current === key) return;
    approvalRestorationRef.current = key;
    // A snapshot can reveal another request after a decision, lost event or
    // reconnect. Restore its exact saved card; this only reads/follows work.
    void selectSession(sessionId, false, true);
  }, [sessionId, runtimeKind, pendingApprovalToRestore, authoritativeState?.revision, pendingResponse?.approval.id, loadingHistory, approvalDecisionBusy]);

  const copyMessage = async (message: ConversationMessage) => {
    try {
      await copySelectionText(message.content);
      setMessageActionStatus(`${message.role === "assistant" ? "Assistant response" : "Operator message"} copied exactly.`);
    } catch (error) {
      void logCaughtDiagnostic("interface.sessions_page.message_copy", "A chat message could not be copied.", error, "sessions_page");
      setChatError(error instanceof Error ? error.message : "Could not copy the message.");
    }
  };

  const quoteMessage = (message: ConversationMessage) => {
    const quoted = message.content.split("\n").map((line) => `> ${line}`).join("\n");
    setDraft((current) => current ? `${current}\n\n${quoted}\n\n` : `${quoted}\n\n`);
    setMessageActionStatus("Quoted message added to the editable composer draft.");
    globalThis.requestAnimationFrame?.(() => {
      const composer = composerRef.current;
      if (!composer) return;
      composer.focus();
      composer.setSelectionRange(composer.value.length, composer.value.length);
    });
  };

  useEffect(() => {
    const pendingNavigation = pendingSessionNavigationRef.current;
    if (pendingNavigation) {
      if (requestedSessionId === pendingNavigation) pendingSessionNavigationRef.current = undefined;
      else return;
    }
    if (!requestedSessionId) {
      explicitNewConversationRef.current = false;
      return;
    }
    if (explicitNewConversationRef.current) return;
    if (!requestedSessionId || requestedSessionId === sessionId || !api || !sessions.some((session) => session.id === requestedSessionId)) return;
    void selectSession(requestedSessionId, false);
  }, [api, requestedSessionId, sessionId, sessions]);

  useEffect(() => {
    if (!api || !engagement || requestedSessionId || sessionId || conversationOpen || !sessions.length) return;
    void selectSession(sessions[0].id);
  }, [api, conversationOpen, engagement, requestedSessionId, sessionId, sessions]);

  const openAttachedChat = async (id: string) => {
    if (!api || !engagement) return;
    setChatError(undefined);
    let summary: ChatSessionSummary | undefined;
    try {
      // The attached conversation may be newer than the loaded list; refresh
      // it first so the selection below can find its runtime.
      const page = await api.listChatSessions(engagement.id);
      const ordered = page.items.sort((left, right) => right.updatedAt.localeCompare(left.updatedAt));
      setSessions(ordered);
      summary = ordered.find((session) => session.id === id);
    } catch (error) {
      void logCaughtDiagnostic("interface.sessions_page.caught_failure_11", "A handled interface operation failed.", error, "sessions_page");
      setChatError(error instanceof Error ? error.message : "Could not open the execution conversation.");
      return;
    }
    if (summary) {
      setRuntimeKind(summary.backend);
      setProviderId(summary.providerId ?? "");
      setHarnessId(summary.harnessProfileId ?? "");
      setHarnessSessionId(summary.harnessSessionId ?? "");
      setModel(summary.model ?? "");
      if (summary.backend === "provider") {
        setSelectedMcpIds(summary.mcpServerIds);
        setSelectedHookIds(summary.hookIds);
      }
    }
    // Selecting through selectSession advances the selection generation and
    // aborts any in-flight load, so an earlier selection cannot overwrite it.
    await selectSession(id);
  };

  const applyChatEvent = (
    streamEvent: ChatStreamEvent,
    assistantId: string,
    userId: string,
    request: ChatCompletionRequest,
  ) => {
    if (streamEvent.type === "connection") {
      setChatReconnecting(streamEvent.state === "reconnecting");
      return;
    }
    // Events invalidate the durable snapshot; they never independently resolve
    // an approval. Polling/visibility recovery also covers missed stream events.
    if (["started", "status", "turn_status", "approval", "interaction", "done", "cancelled", "error"].includes(streamEvent.type)) {
      refreshSessionState();
      void refreshSessionActivity();
    }
    if (streamEvent.type === "started" && streamEvent.sessionId) {
      previewOwnerRef.current = streamEvent.sessionId;
      setSessionId(streamEvent.sessionId);
      openSessionChatView(streamEvent.sessionId, true);
      void refreshSessions();
    }
    if (streamEvent.type === "started") {
      if (request.backend === "provider" && streamEvent.turnId) {
        activeProviderTurnIdRef.current = streamEvent.turnId;
      }
      if (streamEvent.harnessSessionId) setHarnessSessionId(streamEvent.harnessSessionId);
      if (request.backend === "harness") {
        setHarnessProgress((current) => ({
          phase: "running",
          detail: "Harness accepted the turn and is processing the request.",
          sessionId: streamEvent.harnessSessionId ?? current?.sessionId,
          turnId: streamEvent.harnessTurnId ?? current?.turnId,
          previousSessionId: current?.previousSessionId,
        }));
      }
      if (streamEvent.harnessTurnId) {
        setMessages((current) => current.map((message) => message.id === assistantId
          ? { ...message, harnessTurnId: streamEvent.harnessTurnId }
          : message));
      }
    }
    if (streamEvent.type === "status") {
      if (streamEvent.harnessSessionId) setHarnessSessionId(streamEvent.harnessSessionId);
      setHarnessProgress({
        phase: streamEvent.phase,
        detail: streamEvent.detail,
        sessionId: streamEvent.harnessSessionId,
        turnId: streamEvent.harnessTurnId,
        previousSessionId: streamEvent.previousSessionId,
      });
      if (streamEvent.harnessTurnId) {
        setMessages((current) => current.map((message) => message.id === assistantId
          ? { ...message, harnessTurnId: streamEvent.harnessTurnId }
          : message));
      }
    }
    if (["turn_status", "item_upsert", "output_delta", "approval", "interaction", "checkpoint", "notice"].includes(streamEvent.type)) {
      const activityEvent = streamEvent as HarnessActivityEvent;
      if (isTimelineActivity(activityEvent)) {
        setActivityItems((current) => reduceHarnessActivity(current, activityEvent, assistantId));
      }
      if (activityEvent.harnessTurnId) {
        setMessages((current) => current.map((message) => message.id === assistantId
          ? { ...message, harnessTurnId: activityEvent.harnessTurnId }
          : message));
      }
      if (streamEvent.type === "interaction" && activityEvent.harnessTurnId && api) {
        void api.listHarnessInteractions(activityEvent.harnessTurnId)
          .then((items) => setHarnessInteractions((current) => [
            ...current.filter((item) => item.harnessTurnId !== activityEvent.harnessTurnId),
            ...items,
          ]))
          .catch((caughtError) => void logCaughtDiagnostic("interface.sessions_page.interaction_refresh", "Could not refresh harness interactions.", caughtError, "sessions_page"));
        if (activityEvent.itemStatus && activityEvent.itemStatus !== "waiting_input") {
          setMessages((current) => current.map((message) => message.id === assistantId ? { ...message, state: "streaming" } : message));
        }
      }
    }
    if ((streamEvent.type === "delta" || streamEvent.type === "message_delta") && streamEvent.delta) {
      queueStreamDelta(assistantId, streamEvent.delta);
      if (request.backend === "provider" && request.goalId) {
        liveGoalStreamTextRef.current += streamEvent.delta;
        setLiveGoalTokenEstimate(estimateLiveTokens(liveGoalStreamTextRef.current));
      }
    }
    if (streamEvent.type === "reasoning_delta" && streamEvent.delta) {
      queueStreamReasoning(assistantId, streamEvent.delta);
      if (request.backend === "provider" && request.goalId) {
        liveGoalStreamTextRef.current += streamEvent.delta;
        setLiveGoalTokenEstimate(estimateLiveTokens(liveGoalStreamTextRef.current));
      }
    }
    if (streamEvent.type === "tool_started") {
      setHarnessProgress((current) => request.backend === "harness" ? {
        ...current,
        phase: "tool",
        detail: `Running ${streamEvent.displayName ?? streamEvent.capability}.`,
      } : current);
      setToolCards((current) => [...current.filter((item) => item.toolCallId !== streamEvent.toolCallId), {
        assistantId,
        toolCallId: streamEvent.toolCallId,
        capability: streamEvent.capability,
        displayName: streamEvent.displayName,
        status: "running",
        evidenceIds: [],
        artifacts: [],
      }]);
      if (request.backend === "harness") {
        setActivityItems((current) => reduceHarnessActivity(current, {
          schemaVersion: "nebula.harness-activity/v1",
          type: "item_upsert",
          harnessTurnId: streamEvent.turnId,
          itemId: streamEvent.toolCallId,
          itemKind: "tool",
          itemStatus: "running",
          title: streamEvent.capability,
          artifactIds: [],
          payload: { arguments: streamEvent.arguments, display_name: streamEvent.displayName },
        }, assistantId));
      }
    }
    if (streamEvent.type === "tool_completed") {
      if (streamEvent.status === "complete" || streamEvent.status === "failed") setWaitingCallback(undefined);
      setToolCards((current) => current.some((item) => item.toolCallId === streamEvent.toolCallId)
        ? current.map((item) => item.toolCallId === streamEvent.toolCallId
          ? { ...item, status: streamEvent.status, summary: streamEvent.summary, evidenceIds: streamEvent.evidenceIds, resultArtifactId: streamEvent.resultArtifactId, artifacts: streamEvent.artifacts, receipt: streamEvent.receipt }
          : item)
        : [...current, {
          assistantId,
          toolCallId: streamEvent.toolCallId,
          capability: streamEvent.capability,
          displayName: streamEvent.displayName,
          status: streamEvent.status,
          summary: streamEvent.summary,
          evidenceIds: streamEvent.evidenceIds,
          resultArtifactId: streamEvent.resultArtifactId,
          artifacts: streamEvent.artifacts,
          receipt: streamEvent.receipt,
        }]);
      if (request.backend === "harness") {
        setActivityItems((current) => reduceHarnessActivity(current, {
          schemaVersion: "nebula.harness-activity/v1",
          type: "item_upsert",
          harnessTurnId: streamEvent.turnId,
          itemId: streamEvent.toolCallId,
          itemKind: "tool",
          itemStatus: streamEvent.status,
          title: streamEvent.capability,
          summary: streamEvent.summary,
          artifactIds: streamEvent.artifacts.map((artifact) => artifact.artifactId),
          payload: { receipt: streamEvent.receipt ?? {}, result_artifact_id: streamEvent.resultArtifactId, display_name: streamEvent.displayName },
        }, assistantId));
      }
    }
    if (streamEvent.type === "callback_required") {
      setWaitingCallback({
        turnId: streamEvent.turnId,
        assistantId,
        resultsUrl: streamEvent.resultsUrl,
        processId: streamEvent.processId,
        toolCallId: streamEvent.toolCallId,
        summary: streamEvent.summary,
      });
      setSending(false);
      setToolCards((current) => current.map((item) => item.toolCallId === streamEvent.toolCallId
        ? { ...item, status: "waiting_callback", summary: streamEvent.summary }
        : item));
      setMessages((current) => current.map((message) => {
        if (message.id === userId) return { ...message, durable: true };
        return message.id === assistantId ? { ...message, state: "streaming", detail: streamEvent.summary } : message;
      }));
    }
    if (streamEvent.type === "approval_required") {
      setHarnessProgress((current) => request.backend === "harness" ? {
        ...current,
        phase: "waiting_approval",
        detail: "Harness work is paused until the requested action is approved or rejected.",
      } : current);
      setToolCards((current) => current.map((item) => item.toolCallId === streamEvent.toolCallId
        ? { ...item, status: "waiting_approval" }
        : item));
      setPendingResponse({
        turnId: streamEvent.turnId,
        assistantId,
        userId,
        request,
        approval: streamEvent.approval,
      });
      setMessages((current) => current.map((message) => {
        if (message.id === userId) return { ...message, durable: true };
        return message.id === assistantId ? { ...message, state: "waiting_approval" } : message;
      }));
    }
    if (streamEvent.type === "done") {
      setChatReconnecting(false);
      if (request.backend === "provider") activeProviderTurnIdRef.current = undefined;
      if (request.backend === "provider" && request.goalId) {
        setLiveGoalTokenEstimate(streamEvent.usage.totalTokens + (streamEvent.contextUsage?.totalTokens ?? 0));
      }
      if (request.backend === "provider" && streamEvent.turnId && api) {
        void api.listChatHookExecutions(streamEvent.turnId).then(setHookExecutions).catch(error => {
          void logCaughtDiagnostic("interface.chat.hook_outcomes_failed", "Hook outcomes could not be loaded.", error, "chat-hooks");
        });
      }
      if (streamFrameRef.current !== undefined) {
        cancelAnimationFrame(streamFrameRef.current);
        streamFrameRef.current = undefined;
      }
      streamDeltaRef.current.delete(assistantId);
      streamReasoningRef.current.delete(assistantId);
      if (streamEvent.harnessSessionId) setHarnessSessionId(streamEvent.harnessSessionId);
      if (request.backend === "harness") {
        setHarnessProgress((current) => ({
          ...current,
          phase: "complete",
          detail: "Harness response and activity records were saved.",
          sessionId: streamEvent.harnessSessionId ?? current?.sessionId,
          turnId: streamEvent.harnessTurnId ?? current?.turnId,
        }));
      }
      setPendingResponse(undefined);
      const durableAssistantId = streamEvent.message.id ?? assistantId;
      if (durableAssistantId !== assistantId) {
        setActivityItems((current) => current.map((item) => item.assistantId === assistantId
          ? { ...item, assistantId: durableAssistantId }
          : item));
        setToolCards((current) => current.map((item) => item.assistantId === assistantId
          ? { ...item, assistantId: durableAssistantId }
          : item));
      }
      setMessages((current) => reconcileCompletedAssistantMessage(current, {
        temporaryAssistantId: assistantId,
        durableAssistantId: streamEvent.message.id,
        userId,
        content: streamEvent.message.content,
        reasoning: streamEvent.message.reasoning,
        citations: streamEvent.citations,
        usage: streamEvent.usage,
        elapsedMs: streamEvent.elapsedMs,
        approvalWaitMs: streamEvent.approvalWaitMs,
        harnessTurnId: streamEvent.harnessTurnId,
        toolSuggestions: streamEvent.toolSuggestions,
        createdAt: new Date().toISOString(),
      }));
    }
    if (streamEvent.type === "cancelled") {
      // Core ended this viewer's stream because the turn was stopped (here or
      // from another tab): settle the bubble instead of waiting on a reconnect.
      setChatReconnecting(false);
      if (request.backend === "provider") activeProviderTurnIdRef.current = undefined;
      setPendingResponse(undefined);
      setMessages((current) => cancelStreamingAssistantMessage(current, assistantId));
    }
    if (streamEvent.type === "interrupted" && request.backend === "harness") {
      setHarnessProgress((current) => ({ ...current, phase: "interrupted", detail: "The harness turn was interrupted." }));
    }
    if (streamEvent.type === "completed" && request.backend === "harness") {
      setHarnessProgress((current) => ({ ...current, phase: "finalizing", detail: "Harness finished; Nebula is saving the response." }));
    }
    if (streamEvent.type === "error") {
      setChatError(streamEvent.detail);
      if (request.backend === "harness") setHarnessProgress((current) => ({ ...current, phase: "failed", detail: streamEvent.detail }));
    }
  };

  const attachImageFiles = async (files: File[]) => {
    if (!files.length || !api || !engagement || uploadingImage) return;
    if (sending || pendingResponse) {
      setChatError("Image attachments are available after the active response finishes; follow-ups are text-only.");
      return;
    }
    if (!imageInputEnabled) {
      setChatError("The selected runtime has not advertised image input support.");
      return;
    }
    const remaining = Math.max(0, 4 - pendingImages.length);
    if (!remaining) {
      setChatError("A message can contain up to four images. Remove one before adding another.");
      return;
    }
    setUploadingImage(true);
    setChatError(undefined);
    try {
      const acceptedFiles = files.slice(0, remaining);
      const uploaded = await Promise.all(acceptedFiles.map(async (file) => {
        if (!["image/png", "image/jpeg", "image/webp"].includes(file.type)) {
          throw new Error(`${file.name} is not a PNG, JPEG, or WebP image.`);
        }
        if (file.size > 20 * 1024 * 1024) throw new Error(`${file.name} exceeds 20 MiB.`);
        const dataUrl = await new Promise<string>((resolve, reject) => {
          const reader = new FileReader();
          reader.onerror = () => reject(new Error(`Could not read ${file.name}.`));
          reader.onload = () => resolve(String(reader.result));
          reader.readAsDataURL(file);
        });
        const result = await api.uploadChatImage({
          engagementId: engagement.id,
          filename: file.name,
          mediaType: file.type as "image/png" | "image/jpeg" | "image/webp",
          contentBase64: dataUrl.slice(dataUrl.indexOf(",") + 1),
        });
        return {
          filename: file.name,
          previewUrl: URL.createObjectURL(file),
          block: {
            type: "image" as const,
            artifactId: result.artifactId,
            mediaType: result.mediaType,
            alt: file.name,
            metadata: {
              preview_artifact_id: result.previewArtifactId,
              width: result.width,
              height: result.height,
            },
          },
        };
      }));
      setPendingImages((current) => [...current, ...uploaded]);
      if (files.length > acceptedFiles.length) {
        setChatError(`Attached ${acceptedFiles.length} image${acceptedFiles.length === 1 ? "" : "s"}; a message can contain up to four.`);
      }
    } catch (error) {
      logCaughtDiagnostic("interface.chat.image_upload_failed", "A chat image could not be attached.", error, "chat-media");
      setChatError(error instanceof Error ? error.message : "The image could not be attached.");
    } finally {
      setUploadingImage(false);
    }
  };

  const attachImages = async (event: ChangeEvent<HTMLInputElement>) => {
    const files = [...(event.target.files ?? [])];
    event.target.value = "";
    await attachImageFiles(files);
  };

  const pasteComposerImages = (event: ReactClipboardEvent<HTMLTextAreaElement>) => {
    const files = [...event.clipboardData.files].filter((file) => file.type.startsWith("image/"));
    if (!files.length) return;
    event.preventDefault();
    void attachImageFiles(files);
  };

  const dropComposerImages = (event: ReactDragEvent<HTMLFormElement>) => {
    const files = [...event.dataTransfer.files].filter((file) => file.type.startsWith("image/"));
    if (!files.length) return;
    event.preventDefault();
    void attachImageFiles(files);
  };

  const removePendingImage = (index: number) => {
    setPendingImages((current) => {
      const target = current[index];
      if (target) URL.revokeObjectURL(target.previewUrl);
      return current.filter((_, currentIndex) => currentIndex !== index);
    });
  };

  const selectHarnessSkill = (skill: HarnessSkillSummary) => {
    const token = skillToken;
    if (!token) return;
    const visibleToken = `$${skill.name}`;
    const nextDraft = `${draft.slice(0, token.start)}${visibleToken}${draft.slice(token.end)}`;
    setDraft(nextDraft);
    setHarnessSkillPath(skill.path);
    setSkillToken(undefined);
    setSkillMenuIndex(0);
    globalThis.requestAnimationFrame?.(() => {
      const composer = composerRef.current;
      if (!composer) return;
      const caret = token.start + visibleToken.length;
      composer.focus();
      composer.setSelectionRange(caret, caret);
    });
  };

  const updateComposerDraft = (nextDraft: string, caret = nextDraft.length) => {
    setDraft(nextDraft);
    const skillEnabled = runtimeKind === "provider" || (
      Boolean(selectedHarness?.capabilities?.skillInvocation)
      && Boolean(selectedHarness?.nativeCapabilities.skills)
    );
    const nextToken = skillEnabled ? findHarnessSkillToken(nextDraft, caret) : undefined;
    setSkillToken(nextToken);
    setSkillMenuIndex(0);
    if (selectedHarnessSkill && !nextDraft.includes(`$${selectedHarnessSkill.name}`)) {
      setHarnessSkillPath("");
    }
  };

  const submit = async (event?: FormEvent, queuedFollowUp?: ChatFollowUp, queueOptions?: { paused?: boolean; first?: boolean; key?: string; uncertain?: boolean }, resent?: { text: string; contentBlocks?: ConversationMessage["contentBlocks"] }) => {
    event?.preventDefault();
    if (sessionId && !sessionReadReady) return;
    const activeTurn = composerBusy;
    if (activeTurn && !queuedFollowUp && !queueOptions) {
      const text = draft.trim();
      const canSteer = sending
        && !isHarnessCommand(text)
        && runtimeKind === "harness"
        && Boolean(selectedHarness?.capabilities?.steering)
        && Boolean(harnessProgress?.turnId || harnessActivity?.turnId)
        && !harnessControlBusy
        && !pendingImages.length
        && !assistantDrafts.length;
      if (canSteer && text) {
        await steerCurrentHarness(text);
      } else if (text || pendingImages.length || assistantDrafts.length) {
        await submit(undefined, undefined, {});
      }
      return;
    }
    const content = (resent?.text ?? queuedFollowUp?.text ?? draft.trim()) || (!resent && pendingImages.length ? "Attached image" : "");
    const providerRuntime = runtimeKind === "provider" ? selectedProvider : undefined;
    const harnessRuntime = runtimeKind === "harness" ? selectedHarness : undefined;
    if (!content || (!queueOptions && composerBusy) || !api || coreState !== "online" || !engagement || (!providerRuntime && !harnessRuntime) || !model.trim()) return;

    const failQueuedFollowUp = (detail: string) => {
      if (!queuedFollowUp) return;
      followUpAutoDrainRef.current = false;
      setQueuedFollowUps((current) => current.map((item) => item.id === queuedFollowUp.id
        ? { ...item, status: "failed", detail }
        : item));
    };

    const wantsKnowledge = canUseKnowledge;
    const knowledgeRuntimeIsLocal = runtimeKind === "harness" ? harnessIsLocal : providerIsLocal;
    const allowCloudKnowledge = wantsKnowledge && !knowledgeRuntimeIsLocal;

    const wantsTools = browserControlEnabled || (runtimeKind === "harness"
      ? Boolean(harnessSessionId
        ? harnessSessions.find((item) => item.id === harnessSessionId)?.mcpServerIds.length
        : selectedMcpIds.length)
      : canUseTools || selectedMcpIds.length > 0);
    let allowCloudToolResults = false;
    const toolRuntimeIsLocal = runtimeKind === "harness" ? harnessIsLocal : providerIsLocal;
    const toolRuntimeName = runtimeKind === "harness" ? harnessRuntime?.name : providerRuntime?.name;
    const toolRuntimePermitsSensitive = runtimeKind === "harness" ? harnessRuntime?.permitsSensitiveData : providerRuntime?.permitsSensitiveData;
    const toolSharingRuntime: ToolSharingRuntime | undefined = runtimeKind === "harness"
      ? harnessRuntime && { kind: "harness", profile: harnessRuntime }
      : providerRuntime && { kind: "provider", profile: providerRuntime };
    if (wantsTools && !toolRuntimeIsLocal && toolRuntimeName) {
      if (!toolRuntimePermitsSensitive) {
        const detail = "This runtime profile does not permit tool results to leave the device.";
        setChatError(detail);
        failQueuedFollowUp(detail);
        return;
      }
      allowCloudToolResults = sharesToolResultsAlways(toolSharingRuntime) || await confirm({
        title: "Share redacted tool results?",
        message: `Allow this turn to send bounded tool inputs and results to ${toolRuntimeName}? Canonical output remains local and risky calls still require approval.`,
        confirmLabel: "Allow this turn",
        ...(api && toolSharingRuntime
          ? { remember: rememberToolSharing(api, toolSharingRuntime, rememberToolSharingRuntime) }
          : {}),
      });
      if (!allowCloudToolResults) {
        failQueuedFollowUp("Tool-result sharing was cancelled. Review the queued message before retrying.");
        return;
      }
    }

    let contextAttachments: ChatCompletionRequest["contextAttachments"];
    try {
      contextAttachments = !queuedFollowUp && !resent && assistantDrafts.length
        ? await Promise.all(assistantDrafts.map(createHashedSelectionAttachment))
        : undefined;
    } catch (attachmentError) {
      void logCaughtDiagnostic("interface.sessions_page.caught_failure_12", "A handled interface operation failed.", attachmentError, "sessions_page");
      const detail = attachmentError instanceof Error ? attachmentError.message : "Could not attach the selected text.";
      setChatError(detail);
      failQueuedFollowUp(detail);
      return;
    }

    // Core returns an archived conversation to the active list when the operator writes to it.
    if (sessionId) setSessions((current) => current.map((item) => item.id === sessionId && item.archivedAt ? { ...item, archivedAt: undefined } : item));

    if (queuedFollowUp && !queueOptions) {
      setQueuedFollowUps((current) => current.map((item) => item.id === queuedFollowUp.id
        ? { ...item, status: "sending", detail: undefined }
        : item));
    }

    const now = new Date().toISOString();
    const userId = makeId("user");
    const assistantId = makeId("assistant");
    const durableHistory = messages.filter((message) => message.durable && message.state === "complete");
    const outgoingContentBlocks = resent
      ? resent.contentBlocks ?? [{ type: "text" as const, text: content }]
      : queuedFollowUp
      ? [{ type: "text" as const, text: content }]
      : [
        ...(draft.trim() ? [{ type: "text" as const, text: draft.trim() }] : []),
        ...pendingImages.map((image) => image.block),
      ];
    const userMessage: ConversationMessage = {
      id: userId,
      role: "user",
      content,
      createdAt: now,
      citations: [],
      state: "complete",
      durable: false,
      contentBlocks: outgoingContentBlocks,
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
    const initialSessionId = sessionId || undefined;
    let returnedSessionId = initialSessionId;
    const chatRequest: ChatCompletionRequest = {
      backend: runtimeKind,
      providerId: providerRuntime?.id,
      harnessProfileId: harnessRuntime?.id,
      harnessSessionId: !initialSessionId && harnessSessionId ? harnessSessionId : undefined,
      mcpServerIds: selectedMcpIds,
      sshEnvironmentIds: runtimeKind === "provider" ? environmentIdsForTarget(environmentTarget) : undefined,
      hookIds: runtimeKind === "provider" ? selectedHookIds : undefined,
      engagementId: engagement.id,
      sessionId: returnedSessionId,
      goalId: runtimeKind === "provider" && providerGoal?.status === "running" ? providerGoal.id : undefined,
      skill: runtimeKind === "provider" && selectedHarnessSkill
        ? { name: selectedHarnessSkill.name, path: selectedHarnessSkill.path }
        : undefined,
      model: model.trim(),
      messages: returnedSessionId
        ? [{ role: "user", content, contentBlocks: userMessage.contentBlocks }]
        : [
            ...durableHistory.map(({ role, content: historyContent, contentBlocks }) => ({ role, content: historyContent, contentBlocks })),
            { role: "user", content, contentBlocks: userMessage.contentBlocks },
          ],
      contextAttachments,
      includeKnowledge: wantsKnowledge,
      allowCloudKnowledge,
      toolsEnabled: runtimeKind === "provider" ? canUseTools : wantsTools,
      allowCloudToolResults,
      harnessMode: runtimeKind === "harness" ? harnessMode || undefined : undefined,
      harnessReasoningEffort: runtimeKind === "harness"
        ? harnessReasoningEffort
        : undefined,
      reasoningEffort: runtimeKind === "provider" && reasoningEffort
        ? reasoningEffort
        : undefined,
      allowSubagents: runtimeKind === "provider" && allowSubagents,
      harnessServiceTier: runtimeKind === "harness"
        ? harnessServiceTier
        : undefined,
      harnessSkill: runtimeKind === "harness" && selectedHarnessSkill
        ? { name: selectedHarnessSkill.name, path: selectedHarnessSkill.path }
        : undefined,
      runtimeSwitchConfirmation: runtimeKind === "provider" ? runtimeSwitchConfirmation : undefined,
    };
    if (queueOptions) {
      if (!sessionId) { setChatError("Send the first message to save this conversation before queueing follow-ups."); return; }
      const accepted = await coreQueue.enqueue(chatRequest, queueOptions);
      if (!accepted) return;
      if (!queuedFollowUp) {
        setDraft(current => current.trim() === content ? "" : current);
        pendingImages.forEach(image => URL.revokeObjectURL(image.previewUrl));
        setPendingImages([]);
        clearSubmittedContext();
      }
      setMessageActionStatus("Saved in Core. Queued work continues after all browser tabs close.");
      return true;
    }
    setMessages((current) => [...current, userMessage, assistantMessage]);
    if (!queuedFollowUp && !resent) {
      if (activeDraftStorageKey) clearChatDraft(sessionStorage, activeDraftStorageKey);
      setDraft("");
      pendingImages.forEach((image) => URL.revokeObjectURL(image.previewUrl));
      setPendingImages([]);
      clearSubmittedContext();
    }
    setChatError(undefined);
    setFailedProviderRecovery(undefined);
    setSending(true);
    if (chatRequest.goalId) {
      liveGoalStreamTextRef.current = content;
      setLiveGoalTokenEstimate(estimateLiveTokens(content));
    } else {
      liveGoalStreamTextRef.current = "";
      setLiveGoalTokenEstimate(0);
    }
    if (runtimeKind === "harness") {
      setHarnessProgress({
        phase: "queued",
        detail: harnessActivity?.busy
          ? "Existing work is active; Core will start an independent harness session for this request."
          : "Request accepted locally and waiting for the harness connection.",
        sessionId: harnessSessionId || undefined,
      });
    }
    const controller = new AbortController();
    abortRef.current = controller;
    streamBackendRef.current = runtimeKind;
    if (chatRequest.harnessSkill || chatRequest.skill) {
      // The structured invocation belongs to this accepted turn only. The
      // visible `$skill-name` remains in the submitted transcript, while the
      // composer is ready for the next request immediately.
      setHarnessSkillPath("");
      setSkillToken(undefined);
    }

    let requestCompleted = false;
    try {
      const response = await api.streamChat(chatRequest, (streamEvent) => {
        if (controller.signal.aborted) return;
        if (streamEvent.type === "started") {
          returnedSessionId = streamEvent.sessionId ?? returnedSessionId;
        }
        if (streamEvent.type === "done") returnedSessionId = streamEvent.sessionId ?? returnedSessionId;
        applyChatEvent(streamEvent, assistantId, userId, chatRequest);
      }, controller.signal);
      requestCompleted = true;
      if (controller.signal.aborted || detachedStreamsRef.current.has(controller)) return;
      returnedSessionId = response?.sessionId ?? returnedSessionId;
      if (response && returnedSessionId) {
        await refreshSessions(returnedSessionId);
        if (chatRequest.goalId) {
          await readProviderGoal();
          liveGoalStreamTextRef.current = "";
          setLiveGoalTokenEstimate(0);
        }
        if (runtimeKind === "provider") setRuntimeSwitchConfirmation(undefined);
        if (runtimeKind === "harness") {
          const authoritative = await api.listChatMessages(returnedSessionId);
          const recovered = await recoverHarnessHistory(
            authoritative.map(persistedMessage), turnId => api.getHarnessTurn(turnId),
          );
          setMessages(current => current.some(message => message.id === userId)
            ? recovered : current);
        }
      }
    } catch (error) {
      const detached = detachedStreamsRef.current.has(controller);
      if (detached) {
        failQueuedFollowUp("The response was detached before this follow-up completed. Review it before retrying.");
        return;
      }
      if (requestCompleted) {
        setChatError(error instanceof Error ? error.message : "The response completed, but the conversation could not be refreshed.");
        return;
      }
      void logCaughtDiagnostic("interface.sessions_page.caught_failure_13", "A handled interface operation failed.", error, "sessions_page");
      followUpAutoDrainRef.current = false;
      const cancelled = controller.signal.aborted;
      const detail = cancelled ? "Response stopped by the operator." : error instanceof Error ? error.message : "Chat completion failed.";
      if (queuedFollowUp) {
        setQueuedFollowUps((current) => current.map((item) => item.id === queuedFollowUp.id
          ? { ...item, status: "failed", detail }
          : item));
      }
      setMessages((current) => current.map((message) => message.id === assistantId
        ? { ...message, state: cancelled ? "cancelled" : "error", detail }
        : message));
      setChatError(cancelled ? undefined : detail);
      const failedTurnId = activeProviderTurnIdRef.current;
      const finalAnswerRetryable = Boolean(
        !cancelled
        && runtimeKind === "provider"
        && failedTurnId
        && error instanceof ApiError
        && error.code === "provider_final_answer_missing"
      );
      if (finalAnswerRetryable && failedTurnId) {
        setFailedProviderRecovery({
          turnId: failedTurnId,
          assistantId,
          request: chatRequest,
        });
      }
      if (returnedSessionId && !cancelled) {
        try {
          const authoritative = await api.listChatMessages(returnedSessionId);
          if (authoritative.length) {
            const recovered = await recoverHarnessHistory(authoritative.map(persistedMessage), turnId => api.getHarnessTurn(turnId));
            setMessages((current) => {
              if (!finalAnswerRetryable) return recovered;
              const failedAssistant = current.find((message) => message.id === assistantId);
              return failedAssistant ? [...recovered, failedAssistant] : recovered;
            });
          }
          await refreshSessions(returnedSessionId);
        } catch (caughtError) {
          void logCaughtDiagnostic("interface.sessions_page.caught_failure_14", "A handled interface operation failed.", caughtError, "sessions_page");
          // Keep the visible safe error when Core history cannot be refreshed.
          setSessionId(initialSessionId ?? "");
        }
      } else if (!initialSessionId) {
        setSessionId("");
      }
    } finally {
      if (queuedFollowUp && followUpDrainIdRef.current === queuedFollowUp.id) {
        followUpDrainIdRef.current = undefined;
      }
      if (queuedFollowUp && requestCompleted) {
        setQueuedFollowUps((current) => current.filter((item) => item.id !== queuedFollowUp.id));
      }
      if (abortRef.current === controller) {
        abortRef.current = undefined;
        streamBackendRef.current = undefined;
        setChatReconnecting(false);
        setSending(false);
      }
      if (chatRequest.goalId && !requestCompleted) {
        void readProviderGoal().finally(() => {
          liveGoalStreamTextRef.current = "";
          setLiveGoalTokenEstimate(0);
        });
      }
    }
  };



  const pendingSshApproval = pendingResponse ? sshApprovalTarget(pendingResponse.approval) : undefined;
  const alwaysAllowSshHost = async (alias: string) => {
    if (!api || approvalDecisionBusy) return;
    try {
      await api.saveSshEnvironment(alias, { commandApproval: "allow" });
    } catch (saveError) {
      void logCaughtDiagnostic("interface.sessions.ssh_always_allow_failed", "A handled interface operation failed.", saveError, "sessions");
      setChatError(saveError instanceof Error ? saveError.message : "Could not update the host's approval setting.");
      return;
    }
    await decideInlineApproval("approve");
  };
  const decideInlineApproval = async (decision: "approve" | "edit" | "reject" | "stop") => {
    if (!pendingResponse || !api || approvalDecisionBusy) return;
    const selectionGeneration = sessionSelectionGenerationRef.current;
    const selectionIsCurrent = () => sessionSelectionGenerationRef.current === selectionGeneration;
    const approvalId = typeof pendingResponse.approval.id === "string"
      ? pendingResponse.approval.id
      : undefined;
    if (!approvalId) {
      setChatError("The pending approval is missing its durable ID.");
      return;
    }
    setApprovalDecisionBusy(true);
    try {
      let editedArguments: Record<string, unknown> | undefined;
      if (decision === "edit") {
        const exact = pendingResponse.approval.exact_request;
        const current = exact && typeof exact === "object" && "arguments" in exact
          ? (exact as Record<string, unknown>).arguments
          : {};
        const edited = globalThis.prompt(
          "Edit the exact JSON arguments before approval",
          JSON.stringify(current ?? {}, null, 2),
        );
        if (edited === null) return;
        const parsed: unknown = JSON.parse(edited);
        if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
          throw new Error("Edited arguments must be one JSON object.");
        }
        editedArguments = parsed as Record<string, unknown>;
      }
      await resolveApproval(approvalId, {
        decision: decision === "edit" ? "approve" : decision,
        editedArguments,
      });
      if (!selectionIsCurrent()) return;
      refreshSessionState();
      if (pendingResponse.request.backend === "harness") {
        if (decision === "stop") {
          followUpAutoDrainRef.current = false;
          setMessages((current) => current.map((message) => message.id === pendingResponse.assistantId
            ? { ...message, state: "cancelled", elapsedMs: elapsedSince(message.createdAt), detail: "Response stopped by the operator." }
            : message));
        } else {
          setMessages((current) => current.map((message) => message.id === pendingResponse.assistantId
            ? { ...message, state: "streaming" }
            : message));
        }
        setPendingResponse(undefined);
        return;
      }
      if (decision === "stop") {
        followUpAutoDrainRef.current = false;
        setMessages((current) => current.map((message) => message.id === pendingResponse.assistantId
        ? { ...message, state: "cancelled", elapsedMs: elapsedSince(message.createdAt), detail: "Response stopped by the operator." }
          : message));
        setPendingResponse(undefined);
        return;
      }
      setSending(true);
      setMessages((current) => current.map((message) => message.id === pendingResponse.assistantId
        ? { ...message, state: "streaming" }
        : message));
      const response = await api.resumeChatTurn(
        pendingResponse.turnId,
        pendingResponse.request,
        (streamEvent) => selectionIsCurrent() && applyChatEvent(
          streamEvent,
          pendingResponse.assistantId,
          pendingResponse.userId,
          pendingResponse.request,
        ),
      );
      if (!selectionIsCurrent()) return;
      if (response?.sessionId) {
        await refreshSessions(response.sessionId);
      }
    } catch (error) {
      void logCaughtDiagnostic("interface.sessions_page.caught_failure_15", "A handled interface operation failed.", error, "sessions_page");
      if (selectionIsCurrent()) setChatError(error instanceof Error ? error.message : "Could not resume the response.");
    } finally {
      if (selectionIsCurrent()) { setApprovalDecisionBusy(false); setSending(false); }
    }
  };

  const resumeInterruptedResponse = async () => {
    if (!api || !interruptedRecovery || interruptedRecovery.turn.recoveryBlocked) return;
    const recovery = interruptedRecovery;
    // Own the viewer transport so a conversation switch detaches the resumed
    // stream and its replayed ledger events cannot land in another transcript.
    const stream = beginGuardedStream({ generation: sessionSelectionGenerationRef, abort: abortRef, backend: streamBackendRef }, recovery.request.backend);
    setInterruptedRecovery(undefined);
    setSending(true);
    setChatError(undefined);
    setMessages((current) => current.map((message) => message.id === recovery.assistantId
      ? { ...message, state: "streaming", detail: undefined }
      : message));
    try {
      const response = await api.resumeChatTurn(
        recovery.turn.id,
        recovery.request,
        stream.guard((streamEvent) => applyChatEvent(streamEvent, recovery.assistantId, "", recovery.request)),
        stream.controller.signal,
      );
      if (!stream.isCurrent()) return;
      if (response?.sessionId) await refreshSessions(response.sessionId);
    } catch (error) {
      void logCaughtDiagnostic("interface.sessions_page.recovery_resume_failed", "An interrupted provider response could not resume.", error, "sessions_page");
      if (!stream.isCurrent()) return;
      const cancelled = stream.controller.signal.aborted;
      const detail = cancelled ? "Response stopped by the operator." : error instanceof Error ? error.message : "Could not resume the interrupted response.";
      if (!cancelled) setInterruptedRecovery(recovery);
      setMessages((current) => current.map((message) => message.id === recovery.assistantId
        ? { ...message, state: cancelled ? "cancelled" : "error", detail }
        : message));
      setChatError(cancelled ? undefined : detail);
    } finally {
      stream.release();
      if (stream.isCurrent()) setSending(false);
    }
  };

  const retryProviderFinalAnswer = async () => {
    if (!api || !failedProviderRecovery) return;
    const recovery = failedProviderRecovery;
    const stream = beginGuardedStream(
      { generation: sessionSelectionGenerationRef, abort: abortRef, backend: streamBackendRef },
      "provider",
    );
    setFailedProviderRecovery(undefined);
    setSending(true);
    setChatError(undefined);
    setMessages((current) => current.map((message) => message.id === recovery.assistantId
      ? { ...message, state: "streaming", detail: undefined }
      : message));
    try {
      const response = await api.resumeChatTurn(
        recovery.turnId,
        recovery.request,
        stream.guard((streamEvent) => applyChatEvent(
          streamEvent,
          recovery.assistantId,
          "",
          recovery.request,
        )),
        stream.controller.signal,
      );
      if (!stream.isCurrent()) return;
      if (response?.sessionId) await refreshSessions(response.sessionId);
    } catch (error) {
      void logCaughtDiagnostic(
        "interface.sessions_page.final_answer_retry_failed",
        "A provider final answer could not be retried.",
        error,
        "sessions_page",
      );
      if (!stream.isCurrent()) return;
      const cancelled = stream.controller.signal.aborted;
      const detail = cancelled
        ? "Response stopped by the operator."
        : error instanceof Error
          ? error.message
          : "Could not retry the final answer.";
      if (
        !cancelled
        && error instanceof ApiError
        && error.code === "provider_final_answer_missing"
      ) {
        setFailedProviderRecovery(recovery);
      }
      setMessages((current) => current.map((message) => message.id === recovery.assistantId
        ? { ...message, state: cancelled ? "cancelled" : "error", detail }
        : message));
      setChatError(cancelled ? undefined : detail);
    } finally {
      stream.release();
      if (stream.isCurrent()) setSending(false);
    }
  };

  const reconcileInterruptedEffect = async (outcome: "complete" | "failed") => {
    if (!api || !interruptedRecovery || recoveryBusy) return;
    const toolCallId = interruptedRecovery.turn.unresolvedToolCallIds[0];
    const hookExecutionId = interruptedRecovery.turn.unresolvedHookExecutionIds[0];
    const detail = recoveryNote.trim();
    if ((!toolCallId && !hookExecutionId) || !detail) return;
    setRecoveryBusy(true);
    setChatError(undefined);
    try {
      const turn = toolCallId
        ? await api.reconcileChatTool(interruptedRecovery.turn.id, {
          expectedRevision: interruptedRecovery.turn.revision,
          toolCallId,
          outcome,
          detail,
        })
        : await api.reconcileChatHook(interruptedRecovery.turn.id, {
          expectedRevision: interruptedRecovery.turn.revision,
          hookExecutionId: hookExecutionId!,
          outcome,
          detail,
        });
      setInterruptedRecovery((current) => current ? { ...current, turn } : current);
      if (hookExecutionId) setHookExecutions(await api.listChatHookExecutions(turn.id));
      setRecoveryNote("");
    } catch (error) {
      void logCaughtDiagnostic("interface.sessions_page.recovery_reconcile_failed", "An interrupted effect outcome could not be reconciled.", error, "sessions_page");
      setChatError(error instanceof Error ? error.message : "Could not reconcile the interrupted effect outcome.");
    } finally {
      setRecoveryBusy(false);
    }
  };

  const onComposerKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (skillToken && event.key === "Escape") {
      event.preventDefault();
      setSkillToken(undefined);
      return;
    }
    if (skillToken && matchingHarnessSkills.length > 0) {
      if (event.key === "ArrowDown" || event.key === "ArrowUp") {
        event.preventDefault();
        setSkillMenuIndex((current) => {
          const delta = event.key === "ArrowDown" ? 1 : -1;
          return (current + delta + matchingHarnessSkills.length) % matchingHarnessSkills.length;
        });
        return;
      }
      if (event.key === "Escape") {
        event.preventDefault();
        setSkillToken(undefined);
        return;
      }
      if (event.key === "Enter" || event.key === "Tab") {
        event.preventDefault();
        const skill = matchingHarnessSkills[skillMenuIndex];
        if (skill) selectHarnessSkill(skill);
        return;
      }
    }
    if (event.key === "ArrowUp" && !event.altKey && !event.ctrlKey && !event.metaKey && !event.shiftKey && draft.length === 0) {
      // An empty textarea has no caret movement to consume ArrowUp, so WebKit can
      // pass the key through to the scrolling workbench. Keep history navigation
      // owned by the composer even when there is no message to restore.
      event.preventDefault();
      let lastUserMessage: ConversationMessage | undefined;
      for (let index = messages.length - 1; index >= 0; index -= 1) {
        if (messages[index]?.role === "user") {
          lastUserMessage = messages[index];
          break;
        }
      }
      if (lastUserMessage) {
        setDraft(lastUserMessage.content);
        globalThis.requestAnimationFrame?.(() => {
          const composer = composerRef.current;
          if (composer) composer.setSelectionRange(composer.value.length, composer.value.length);
        });
      }
      return;
    }
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      event.currentTarget.form?.requestSubmit();
    }
  };

  const openArtifacts = async (card: ToolLifecycleCard) => {
    setArtifactInspector(card);
    setArtifactQuery("");
    setArtifactSearch(undefined);
    setArtifactRead(undefined);
    setArtifactError(undefined);
    if (!api) return;
    setArtifactBusy(true);
    try {
      const artifacts = await api.listToolCallArtifacts(card.toolCallId);
      setArtifactInspector((current) => current?.toolCallId === card.toolCallId
        ? { ...current, artifacts }
        : current);
    } catch (listError) {
      void logCaughtDiagnostic("interface.sessions_page.caught_failure_20", "A handled interface operation failed.", listError, "sessions_page");
      setArtifactError(listError instanceof Error ? listError.message : "Could not list all tool artifacts.");
    } finally {
      setArtifactBusy(false);
    }
  };

  const searchArtifacts = async (event: FormEvent) => {
    event.preventDefault();
    if (!api || !artifactInspector || !artifactQuery.trim()) return;
    setArtifactBusy(true); setArtifactError(undefined); setArtifactRead(undefined);
    try {
      setArtifactSearch(await api.searchToolOutput(artifactInspector.toolCallId, artifactQuery.trim()));
    } catch (searchError) {
      void logCaughtDiagnostic("interface.sessions_page.caught_failure_17", "A handled interface operation failed.", searchError, "sessions_page");
      setArtifactError(searchError instanceof Error ? searchError.message : "Could not search tool artifacts.");
    } finally { setArtifactBusy(false); }
  };

  const readArtifact = async (artifactId: string, startingLine = 1) => {
    if (!api) return;
    setArtifactBusy(true); setArtifactError(undefined);
    try {
      setArtifactRead(await api.readToolOutput(artifactId, startingLine));
    } catch (readError) {
      void logCaughtDiagnostic("interface.sessions_page.caught_failure_18", "A handled interface operation failed.", readError, "sessions_page");
      setArtifactError(readError instanceof Error ? readError.message : "Could not read the artifact.");
    } finally { setArtifactBusy(false); }
  };

  const decideHarnessInteraction = async (
    interaction: HarnessInteraction,
    action: "answer" | "decline",
  ) => {
    if (!api || harnessControlBusy) return;
    setHarnessControlBusy(true);
    setChatError(undefined);
    try {
      let response: Record<string, unknown> = {};
      if (action === "answer") {
        const raw = interactionAnswers[interaction.id] ?? "";
        if (interaction.kind === "mcp_elicitation") {
          const parsed: unknown = JSON.parse(raw || "{}");
          if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
            throw new Error("MCP form input must be a JSON object.");
          }
          response = parsed as Record<string, unknown>;
        } else {
          const questions = interaction.questions;
          response = Object.fromEntries(questions.map((question, index) => [
            typeof question.id === "string" ? question.id : `question_${index + 1}`,
            interactionAnswers[`${interaction.id}:${typeof question.id === "string" ? question.id : index}`] ?? raw,
          ]));
        }
      }
      const updated = await api.decideHarnessInteraction(interaction.id, action, response);
      setHarnessInteractions((current) => current.map((item) => item.id === updated.id ? updated : item));
      setInteractionAnswers((current) => Object.fromEntries(
        Object.entries(current).filter(([key]) => key !== interaction.id && !key.startsWith(`${interaction.id}:`)),
      ));
    } catch (error) {
      void logCaughtDiagnostic("interface.sessions_page.interaction_decision", "Could not resolve harness interaction.", error, "sessions_page");
      setChatError(error instanceof Error ? error.message : "Could not resolve harness input.");
    } finally {
      setHarnessControlBusy(false);
    }
  };

  const stopCurrentResponse = async (): Promise<boolean> => {
    followUpAutoDrainRef.current = false;
    const reloadSessionId = sessionId;
    if (runtimeKind === "harness" && api) {
      const turnId = harnessProgress?.turnId ?? harnessActivity?.turnId
        ?? [...messages].reverse().find((message) => message.state === "streaming")?.harnessTurnId;
      if (turnId) {
        try {
          await api.stopHarnessTurn(turnId);
        } catch (error) {
          void logCaughtDiagnostic("interface.sessions_page.harness_stop", "Could not stop harness turn.", error, "sessions_page");
          setChatError(error instanceof Error ? error.message : "Could not stop the harness turn.");
          return false;
        }
      }
    } else if (api) {
      const turnId = activeProviderTurnIdRef.current ?? pendingResponse?.turnId
        ?? (authoritativeState?.busy ? authoritativeState.turn_id : undefined);
      if (turnId) {
        try {
          await api.cancelChatTurn(turnId);
        } catch (error) {
          void logCaughtDiagnostic("interface.sessions_page.provider_stop", "Could not stop provider turn.", error, "sessions_page");
          setChatError(error instanceof Error ? error.message : "Could not stop the provider turn.");
          return false;
        }
      }
    }
    harnessFollowDetachRef.current?.();
    harnessFollowDetachRef.current = undefined;
    abortRef.current?.abort();
    activeProviderTurnIdRef.current = undefined;
    setPendingResponse(undefined);
    setHarnessProgress(undefined);
    setMessages((current) => cancelActiveAssistantMessage(current));
    setSending(false);
    if (runtimeKind === "harness" && reloadSessionId) {
      await selectSession(reloadSessionId, false);
    } else if (api && reloadSessionId) {
      try {
        setProviderGoal(await api.getChatGoal(reloadSessionId));
      } catch (error) {
        if (!(error instanceof ApiError && error.status === 404)) {
          void logCaughtDiagnostic("interface.sessions.goal_stop_refresh_failed", "The paused goal could not be refreshed after stopping its response.", error, "goal");
          setProviderGoalError(error instanceof Error ? error.message : "Goal status could not be refreshed.");
        }
      }
    }
    return true;
  };

  const stopAndSend = async () => {
    if (harnessControlBusy) return;
    if (!await coreQueue.mutate({action: "pause"})) return;
    if (!await submit(undefined, undefined, {paused: true, first: true})) return;
    setHarnessControlBusy(true);
    const stopped = await stopCurrentResponse();
    setHarnessControlBusy(false);
    if (stopped) await coreQueue.mutate({action: "resume"});
  };

  const steerCurrentHarness = async (guidance?: string) => {
    if (!api || harnessControlBusy) return;
    const turnId = harnessProgress?.turnId ?? harnessActivity?.turnId;
    if (!turnId) return;
    const text = guidance?.trim();
    if (!text) {
      composerRef.current?.focus();
      return;
    }
    setHarnessControlBusy(true);
    setChatError(undefined);
    try {
      await api.steerHarnessTurn(turnId, text);
      if (activeDraftStorageKey) clearChatDraft(sessionStorage, activeDraftStorageKey);
      setDraft("");
      setMessageActionStatus("Guidance sent to the active harness turn.");
    } catch (error) {
      void logCaughtDiagnostic("interface.sessions_page.harness_steer", "The active harness turn could not be steered.", error, "sessions_page");
      setChatError(error instanceof Error ? error.message : "Could not steer the harness turn.");
    } finally {
      setHarnessControlBusy(false);
    }
  };

  const retryHarnessMessage = async (message: ConversationMessage) => {
    if (!api || !message.harnessTurnId || harnessControlBusy) return;
    const approved = await confirm({
      title: "Retry harness turn?",
      message: "This starts a linked new turn and preserves the failed execution unchanged.",
      confirmLabel: "Start retry",
    });
    if (!approved) return;
    setHarnessControlBusy(true);
    try {
      await api.retryHarnessTurn(message.harnessTurnId);
      setHarnessProgress({ phase: "queued", detail: "Linked retry queued.", turnId: message.harnessTurnId });
      if (sessionId) await selectSession(sessionId);
    } catch (error) {
      void logCaughtDiagnostic("interface.sessions_page.harness_retry", "The harness turn could not be retried.", error, "sessions_page");
      setChatError(error instanceof Error ? error.message : "Could not retry the harness turn.");
    } finally {
      setHarnessControlBusy(false);
    }
  };

  const rewindCheckpoint = async (item: HarnessActivityItem) => {
    const checkpointSessionId = item.sessionId ?? harnessSessionId;
    if (!api || !checkpointSessionId || !item.itemId || harnessControlBusy) return;
    const approved = await confirm({
      title: "Rewind files to checkpoint?",
      message: "The harness will restore tracked files to this checkpoint. This is available only while the session is idle.",
      confirmLabel: "Rewind files",
      tone: "danger",
    });
    if (!approved) return;
    setHarnessControlBusy(true);
    try {
      await api.rewindHarnessCheckpoint(checkpointSessionId, item.itemId);
    } catch (error) {
      void logCaughtDiagnostic("interface.sessions_page.harness_rewind", "The harness checkpoint could not be rewound.", error, "sessions_page");
      setChatError(error instanceof Error ? error.message : "Could not rewind the checkpoint.");
    } finally {
      setHarnessControlBusy(false);
    }
  };

  const stopSubagent = async (item: HarnessActivityItem) => {
    if (!api || !item.turnId || !item.itemId || harnessControlBusy) return;
    setHarnessControlBusy(true);
    try {
      await api.stopHarnessSubagent(item.turnId, item.itemId);
    } catch (error) {
      void logCaughtDiagnostic("interface.sessions_page.harness_subagent_stop", "The harness subagent could not be stopped.", error, "sessions_page");
      setChatError(error instanceof Error ? error.message : "Could not stop the subagent.");
    } finally {
      setHarnessControlBusy(false);
    }
  };

  const saveRawArtifact = async (artifact: ToolArtifactReference) => {
    if (!api || !await confirm({
      title: "Open raw tool output?",
      message: "Raw tool output may contain secrets, exploit payloads, or untrusted instructions. Acknowledge this data boundary before saving the immutable artifact.",
      confirmLabel: "Acknowledge and save",
    })) return;
    try {
      const downloaded = await api.downloadToolArtifact(artifact.artifactId);
      const url = URL.createObjectURL(downloaded.blob);
      const anchor = document.createElement("a");
      anchor.href = url; anchor.download = downloaded.filename ?? artifact.filename ?? artifact.artifactId; anchor.click();
      URL.revokeObjectURL(url);
    } catch (downloadError) {
      void logCaughtDiagnostic("interface.sessions_page.caught_failure_19", "A handled interface operation failed.", downloadError, "sessions_page");
      setArtifactError(downloadError instanceof Error ? downloadError.message : "Could not save the artifact.");
    }
  };

  const continueAsMission = async () => {
    const chat = sessions.find((item) => item.id === sessionId);
    const objective = [...messages].reverse().find((item) => item.role === "user")?.content;
    if (!engagement || !chat?.harnessProfileId || !chat.harnessSessionId || !objective) return;
    try {
      const harness = harnesses.find((item) => item.id === chat.harnessProfileId);
      const attached = harnessSessions.find((item) => item.id === chat.harnessSessionId);
      let allowCloudToolResults = false;
      if (attached?.mcpServerIds.length && harness && !harness.localOnly) {
        if (!harness.permitsSensitiveData) {
          setChatError("This harness profile is text-only. Permit project/document data in Settings before continuing with MCP.");
          return;
        }
        allowCloudToolResults = await confirm({
          title: "Allow MCP results in this mission?",
          message: `Allow bounded MCP tool inputs and results to reach ${harness.name} for the continued mission?`,
          confirmLabel: "Continue as mission",
        });
        if (!allowCloudToolResults) return;
      }
      await startMission({
        engagementId: engagement.id,
        name: `Continue: ${chat.title ?? "assistant investigation"}`,
        objective,
        backend: "harness",
        harnessProfileId: chat.harnessProfileId,
        harnessSessionId: chat.harnessSessionId,
        model: chat.model,
        allowCloudToolResults,
      });
      setView("missions");
    } catch (error) {
      void logCaughtDiagnostic("interface.sessions_page.caught_failure_16", "A handled interface operation failed.", error, "sessions_page");
      setChatError(error instanceof Error ? error.message : "Could not continue this chat as a mission.");
    }
  };

  useEffect(() => {
    if (!sessionActionsId) return;
    const closeOnOutsidePointer = (event: PointerEvent) => {
      const target = event.target as Node;
      if (sessionActionsMenuRef.current?.contains(target) || sessionActionsButtonRef.current?.contains(target)) return;
      setSessionActionsId(undefined);
    };
    const closeOnEscape = (event: globalThis.KeyboardEvent) => {
      if (event.key !== "Escape") return;
      event.preventDefault();
      setSessionActionsId(undefined);
      sessionActionsButtonRef.current?.focus();
    };
    const closeOnViewportChange = () => setSessionActionsId(undefined);
    document.addEventListener("pointerdown", closeOnOutsidePointer);
    document.addEventListener("keydown", closeOnEscape);
    document.addEventListener("scroll", closeOnViewportChange, true);
    window.addEventListener("resize", closeOnViewportChange);
    requestAnimationFrame(() => sessionActionsMenuRef.current?.querySelector<HTMLButtonElement>('[role="menuitem"]')?.focus());
    return () => {
      document.removeEventListener("pointerdown", closeOnOutsidePointer);
      document.removeEventListener("keydown", closeOnEscape);
      document.removeEventListener("scroll", closeOnViewportChange, true);
      window.removeEventListener("resize", closeOnViewportChange);
    };
  }, [sessionActionsId]);
  useEffect(() => {
    if (!assistantSettingsOpen) return;
    const closeOnOutsidePointer = (event: PointerEvent) => {
      const target = event.target as Node;
      if (assistantSettingsPanelRef.current?.contains(target) || assistantSettingsButtonRef.current?.contains(target) || isGuideLayerTarget(event.target)) return;
      if (event.target instanceof Element && event.target.closest("[data-assistant-settings-trigger]")) return;
      setAssistantSettingsOpen(false);
    };
    const closeOnEscape = (event: globalThis.KeyboardEvent) => {
      if (event.key !== "Escape") return;
      event.preventDefault();
      setAssistantSettingsOpen(false);
      assistantSettingsButtonRef.current?.focus();
    };
    document.addEventListener("pointerdown", closeOnOutsidePointer);
    document.addEventListener("keydown", closeOnEscape);
    requestAnimationFrame(() => assistantSettingsPanelRef.current?.focus());
    return () => {
      document.removeEventListener("pointerdown", closeOnOutsidePointer);
      document.removeEventListener("keydown", closeOnEscape);
    };
  }, [assistantSettingsOpen]);
  const authoritativeProviderBusy = runtimeKind === "provider" && Boolean(authoritativeState?.busy);
  const composerBusy = sending || authoritativeProviderBusy || pendingResponseActive || Boolean(interruptedRecovery) || Boolean(waitingCallback);
  const unresolvedHookExecution = interruptedRecovery
    ? hookExecutions.find((item) => interruptedRecovery.turn.unresolvedHookExecutionIds.includes(item.id))
    : undefined;
  const unresolvedHookLabel = unresolvedHookExecution
    ? nativeHooks.find((hook) => hook.id === unresolvedHookExecution.hookId)?.manifest.name ?? unresolvedHookExecution.hookId
    : interruptedRecovery?.turn.unresolvedHookExecutionIds[0];
  const canSend = Boolean((!sessionId || sessionReadReady) && api && coreState === "online" && engagement && runtimeReady && model.trim() && (draft.trim() || pendingImages.length) && !composerBusy && !uploadingImage);
  const canSteerCurrentHarness = Boolean(
    !isHarnessCommand(draft)
    && sending
    && runtimeKind === "harness"
    && selectedHarness?.capabilities?.steering
    && (harnessProgress?.turnId || harnessActivity?.turnId)
    && !harnessControlBusy,
  );
  const canStopAndSend = Boolean(
    sending
    && (runtimeKind === "provider" || (selectedHarness?.capabilities?.interruption === true && (harnessProgress?.turnId || harnessActivity?.turnId)))
    && !harnessControlBusy,
  );
  const queueMode = composerBusy && !canSteerCurrentHarness;
  const [reviewPendingRequested, setReviewPendingRequested] = useState(false);
  const focusPendingAction = (card: HTMLDivElement | null) => {
    if (!card || !reviewPendingRequested || loadingHistory || reloadingConversation) return;
    card.scrollIntoView({ block: "center", behavior: "instant" });
    card.focus({ preventScroll: true });
    setReviewPendingRequested(false);
  };
  const reviewPendingActions = async () => {
    await reloadActiveConversation();
    setReviewPendingRequested(true);
  };
  const reloadActiveConversation = async () => {
    if (!sessionId || reloadingConversation) return;
    setReloadingConversation(true);
    await selectSession(sessionId, false);
    refreshSessionState();
    setReloadingConversation(false);
  };
  const visibleHarnessProgress: HarnessProgress | undefined = authoritativeState && harnessSessionId
    ? {phase: authoritativeState.execution === "continuing" ? "decision_recorded" : authoritativeState.execution === "idle" ? "ready" : authoritativeState.execution,
       detail: authoritativeState.detail, sessionId: harnessSessionId, turnId: authoritativeState.harness_turn_id ?? undefined}
    : runtimeKind !== "harness" || !harnessSessionId
    ? harnessProgress
    : harnessProgress ?? (harnessActivityError
      ? { phase: "status_unavailable", detail: harnessActivityError, sessionId: harnessSessionId }
      : harnessActivity
        ? {
            phase: harnessActivity.busy
              ? harnessActivity.turnStatus === "waiting_approval" ? "waiting_approval" : harnessActivity.turnStatus ?? "running"
              : "ready",
            detail: harnessActivity.detail,
            sessionId: harnessActivity.sessionId,
            turnId: harnessActivity.turnId,
          }
        : { phase: "connecting", detail: "Reading authoritative harness activity from Core.", sessionId: harnessSessionId });
  const showHarnessProgress = runtimeKind === "harness"
    && Boolean(visibleHarnessProgress)
    && visibleHarnessProgress?.phase !== "ready"
    && visibleHarnessProgress?.phase !== "complete";
  const runnableLanguages = useMemo(() => new Set<ExecutionLanguage>(
    executionCapabilities?.runtimes
      .filter((runtime) => runtime.unrestrictedNetwork)
      .map((runtime) => runtime.language) ?? [],
  ), [executionCapabilities]);
  const assistantRunnableLanguages = useMemo(() => new Set<ExecutionLanguage>([...runnableLanguages, "bash", "sh"]), [runnableLanguages]);
  const pendingHarnessRequests = authoritativeState?.pending.length ?? harnessInteractions.filter((item) => item.status === "pending").length;
  const decisionNotice = authoritativeState
    ? authoritativeState.execution === "continuing" ? authoritativeState.decisions.at(-1) : undefined
    : resolvedApproval;
  const showHarnessStatusRail = runtimeKind === "harness"
    && (!decisionNotice || pendingHarnessRequests > 0)
    && Boolean(harnessActivity)
    && Boolean((authoritativeState?.busy ?? harnessActivity?.busy) || pendingHarnessRequests);
  const assistantSource = runtimeKind === "harness"
    ? selectedHarness?.name ?? "Agent harness"
    : selectedProvider?.name ?? "Model provider";
  const runtimeConfiguration = [
    model,
    runtimeKind === "harness" && harnessReasoningEffort ? `${harnessReasoningEffort} effort` : "",
    runtimeKind === "harness" && harnessServiceTier ? `${harnessServiceTier} speed` : "",
  ].filter(Boolean).join(" · ");

  const browserEngine = searchParams.get("browserEngine") === "native" || (!searchParams.has("browserEngine") && searchParams.has("browserTool")) ? "native" : "managed";
  const setBrowserEngine = (engine: "managed" | "native") => {
    updateSearchParams(params => params.set("browserEngine", engine), { replace: true });
  };
  const [browserAssistantOpen, setBrowserAssistantOpen] = useState(false);
  const attachBrowserContext = useCallback((request: Parameters<typeof requestChatContext>[0]) => { setBrowserAssistantOpen(true); requestChatContext(request, "browser"); }, [requestChatContext]);
  const [browserControlsOpen, setBrowserControlsOpen] = useState(true);
  const [browserControlEnabled, setBrowserControlEnabled] = useState(false);
  const [terminalToolbarHost, setTerminalToolbarHost] = useState<HTMLDivElement | null>(null);
  const [browserActionContainer, setBrowserActionContainer] = useState<HTMLDivElement | null>(null);
  const runInTerminal = useCallback((candidate: FencedRunCandidate) => {
    setTerminalCommandRequest({ id: globalThis.crypto?.randomUUID?.() ?? `${Date.now()}`, source: candidate.source });
    if (view === "chat" && !compact) {
      // Keep the conversation in place and run beside it.
      setChatTerminalOpen(true);
      return;
    }
    // Phones give the shell the whole screen; the command's output stays visible.
    setTerminalAssistantOpen(!compact);
    setView("terminal");
  }, [compact, setChatTerminalOpen, setView, view]);
  // A phone never splits the chat: the terminal is its own tab there.
  const chatTerminalVisible = view === "chat" && chatTerminalOpen && !compact && Boolean(api && engagement);
  const chatTerminalSize = useResizableSidePanel({
    defaultWidth: 520,
    enabled: chatTerminalVisible && !chatTerminalStacked,
    label: "Resize terminal",
    maxWidth: 960,
    minPrimaryWidth: 420,
    minWidth: 360,
    storageKey: "nebula.chat-side-terminal.width",
  });
  const collapseBrowserAssistant = () => {
    setBrowserAssistantOpen(false);
    requestAnimationFrame(() => document.querySelector<HTMLButtonElement>('button[aria-controls="browser-assistant-panel"]')?.focus({ preventScroll: true }));
  };

  const [transcriptSearchOpen, setTranscriptSearchOpen] = useState(false);
  const transcriptSearchButtonRef = useRef<HTMLButtonElement>(null);
  useEffect(() => { setTranscriptSearchOpen(false); }, [sessionId, view]);
  const closeTranscriptSearch = () => {
    setTranscriptSearchOpen(false);
    // Phones open search from the Conversation actions menu; focus returns there.
    (transcriptSearchButtonRef.current ?? mobileConversationActionsRef.current)?.focus();
  };
  const transcriptSearchAction = <button ref={transcriptSearchButtonRef} type="button" className="icon-button subtle transcript-search-toggle" data-guide="transcript-search" aria-label="Search messages and bookmarks" title="Search messages and bookmarks" aria-expanded={transcriptSearchOpen} aria-controls={transcriptSearchOpen ? "assistant-transcript-search" : undefined} onClick={() => setTranscriptSearchOpen(open => !open)}><Search size={18} aria-hidden="true" /></button>;

  const toolAssistanceAction = api && engagement && <PostToolAssistant api={api} engagementId={engagement.id} providers={providers} harnesses={harnesses} onRun={setRunCandidate} />;
  const focusAction = <button className="icon-button subtle workbench-full-screen-toggle" type="button"
            aria-label={fullScreen ? "Exit full screen workbench" : "Enter focus mode"}
            title={fullScreen ? "Exit focus mode" : "Enter focus mode"}
            aria-pressed={fullScreen} onClick={() => setFullScreen((value) => !value)}>
            {fullScreen ? <Minimize2 size={18} aria-hidden="true" /> : <Maximize2 size={18} aria-hidden="true" />}
          </button>;
  const conversationTitle = sessions.find(session => session.id === sessionId)?.title
    ?? (loadingHistory ? "Loading conversation…" : conversationOpen ? "New conversation" : "No conversation open");
  const conversationActions = (
        <div className="session-toolbar-actions" role="toolbar" aria-label="Conversation actions">
          {view === "chat" && conversationOpen && transcriptSearchAction}
          {toolAssistanceAction}
          {view === "chat" && api && engagement && <button className="icon-button subtle" type="button"
            data-guide="terminal-toggle"
            aria-label={chatTerminalOpen ? "Hide terminal" : "Show terminal"}
            title={chatTerminalOpen ? "Hide terminal" : "Show terminal beside the chat"}
            aria-pressed={chatTerminalOpen}
            aria-controls="chat-side-terminal"
            onClick={() => setChatTerminalOpen(!chatTerminalOpen)}><SquareTerminal size={18} aria-hidden="true" /></button>}
          {view === "chat" && <button className="icon-button subtle" type="button"
            aria-label={sessionInspectorOpen ? "Hide session details" : "Show session details"}
            title={sessionInspectorOpen ? "Hide session details" : "Show session details"}
            aria-expanded={sessionInspectorOpen}
            onClick={() => setSessionInspectorOpen((open) => {
              localStorage.setItem("nebula.session-inspector.open", String(!open));
              return !open;
            })}><PanelRight size={18} aria-hidden="true" /></button>}
          {focusAction}
        </div>
  );

  const assistantPanel = (
            <div className="chat-panel">
              <ChatSearchPanel open={transcriptSearchOpen} onClose={closeTranscriptSearch} key={`search:${sessionId || "new"}`} search={chatNavigation.search} onSelect={(hit) => {
                updateSearchParams(next => {next.set("session", hit.session_id); next.set("message", hit.message_id); next.set("view", view === "browser" ? "browser" : "chat");});
              }} />
              {sessions.find(item => item.id === sessionId)?.parentSessionId && <div className="chat-action-status">Branched conversation · files remain shared. <button className="button quiet" onClick={() => void selectSession(sessions.find(item => item.id === sessionId)!.parentSessionId!)}>Open parent</button></div>}
              {chatNavigation.error && <div role="alert">{chatNavigation.error}<button className="button quiet" onClick={chatNavigation.reload}>Reload bookmarks</button></div>}
              {assistantSettingsOpen && createPortal(<section ref={assistantSettingsPanelRef} className="chat-settings-popover chat-settings-floating" id="assistant-settings-popover" role="dialog" aria-modal="false" aria-labelledby="assistant-settings-title" tabIndex={-1}>
                <header role="presentation"><strong id="assistant-settings-title">Assistant settings</strong><button className="icon-button subtle" type="button" aria-label="Close assistant settings" onClick={() => { setAssistantSettingsOpen(false); assistantSettingsButtonRef.current?.focus(); }}><X size={16} aria-hidden="true" /></button></header>
                <div className="chat-context-bar">
                <div className="chat-settings-fields" data-guide="assistant-runtime">
                <label><span>Runtime</span><select aria-label="Chat runtime" value={runtimeKind} disabled={composerBusy} onChange={(event) => { const next = event.target.value as "provider" | "harness"; if (engagement) runtimeDefaultEngagementRef.current = engagement.id; setRuntimeKind(next); setHarnessSessionId(""); setSelectedMcpIds([]); setAssistantSettingsStatus("Runtime updated. Applies to your next message."); if (next === "provider") selectProvider(providerId || enabledProviders[0]?.id || ""); else { setModel(selectedHarness?.defaultModel?.trim() || selectedHarness?.models[0] || ""); } }}><option value="provider">Provider</option><option value="harness">Agent harness</option></select></label>
                {runtimeKind === "provider" ? <label><span>Provider</span><select aria-label="Chat provider" value={providerId} disabled={composerBusy} onChange={(event) => { const nextProvider = enabledProviders.find(item => item.id === event.target.value); void proposeProviderRuntime(event.target.value, providerDefaultModel(nextProvider)); }}><option value="">Select provider</option>{enabledProviders.map((provider) => <option value={provider.id} key={provider.id}>{provider.name} · {provider.state}</option>)}</select></label> : <><label><span>Harness</span><select aria-label="Chat harness" value={harnessId} disabled={composerBusy} onChange={(event) => { const profile = harnesses.find(item => item.id === event.target.value); setHarnessSessionId(""); setSelectedMcpIds([]); setHarnessId(event.target.value); setModel(profile?.defaultModel || profile?.models[0] || ""); setAssistantSettingsStatus("Harness updated. Applies to your next message."); }}><option value="">Select harness</option>{harnesses.map((harness) => <option value={harness.id} key={harness.id}>{harness.name}</option>)}</select></label></>}
                {runtimeKind === "provider" ? <>{(selectedProvider?.models.length ?? 0) + unlistedProviderModels.length > 8 && <label><span>Find model</span><input type="search" value={providerModelQuery} placeholder="Search name or model ID" onChange={(event) => setProviderModelQuery(event.target.value)} /></label>}<label title={selectedProvider?.message}><span>Model</span><select aria-label="Chat model" aria-busy={modelDiscoveryInProgress} value={model} disabled={composerBusy || modelDiscoveryInProgress || (!selectedProvider?.models.length && !unlistedProviderModels.length)} onChange={(event) => void chooseProviderModel(event.target.value)}><option value="">{modelPlaceholder}</option>{selectedModelIsUnavailable && <option value={model}>{model} · saved model</option>}{filteredUnlistedProviderModels.length > 0
                  ? <><optgroup label="Allowed models">{filteredProviderModels.map((item) => <option value={item} key={item}>{modelOptionLabel(item, selectedProvider?.modelDescriptors)}</option>)}</optgroup><optgroup label={`More ${selectedProvider?.name ?? "provider"} models · adds to allowed`}>{filteredUnlistedProviderModels.map((item) => <option value={item} key={item}>{modelOptionLabel(item, selectedProvider?.modelDescriptors)}</option>)}</optgroup></>
                  : filteredProviderModels.map((item) => <option value={item} key={item}>{modelOptionLabel(item, selectedProvider?.modelDescriptors)}</option>)}</select>{selectedModelSummary && <small>{selectedModelSummary}</small>}</label></> : <label><span>Model</span><select aria-label="Chat harness model" value={model} disabled={composerBusy || !harnessModelOptions.length} onChange={(event) => { setModel(event.target.value); setAssistantSettingsStatus("Model updated. Applies to your next message."); }}><option value="">{harnessModelOptions.length ? "Select model" : "Run a harness check to discover models"}</option>{harnessModelOptions.map((item) => <option value={item} key={item}>{item}</option>)}</select></label>}
                {runtimeKind === "provider" && <label><span>Effort</span><select aria-label="Reasoning effort" value={reasoningEffort} disabled={composerBusy || assistantSettingsBusy} onChange={(event) => { const next = event.target.value as ReasoningEffort | ""; setReasoningEffort(next); void saveProviderAssistantSelections(selectedMcpIds, selectedHookIds, next); }}><option value="">Model default</option>{REASONING_EFFORTS.map((item) => <option value={item} key={item}>{item === "none" ? "None · answer only" : item}</option>)}</select></label>}{runtimeKind === "harness" && (harnessReasoningEfforts.length > 0 || harnessReasoningEffort) && <label><span>Effort</span><select aria-label="Harness reasoning effort" value={harnessReasoningEffort} disabled={composerBusy} onChange={(event) => { setHarnessReasoningEffort(event.target.value); setAssistantSettingsStatus("Effort updated. Applies to your next message."); }}><option value="">Harness default</option>{harnessReasoningEffort && !harnessReasoningEfforts.some((item) => item.id === harnessReasoningEffort) && <option value={harnessReasoningEffort}>{harnessReasoningEffort} · saved</option>}{harnessReasoningEfforts.map((item) => <option title={item.description || undefined} value={item.id} key={item.id}>{item.label}</option>)}</select></label>}
                {runtimeKind === "harness" && (harnessServiceTiers.length > 0 || harnessServiceTier) && <label><span>Speed</span><select aria-label="Harness speed" value={harnessServiceTier} disabled={composerBusy} onChange={(event) => { setHarnessServiceTier(event.target.value); setAssistantSettingsStatus("Speed updated. Applies to your next message."); }}><option value="">Harness default</option>{harnessServiceTier && !harnessServiceTiers.some((item) => item.id === harnessServiceTier) && <option value={harnessServiceTier}>{harnessServiceTier} · saved</option>}{harnessServiceTiers.map((item) => <option title={item.description || undefined} value={item.id} key={item.id}>{item.label}</option>)}</select></label>}
                {runtimeKind === "harness" && Boolean(selectedHarness?.capabilities?.modes.length) && <label><span>Mode</span><select aria-label="Chat harness mode" value={harnessMode} disabled={sending} onChange={(event) => setHarnessMode(event.target.value)}><option value="">Harness default</option>{selectedHarness?.capabilities?.modes.map((item) => <option value={item} key={item}>{item === "plan" || item === "planning" ? "Planning" : item.replaceAll("_", " ")}</option>)}</select></label>}
                </div>
                {assistantSettingsStatus && <p className="provider-dialog-note" role="status">{assistantSettingsStatus}</p>}
                {assistantSettingsError && <p className="provider-dialog-note error" role="alert">{assistantSettingsError}</p>}
                {runtimeKind === "harness" && <details className="chat-advanced-session"><summary>Resume existing session</summary><label><span>Filter by name</span><input type="search" aria-label="Filter resumable sessions by name" value={externalSessionQuery} placeholder="Search Codex or Grok sessions" onChange={(event) => setExternalSessionQuery(event.target.value)} /></label><label><span>Session</span><select aria-label="Chat harness session" value={harnessSessionId} disabled={sending || Boolean(sessionId) || externalSessionsLoading} onChange={(event) => void selectHarnessSession(event.target.value)}><option value="">New session</option>{harnessSessions.filter((item) => (item.harnessProfileId === harnessId || item.id === harnessSessionId) && (item.id === harnessSessionId || item.displayName.toLocaleLowerCase().includes(externalSessionQuery.trim().toLocaleLowerCase()))).map((item) => <option value={item.id} key={item.id}>{item.displayName} · {item.model}{item.reasoningEffort ? ` · ${item.reasoningEffort}` : ""}{item.serviceTier ? ` · ${item.serviceTier}` : ""} · {item.status} · {new Date(item.lastActivityAt).toLocaleString()}</option>)}{externalHarnessSessions.filter((item) => !item.internalSessionId && item.displayName.toLocaleLowerCase().includes(externalSessionQuery.trim().toLocaleLowerCase())).map((item) => <option value={`external:${item.externalSessionId}`} key={`external:${item.externalSessionId}`}>{item.displayName}{item.model ? ` · ${item.model}` : ""}{item.updatedAt ? ` · ${new Date(item.updatedAt).toLocaleString()}` : ""}</option>)}</select></label>{externalSessionsLoading && <p className="provider-dialog-note" role="status">Loading resumable sessions…</p>}{externalSessionsError && <p className="provider-dialog-note error" role="alert">{externalSessionsError}</p>}</details>}
                {sessionId && <p className="provider-dialog-note">Changes apply to your next message. Conversation history is kept.</p>}
                <div className="chat-settings-capabilities">
                {runtimeKind === "harness" && selectedHarness?.capabilities?.skillInvocation && !selectedHarness.nativeCapabilities.skills && <div className="chat-knowledge-toggle" role="status"><ShieldCheck size={15} /><span>Skills unavailable<small>Enable installed skills for this harness in Settings.</small></span></div>}
                {runtimeKind === "harness" && selectedHarness && !selectedHarness.capabilities?.skillInvocation && <div className="chat-knowledge-toggle" role="status"><ShieldCheck size={15} /><span>Skills unavailable<small>This harness did not advertise structured skill invocation.</small></span></div>}
                {runtimeKind === "harness" && selectedHarness?.capabilities?.skillInvocation && selectedHarness.nativeCapabilities.skills && <div className="chat-knowledge-toggle" role="status"><span><strong>Skills</strong><small>{harnessSkillError ?? (harnessSkillsLoading ? "Discovering project and installed skills…" : harnessSkills.length ? "Type $ in the composer to invoke a skill for one turn." : "No project or installed skills were discovered.")}</small></span></div>}
                {runtimeKind === "provider" && <div className="chat-knowledge-toggle" role="status"><span><strong>Skills</strong><small>{harnessSkillError ?? (harnessSkillsLoading ? "Discovering project skills…" : harnessSkills.length ? "Type $ in the composer to select a skill. Running goals keep its immutable snapshot." : "No project skills were discovered.")}</small></span></div>}
                {runtimeKind === "provider" && <div className="chat-harness-mcp" data-guide="lifecycle-hooks"><span>Lifecycle hooks</span>{nativeHookError ? <><small role="alert">{nativeHookError}</small><ShowMeHow guide="lifecycle-hooks" step={1} label="Fix with guide" /></> : nativeHooks.length ? nativeHooks.map(hook => <label className="chat-knowledge-toggle" key={hook.id}><input type="checkbox" checked={selectedHookIds.includes(hook.id)} disabled={composerBusy || assistantSettingsBusy} onChange={(event) => void saveProviderAssistantSelections(selectedMcpIds, event.target.checked ? [...selectedHookIds, hook.id] : selectedHookIds.filter(id => id !== hook.id))} /><span>{hook.manifest.name}<small>{hook.manifest.description || hook.id} · {hook.manifest.events.length} events · {hook.manifest.failurePolicy} on failure</small></span></label>) : <><small>No project hooks found in .agents/hooks.</small><ShowMeHow guide="lifecycle-hooks" /></>}</div>}
                <div className="chat-knowledge-toggle" role="status" data-guide="knowledge-status"><ShieldCheck size={15} aria-hidden="true" /><span>Knowledge<small>{knowledgeItemCount ? runtimePermitsKnowledge ? `${knowledgeItemCount} source${knowledgeItemCount === 1 ? "" : "s"} available automatically` : `${runtimeKind === "provider" ? "Profile" : "Harness"} is text-only` : "No sources loaded"}</small></span></div>
                {runtimeKind === "provider" && <label className="chat-knowledge-toggle" data-guide="subagents"><input type="checkbox" checked={allowSubagents} disabled={sending || assistantSettingsBusy} onChange={(event) => { setAllowSubagents(event.target.checked); setAssistantSettingsStatus(event.target.checked ? "Subagents allowed. Applies to your next message." : "Subagents turned off. Applies to your next message."); }} /><span><strong>Subagents</strong><small>Let the assistant delegate independent work to parallel children on this model, tools and approval policy</small></span></label>}
                {runtimeKind === "provider" ? <><div className="chat-knowledge-toggle" role="status" title={commandRuntimeUnavailableReason}><ShieldCheck size={15} /><span>Command runtime<small>{canUseTools ? "run_command and process_io ready" : commandRuntimeUnavailableReason}</small></span></div><div className="chat-harness-mcp" data-guide="mcp-turn"><span>MCP servers</span>{mcpServers.length ? mcpServers.map((server) => <label className="chat-knowledge-toggle" key={server.id}><input type="checkbox" checked={selectedMcpIds.includes(server.id)} disabled={sending || assistantSettingsBusy} onChange={(event) => void saveProviderAssistantSelections(event.target.checked ? [...selectedMcpIds, server.id] : selectedMcpIds.filter((id) => id !== server.id), selectedHookIds)} /><span>{server.name}<small>{server.tools.length} tools · Core-captured</small></span></label>) : <small>No enabled MCP profiles</small>}</div></> : <div className="chat-harness-mcp" data-guide="mcp-turn"><span>MCP servers</span>{mcpServers.length ? mcpServers.map((server) => <label className="chat-knowledge-toggle" key={server.id}><input type="checkbox" checked={selectedMcpIds.includes(server.id)} disabled={composerBusy} onChange={(event) => setSelectedMcpIds((current) => event.target.checked ? [...current, server.id] : current.filter((id) => id !== server.id))} /><span>{server.name}<small>{server.tools.length} tools · {server.defaultApproval.replace("_", " ")}</small></span></label>) : <small>No enabled MCP profiles</small>}</div>}
                </div>
                <AssistantSetupLinks items={[
                  { entry: settingCatalogEntry(runtimeKind === "provider" ? "settings.providers" : "settings.harnesses"), icon: runtimeKind === "provider" ? Settings2 : Bot, label: runtimeKind === "provider" ? "Model providers" : "Assistant harnesses", detail: runtimeKind === "provider" ? `${enabledProviders.length} enabled` : `${harnesses.filter((item) => item.enabled).length} enabled` },
                  { entry: settingCatalogEntry("settings.mcp"), icon: Boxes, label: "MCP tools", detail: mcpServers.length ? `${mcpServers.length} available` : "None configured" },
                  { entry: settingCatalogEntry("settings.automation-runtime"), icon: SquareTerminal, label: "Command runtime", detail: canUseTools ? "Ready" : "Needs attention" },
                  { entry: settingCatalogEntry("settings.follow-up"), icon: Sparkles, label: "Tool follow-up", detail: "Notes and next steps" },
                ]} onOpen={(entry) => {
                  setAssistantSettingsOpen(false);
                  openSetting?.(entry, assistantSettingsButtonRef.current);
                }} />
                </div>
              </section>, document.body)}
              <AssistantRuntimeProvider runtime={chatRuntime} key={sessionId || "new-conversation"}>
                <ThreadPrimitive.Root className="chat-thread">
                  {loadingHistory && messages.length > 0 && <div className="chat-thinking chat-syncing" role="status"><LoaderCircle className="spin" size={14} /> Showing saved messages · syncing…</div>}
                  <ThreadPrimitive.Viewport
                    ref={chatViewportRef}
                    className="chat-scroll"
                    aria-live="polite"
                    onScroll={(event) => {
                      const viewport = event.currentTarget;
                      if (!viewport.clientHeight) return;
                      const geometry = {scrollTop: viewport.scrollTop, scrollHeight: viewport.scrollHeight, clientHeight: viewport.clientHeight};
                      const atBottom = followsChatBottom(chatScrollGeometryRef.current, geometry, chatFollowBottomRef.current);
                      chatScrollGeometryRef.current = geometry;
                      chatReadingPositionRef.current = {sessionId, scrollTop: viewport.scrollTop, followBottom: atBottom};
                      chatFollowBottomRef.current = atBottom;
                      setHasNewerMessages(!atBottom);
                    }}
                    onWheel={event => {if (event.deltaY < 0) chatFollowBottomRef.current = false;}}
                    onTouchStart={event => {chatTouchYRef.current = event.touches[0]?.clientY;}}
                    onTouchMove={event => {const y = event.touches[0]?.clientY; if (y !== undefined && chatTouchYRef.current !== undefined && y > chatTouchYRef.current) chatFollowBottomRef.current = false; chatTouchYRef.current = y;}}
                    onKeyDown={event => {if (["ArrowUp", "PageUp", "Home"].includes(event.key)) chatFollowBottomRef.current = false;}}
                    onPointerDown={event => {if (event.target === event.currentTarget) chatFollowBottomRef.current = false;}}
                    autoScroll={!restoredScrollRef.current || restoredScrollRef.current.followBottom}
                    scrollToBottomOnInitialize={!restoredScrollRef.current}
                    scrollToBottomOnRunStart
                    scrollToBottomOnThreadSwitch={!restoredScrollRef.current}
                    turnAnchor="bottom"
                  >
                {leadingReplacements.map((group) => <ReplacedMessages group={group} key={group.id} />)}
                {loadingHistory && !messages.length ? <div className="chat-thinking"><LoaderCircle className="spin" size={14} /> Loading conversation…</div> : messages.length ? <ThreadPrimitive.Messages>{({ message: threadMessage }) => {
                  const message = messagesById.get(threadMessage.id);
                  if (!message) return null;
                  const editing = messageEdit?.messageId === message.id ? messageEdit : undefined;
                  const replacedByEdit = editing
                    ? messages.filter((item) => item.durable && (item.sequence ?? 0) > (editing.sequence ?? 0)).length
                    : 0;
                  const pendingReplacement = Boolean(messageEdit && !editing && message.durable && (message.sequence ?? 0) > (messageEdit.sequence ?? 0));
                  const messageReplacements = anchoredReplacements.get(message.id) ?? [];
                  const messageActivityItems = activityItems.filter((item) => item.assistantId === message.id && shouldShowActivityItem(item));
                  const commentaryItems = messageActivityItems
                    .map((item) => ({ key: item.key, text: item.streams.commentary?.trim() }))
                    .filter((item): item is { key: string; text: string } => Boolean(item.text));
                  const messageToolCards = toolCards.filter((card) => card.assistantId === message.id);
                  const historicalTurnId = (message.durable || message.recoveredHarnessTurn) && message.role === "assistant" ? message.harnessTurnId : undefined;
                  const historicalState = historicalTurnId ? historicalActivityState[historicalTurnId] : undefined;
                  const historicalError = historicalTurnId ? historicalActivityErrors[historicalTurnId] : undefined;
                  const activityLedger = messageActivityItems.length > 0
                    ? activityLedgerFromHarness("Work summary", message.state, messageActivityItems)
                    : messageToolCards.length > 0
                      ? activityLedgerFromNative("Work summary", message.state, messageToolCards)
                      : historicalTurnId
                        ? activityLedgerFromHarness("Work summary", message.state, [])
                        : undefined;
                  return (
                  <article
                    className={`chat-message ${message.role === "user" ? "operator" : "assistant"}${editing ? " editing" : ""}${pendingReplacement ? " pending-replacement" : ""}`}
                    id={`chat-message-${message.id}`}
                    data-sequence={message.sequence}
                    data-selection-source-kind={message.role === "assistant" ? "assistant_message" : "chat_message"}
                    data-selection-source-id={message.id}
                    data-selection-source-label={message.role === "assistant" ? "Assistant response" : "Chat message"}
                    key={message.runtimeId ?? message.id}
                    tabIndex={-1}
                  >
                    <div className="chat-message-body">
                      <header>{message.role === "assistant" && <><strong>{assistantSource}</strong>{runtimeConfiguration && <span>{runtimeConfiguration}</span>}</>}<span className="chat-message-time">{timeLabel(message.createdAt)}</span></header>
                      {message.role === "assistant" && message.toolSuggestions && <ToolSuggestionChip summary={message.toolSuggestions} />}
                      {commentaryItems.length > 0 && <div className={`assistant-commentary${message.state === "streaming" ? " live" : ""}`} aria-label="Assistant commentary" aria-live="polite">
                        {commentaryItems.map((item) => <HarnessMarkdown content={item.text} key={item.key} />)}
                      </div>}
                      {message.role === "assistant" && <HarnessThinking items={messageActivityItems} />}
                      {message.role === "assistant" && <ThinkingDisclosure text={message.reasoning} streaming={message.state === "streaming" && Boolean(message.reasoning)} />}
                      {message.content && (message.role === "assistant"
                        ? <AssistantMarkdown content={message.content} messageId={message.id} durable={message.durable && message.state === "complete"} streaming={message.state === "streaming"} runnableLanguages={assistantRunnableLanguages} onRun={setRunCandidate} onRunInTerminal={runInTerminal} />
                        : editing
                          ? <form className="chat-message-edit" onSubmit={event => {event.preventDefault(); void resendEditedMessage();}}>
                            <textarea
                              aria-label="Edit message"
                              value={editing.text}
                              autoFocus
                              disabled={editing.busy}
                              rows={Math.min(12, Math.max(3, editing.text.split("\n").length + 1))}
                              onChange={event => setMessageEdit(current => current && ({...current, text: event.target.value}))}
                              onKeyDown={event => {
                                if (event.key === "Escape") { event.preventDefault(); cancelMessageEdit(); }
                                if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) { event.preventDefault(); void resendEditedMessage(); }
                              }}
                            />
                            {editing.error && <DiagnosticErrorNotice error={editing.error} fallback="The edited message could not be resent." compact />}
                            <div className="chat-message-edit-actions">
                              <small>{replacedByEdit === 0
                                ? "Resending replaces this message in this conversation."
                                : `Resending replaces this message and the ${replacedByEdit === 1 ? "reply" : `${replacedByEdit} messages`} below. Nothing new is created.`}</small>
                              <button className="button quiet" type="button" disabled={editing.busy} onClick={cancelMessageEdit}>Cancel</button>
                              <button className="button primary" type="submit" disabled={editing.busy || !editing.text.trim()}>{editing.busy ? <><LoaderCircle className="spin" size={13} /> Resending</> : "Resend"}</button>
                            </div>
                          </form>
                          : <p>{message.content}</p>)}
                      {message.id && subagentResultsByMessage.get(message.id) && <ChatSubagentResultCard
                        subagent={subagentResultsByMessage.get(message.id)!}
                        onOpenConversation={id => void selectSession(id)}
                      />}
                      {message.role === "assistant" && message.state === "complete" && !message.content && message.reasoning && <small className="muted" role="status">The model spent this turn thinking and returned no answer. Its thinking is above.</small>}
                      {api && message.contentBlocks?.filter((block) => block.type === "image").map((block, index) => <AuthenticatedChatImage api={api} block={block} key={`${block.artifactId ?? "image"}-${index}`} />)}
                      {historicalState === "failed" && historicalError && <div className="harness-activity-load-error"><DiagnosticErrorNotice error={historicalError} fallback="Saved work details could not be loaded; the answer remains available." compact /><button className="button quiet" type="button" onClick={() => void loadHistoricalHarnessActivity(message)}>Retry work details</button></div>}
                      {message.role === "assistant" && activityLedger && <ActivityLedger
                        compact
                        historyPending={Boolean(historicalTurnId && historicalState !== "loaded" && !messageActivityItems.length && !messageToolCards.length)}
                        model={activityLedger}
                        onExpandedChange={historicalTurnId ? (expanded) => {
                          if (expanded) void loadHistoricalHarnessActivity(message);
                        } : undefined}
                        emptyState={historicalState === "loading"
                          ? <div className="chat-thinking"><LoaderCircle className="spin" size={14} /> Loading saved work…</div>
                          : undefined}
                        renderEntryDetails={(entry) => <AssistantLedgerEntryDetails entry={entry} />}
                        renderEntryActions={(entry) => {
                          const item = entry.sourceItem;
                          const card = entry.sourceTool;
                          return <>
                            {item?.kind === "subagent" && item.status === "running" && selectedHarness?.capabilities?.subagentControl && <button className="button quiet" type="button" disabled={harnessControlBusy} onClick={() => void stopSubagent(item)}>Stop subagent</button>}
                            {item?.type === "checkpoint" && selectedHarness?.capabilities?.checkpointRewind && <button className="button quiet" type="button" disabled={harnessControlBusy || (item.sessionId === harnessSessionId && harnessActivity?.busy)} title={item.sessionId === harnessSessionId && harnessActivity?.busy ? "Checkpoint rewind is available while the session is idle" : undefined} onClick={() => void rewindCheckpoint(item)}>Rewind files here</button>}
                            {card && card.status !== "running" && <button className="button quiet" type="button" onClick={() => void openArtifacts(card)}><Search size={13} /> Artifacts</button>}
                          </>;
                        }}
                      />}
                      {harnessInteractions.filter((interaction) => interaction.harnessTurnId === message.harnessTurnId && interaction.status === "pending" && isPendingRequest(authoritativeState, interaction.id)).map((interaction) => <div className="chat-approval-card harness-interaction" ref={focusPendingAction} tabIndex={-1} role="region" aria-label="Input required" key={interaction.id}>
                        <strong>{interaction.prompt}</strong>
                        {interaction.kind === "user_input" ? interaction.questions.map((question, index) => {
                          const questionId = typeof question.id === "string" ? question.id : String(index);
                          const answerKey = `${interaction.id}:${questionId}`;
                          return <label key={questionId}><span>{typeof question.question === "string" ? question.question : `Question ${index + 1}`}</span>{Array.isArray(question.options) ? <><input type={interaction.containsSecret ? "password" : "text"} list={interaction.containsSecret ? undefined : `answers-${answerKey}`} autoComplete="off" placeholder="Choose a suggestion or write your answer" value={interactionAnswers[answerKey] ?? ""} onChange={(event) => setInteractionAnswers((current) => ({ ...current, [answerKey]: event.target.value }))} /><datalist id={`answers-${answerKey}`}>{question.options.map((option, optionIndex) => <option value={typeof option === "object" && option && "label" in option ? String(option.label) : String(option)} key={optionIndex} />)}</datalist></> : <input type={interaction.containsSecret ? "password" : "text"} value={interactionAnswers[answerKey] ?? ""} onChange={(event) => setInteractionAnswers((current) => ({ ...current, [answerKey]: event.target.value }))} autoComplete="off" />}</label>;
                        }) : <label><span>JSON response</span>{interaction.containsSecret ? <input type="password" value={interactionAnswers[interaction.id] ?? ""} onChange={(event) => setInteractionAnswers((current) => ({ ...current, [interaction.id]: event.target.value }))} autoComplete="off" /> : <textarea rows={3} value={interactionAnswers[interaction.id] ?? ""} onChange={(event) => setInteractionAnswers((current) => ({ ...current, [interaction.id]: event.target.value }))} autoComplete="off" />}</label>}
                        {interaction.containsSecret && <small>Secret answer is forwarded in memory and will not be persisted.</small>}
                        <div><button className="button secondary" type="button" disabled={harnessControlBusy} onClick={() => void decideHarnessInteraction(interaction, "decline")}>Decline</button><button className="button primary" type="button" disabled={harnessControlBusy} onClick={() => void decideHarnessInteraction(interaction, "answer")}>Submit</button></div>
                      </div>)}
                      {message.state === "streaming" && !message.content && <div className="chat-thinking"><span /><span /><span /> {runtimeKind === "harness" ? visibleHarnessProgress?.detail ?? "Waiting for harness" : "Waiting for provider"}</div>}
                      {message.state === "waiting_approval" && pendingResponse?.assistantId === message.id && pendingResponseActive && <div className="chat-approval-card" ref={focusPendingAction} tabIndex={-1} role="region" aria-label="Approval required"><strong>Approval required</strong><AssistantApprovalDetails request={pendingResponse.approval} /><div>{pendingSshApproval && <button className="button quiet" type="button" disabled={approvalDecisionBusy} title={`Stop asking before commands on ${pendingSshApproval.label}, then run this one`} onClick={() => void alwaysAllowSshHost(pendingSshApproval.alias)}>Always allow on this host</button>}<button className="button secondary" type="button" disabled={approvalDecisionBusy} onClick={() => void decideInlineApproval("reject")}>Reject</button><button className="button secondary" type="button" disabled={approvalDecisionBusy} onClick={() => void decideInlineApproval("stop")}>Stop response</button><button className="button primary" type="button" disabled={approvalDecisionBusy} onClick={() => void decideInlineApproval("approve")}>Approve</button></div></div>}
                      {message.state === "cancelled" && <small className="muted" role="status">Stopped{message.elapsedMs !== undefined ? ` · ${formatTurnElapsed(message.elapsedMs)}` : ""}</small>}
                      {message.detail && message.state !== "cancelled" && <DiagnosticErrorNotice error={message.detail} fallback="The response could not be completed." compact />}
                      {runtimeKind === "harness" && ["error", "cancelled"].includes(message.state) && message.harnessTurnId && <button className="button quiet" type="button" disabled={harnessControlBusy} onClick={() => void retryHarnessMessage(message)}>Retry as linked turn</button>}
                      {api && sessionId && message.durable && message.role === "assistant" && <ChatEvidence key={message.id} api={api} sessionId={sessionId} messageId={message.id} onResults={() => updateSearchParams(next => {next.set("drawer", "results");})} />}
                      {message.citations.map((citation) => <Link className="citation-chip" to={`/knowledge?source=${encodeURIComponent(citation.sourceId)}`} title={citation.excerpt} key={`${citation.sourceId}-${citation.chunkId}`}><Braces size={13} /> {citation.name}{citation.page ? ` · p. ${citation.page}` : ""}</Link>)}
                      {message.role === "assistant" && ["streaming", "waiting_approval"].includes(message.state) && <LiveTurnElapsed startedAt={message.createdAt} />}
                      {(() => {
                        const tokens = message.usage && message.usage.totalTokens > 0 ? message.usage : undefined;
                        if (!tokens && message.elapsedMs === undefined) return null;
                        const detail = elapsedDetail(message.elapsedMs, message.approvalWaitMs);
                        const summary = [
                          tokens ? `${tokens.totalTokens.toLocaleString()} tokens` : undefined,
                          message.elapsedMs !== undefined ? formatTurnElapsed(message.elapsedMs) : undefined,
                        ].filter(Boolean).join(" · ");
                        return <details className="chat-message-usage"><summary>{summary}</summary>{tokens && <span>{tokens.inputTokens.toLocaleString()} input · {tokens.outputTokens.toLocaleString()} output</span>}{detail && <span>{detail}</span>}</details>;
                      })()}
                      {message.content && !editing && <footer className="chat-message-actions" data-guide="message-actions" aria-label="Message actions">{message.durable && <><IconAction icon={NotebookPen} label="Save as decision" onClick={event => {const selection = window.getSelection(); const container = event.currentTarget.closest(".chat-message"); const exact = selection && container?.contains(selection.anchorNode) && container.contains(selection.focusNode) ? selection.toString() : ""; const text = exact || message.content; setDecisionSeed({messageId: message.id, text, selection: text}); setSessionInspectorOpen(true); updateSearchParams(next => {next.set("drawer", "context");});}} /><IconAction icon={Bookmark} label="Bookmark" aria-pressed={chatNavigation.bookmarks.some(item => item.message_id === message.id && item.active)} onClick={() => void chatNavigation.toggleBookmark(message.id)} />{message.role === "user" && <IconAction icon={Pencil} label="Edit message" title="Edit and resend in this conversation" disabled={sending} onClick={() => beginMessageEdit(message)} />}</>}<button className="icon-button subtle" type="button" aria-label="Copy message" title="Copy exact message" onClick={() => void copyMessage(message)}><Copy size={14} /></button><button className="icon-button subtle" type="button" aria-label="Quote in composer" title={sending && runtimeKind === "harness" && selectedHarness?.capabilities?.steering ? "Quote as guidance for the active turn" : "Quote in an editable draft"} onClick={() => quoteMessage(message)}><MessageSquareQuote size={14} /></button>{message.durable && sessionId && <button className="icon-button subtle chat-fork-button" type="button" aria-label="Fork conversation here" title="Fork conversation here · files remain shared" disabled={sending} onClick={() => void forkConversation(message)}><GitFork size={14} /></button>}</footer>}
                    </div>
                    {messageReplacements.map((group) => <ReplacedMessages group={group} key={group.id} />)}
                  </article>
                  );
                }}</ThreadPrimitive.Messages> : <div className="empty-state compact"><MessageSquare size={23} /><strong>Start an analyst conversation</strong><p>Ask a question or bring something you want to work on.</p><div className="assistant-starters">{["Ask about this project", "Review a document"].map((label) => <button className="button quiet" type="button" disabled={!runtimeReady} key={label} onClick={() => { updateComposerDraft(label === "Review a document" ? "Please review the document I attach. " : "Help me understand this project. "); composerRef.current?.focus(); }}>{label}</button>)}{imageInputEnabled && <button className="button quiet" type="button" onClick={() => imageInputRef.current?.click()}>Attach images</button>}</div></div>}
                    {messages.length > 0 && hasNewerMessages && <ThreadPrimitive.ScrollToBottom className="chat-scroll-to-bottom" aria-label="Scroll to latest message" title="Scroll to latest message" onClick={() => { chatFollowBottomRef.current = true; }}>
                      <ChevronDown size={16} aria-hidden="true" />
                    </ThreadPrimitive.ScrollToBottom>}
                  </ThreadPrimitive.Viewport>
                </ThreadPrimitive.Root>
              </AssistantRuntimeProvider>
              {decisionNotice && <ResolvedApprovalNotice status={decisionNotice.status}
                busy={reloadingConversation || harnessControlBusy}
                canStop={runtimeKind !== "harness" || selectedHarness?.capabilities?.interruption !== false}
                onCheck={() => void reloadActiveConversation()}
                onStop={() => { if (harnessControlBusy) return; setHarnessControlBusy(true); void stopCurrentResponse().then(stopped => { if (stopped) setResolvedApproval(undefined); }).finally(() => setHarnessControlBusy(false)); }} />}
              <div className="chat-operator-updates">
              {stateSyncError && <div className="chat-recovery-notice" role="status"><p>{stateSyncError}</p><button className="icon-button subtle" type="button" aria-label="Retry response status" title="Retry response status" onClick={refreshSessionState}><RefreshCw size={16} aria-hidden="true" /></button></div>}
              {api && sessionId && <ChatCatchUp key={`catch-up:${sessionId}`} api={api} sessionId={sessionId} pendingActions={authoritativeState?.pending} ready={!loadingHistory} atLatest={!hasNewerMessages} actionRevision={`${pendingResponse?.assistantId ?? ""}:${harnessInteractions.map(item => `${item.id}:${item.status}`).join(",")}`} onTurn={id => updateSearchParams(next => {next.set("turn", id); next.set("drawer", "context");})} onMessage={openDrawerMessage} onPending={() => void reviewPendingActions()} />}
              {runtimeKind === "provider" && hookExecutions.length > 0 && <details className="chat-action-status" data-guide="hook-outcomes" open={Boolean(interruptedRecovery)}><summary>Lifecycle hooks · {hookExecutions.filter(item => item.status === "complete" || item.status === "reconciled").length}/{hookExecutions.length} completed</summary><div role="list" aria-label="Lifecycle hook outcomes">{hookExecutions.map(execution => { const hookName = nativeHooks.find(hook => hook.id === execution.hookId)?.manifest.name ?? execution.hookId; return <div role="listitem" key={execution.id}><strong>{hookName}</strong><small>{execution.eventName.replaceAll(".", " ")} · {execution.status.replaceAll("_", " ")}{execution.sideEffects !== "none" ? ` · ${execution.sideEffects} effects` : ""}</small>{execution.error && <span role="alert">{execution.error}</span>}{execution.reconciliation && typeof execution.reconciliation.detail === "string" && <small>{execution.reconciliation.detail}</small>}</div>; })}</div></details>}
              {waitingCallback && <div className="chat-action-status" role="status">
                <span>{waitingCallback.summary}</span>
                {waitingCallback.resultsUrl && <div className="chat-inline-approval-actions">
                  <code title={waitingCallback.resultsUrl}>{waitingCallback.resultsUrl}</code>
                  <button className="button quiet" type="button" onClick={() => void navigator.clipboard.writeText(waitingCallback.resultsUrl ?? "")}>Copy results URL</button>
                  <small>The command received an API key in NEBULA_RESULTS_KEY. POST the result to this LAN URL.</small>
                </div>}
              </div>}
              {interruptedRecovery && <div className="chat-action-status" role={interruptedRecovery.turn.recoveryBlocked ? "alert" : "status"}>
                <span>{interruptedRecovery.turn.error ?? "Core restarted before this response completed."}</span>
                {interruptedRecovery.turn.recoveryBlocked ? <div className="chat-inline-approval-actions">
                  <label><span>What happened to {interruptedRecovery.turn.unresolvedToolCallIds[0] ? `tool call ${interruptedRecovery.turn.unresolvedToolCallIds[0]}` : unresolvedHookLabel ?? "the interrupted hook"}?</span><input value={recoveryNote} onChange={(event) => setRecoveryNote(event.target.value)} placeholder="Operator verification note" disabled={recoveryBusy} /></label>
                  <button className="button secondary" type="button" disabled={recoveryBusy || !recoveryNote.trim()} onClick={() => void reconcileInterruptedEffect("failed")}>Mark failed</button>
                  <button className="button primary" type="button" disabled={recoveryBusy || !recoveryNote.trim()} onClick={() => void reconcileInterruptedEffect("complete")}>Confirm completed</button>
                  <small>This records your confirmation as unverified evidence. Nebula will not run the effect again.</small>
                </div> : <button className="button quiet" type="button" disabled={sending} onClick={() => void resumeInterruptedResponse()}>Resume response</button>}
              </div>}
              {pendingResponse && pendingResponse.request.backend !== "harness" && <div className="chat-inline-approval-actions"><button className="button secondary" type="button" disabled={approvalDecisionBusy} onClick={() => void decideInlineApproval("edit")}>Edit pending request</button></div>}
              {chatReconnecting && <p role="status" className="chat-recovery-notice">Connection lost. Reconnecting to the existing turn…</p>}
              {chatError && (failedProviderRecovery
                ? // The turn is resumable and needs nothing decided: its tool
                  // results, reasoning and partial state are all preserved and
                  // only the prose is missing. That is not an error to alarm
                  // an operator with, and the conversation stays usable.
                  <div className="chat-recovery-notice unfinished" role="status"><p><strong>The model stopped without finishing this reply.</strong> Everything it did is saved — only the written answer is missing.</p><button className="button quiet" type="button" disabled={sending} onClick={() => void retryProviderFinalAnswer()}>Finish the answer</button></div>
                : <div className="chat-recovery-notice"><DiagnosticErrorNotice error={chatError} fallback="The chat operation could not be completed." compact />{sessionId && <button className="button quiet" type="button" disabled={reloadingConversation} onClick={() => void reloadActiveConversation()}>{reloadingConversation ? "Reloading…" : "Reload conversation"}</button>}</div>)}
              {activeArchivedSession && <div className="chat-archived-notice" role="status"><Archive size={14} aria-hidden="true" /><span>This conversation is archived. Sending a message moves it back to your conversations.</span><button className="button quiet" type="button" disabled={Boolean(archivingSessionId)} onClick={() => void setConversationArchived(activeArchivedSession, false)}>Unarchive</button></div>}
              {messageActionStatus && <div className="chat-action-status" role="status" aria-live="polite"><Check size={13} aria-hidden="true" /> {messageActionStatus}</div>}
              {runtimeKind === "harness" && harnessActivityError && <div className="chat-recovery-notice" role="status"><span>Harness status could not be loaded. Saved messages remain available.</span><button className="button quiet" type="button" onClick={() => void reloadActiveConversation()}>Retry status</button></div>}
              {queuedFollowUps.length > 0 && <section className="chat-follow-up-queue"><strong>Old browser queue</strong><p>Import these messages into Core, paused for review. Uncertain sending entries need review.</p>{queuedFollowUps.map(item => <div key={item.id}><p>{item.text}</p><button type="button" onClick={() => void (async () => { if (await submit(undefined, item, {paused: true, key: `legacy-${item.id}`, uncertain: item.status !== "queued"})) { setQueuedFollowUps(current => current.filter(row => row.id !== item.id)); } })()}>Import paused</button><button type="button" onClick={() => setQueuedFollowUps(current => current.filter(row => row.id !== item.id))}>Discard</button></div>)}</section>}
              </div>
              <form className="chat-composer" onSubmit={(event) => void submit(event)} onDragOver={(event) => { if ([...event.dataTransfer.items].some((item) => item.kind === "file" && item.type.startsWith("image/"))) event.preventDefault(); }} onDrop={dropComposerImages}>
              <div className="chat-composer-context" role="region" aria-label="Composer context and activity" tabIndex={0}>
              {runtimeKind === "provider" && api && <>{providerGoalLoading && <p className="provider-dialog-note" role="status">Loading goal…</p>}{providerGoalError && <p className="provider-dialog-note error" role="alert">{providerGoalError}</p>}{!providerGoalLoading && <ProviderGoalPanel api={api} sessionId={sessionId || undefined} goal={providerGoal} skills={harnessSkills} liveTokenEstimate={liveGoalTokenEstimate} settingsBusy={assistantSettingsBusy} onCreate={createGoalConversation} onChange={setProviderGoal} onWorkDispatched={async () => { if (sessionId) await selectSession(sessionId, false); }} />}</>}
              {runtimeKind === "provider" && sessionId && <ChatSubagentRail
                subagents={subagentState.subagents}
                open={sessionInspectorOpen && drawerTab === "subagents"}
                onToggle={() => updateSearchParams(next => {
                  if (sessionInspectorOpen && drawerTab === "subagents") next.delete("drawer");
                  else next.set("drawer", "subagents");
                })}
              />}
              {sessionId && <ChatQueuePanel key={sessionId} queue={coreQueue} onRefreshConversation={() => void reloadActiveConversation()} />}
              {showHarnessStatusRail && harnessActivity && <HarnessStatusRail activity={harnessActivity} pendingRequests={pendingHarnessRequests} authoritativeStatus={authoritativeState?.detail} />}
                {assistantDrafts.length > 0 && <section className="chat-context-pack" aria-label="Selected context pack">
                  <header><div><strong>Context pack</strong><small>{assistantDrafts.length} selection{assistantDrafts.length === 1 ? "" : "s"} · {assistantDrafts.reduce((total, item) => total + item.text.length, 0).toLocaleString()} characters</small></div><button className="button quiet" type="button" onClick={clearAssistantDrafts}>Clear all</button></header>
                  <div role="list">{assistantDrafts.map((item, index) => {
                    const expanded = expandedContextIndex === index;
                    return <div className="chat-context-attachment" role="listitem" key={`${item.source.kind}:${item.source.id ?? item.source.label}:${index}`}>
                      <div className="chat-context-attachment-summary"><strong>{item.source.label}</strong><small>{item.source.kind.replaceAll("_", " ")} · {item.text.length.toLocaleString()} characters{item.truncated ? " · truncated" : ""}</small></div>
                      <div className="chat-context-attachment-actions"><button className="icon-button subtle" type="button" aria-label={expanded ? `Collapse ${item.source.label}` : `Expand ${item.source.label}`} aria-expanded={expanded} onClick={() => setExpandedContextIndex(expanded ? undefined : index)}><ChevronDown size={14} /></button><button className="icon-button subtle" type="button" aria-label={`Remove ${item.source.label} from context`} onClick={() => removeAssistantDraft(index)}><X size={14} /></button></div>
                      {expanded && <p>{item.text}</p>}
                    </div>;
                  })}</div>
                  {assistantDraftNotice && <div className="chat-context-pack-notice" role="status"><span>{assistantDraftNotice}</span><button className="icon-button subtle" type="button" aria-label="Dismiss context notice" onClick={clearAssistantDraftNotice}><X size={13} /></button></div>}
                </section>}
                {pendingImages.length > 0 && <div className="chat-image-attachments" role="list" aria-label="Image attachments">{pendingImages.map((image, index) => <div role="listitem" key={`${image.block.artifactId}-${index}`}><img src={image.previewUrl} alt={image.filename} /><button className="icon-button subtle" type="button" aria-label={`Remove ${image.filename}`} onClick={() => removePendingImage(index)}><X size={14} /></button></div>)}</div>}
              </div>
                <label className="sr-only" htmlFor="analyst-message">Message the analyst assistant</label>
                <div className="chat-composer-input" role="combobox" aria-label="Skill suggestions" aria-autocomplete="list" aria-expanded={Boolean(skillToken)} aria-controls={skillToken ? "harness-skill-menu" : undefined} aria-activedescendant={skillToken && matchingHarnessSkills.length ? `harness-skill-option-${skillMenuIndex}` : undefined}>
                  <textarea ref={composerRef} id="analyst-message" data-guide="composer" data-selection-actions-disabled="true" value={draft} disabled={!engagement || !runtimeReady || (loadingHistory && !sessionReadReady)} placeholder={!engagement ? "Create or select a project to chat…" : canSteerCurrentHarness ? "Add guidance while the harness works…" : canStopAndSend ? "Queue a follow-up or send it now…" : queueMode ? "Queue the next message while this response finishes…" : runtimeReady ? "Ask about this project…" : "Add a model or harness in Settings…"} rows={1} onFocus={() => setAssistantSettingsOpen(false)} onPaste={pasteComposerImages} onKeyDown={onComposerKeyDown} onChange={(event) => updateComposerDraft(event.target.value, event.target.selectionStart ?? event.target.value.length)} />
                  {runtimeKind === "harness" && ["grok_acp", "codex_app_server"].includes(selectedHarness?.kind ?? "") && <HarnessCommandHints draft={draft} commands={harnessActivity?.sessionId === harnessSessionId && !harnessActivityError ? harnessActivity.commands : undefined} discoveryPending={selectedHarness?.kind === "grok_acp" && (harnessActivity?.sessionId !== harnessSessionId || !harnessActivity?.commandsDiscovered || Boolean(harnessActivityError))} onSelect={(text) => { updateComposerDraft(text); composerRef.current?.focus(); }} />}
                  {skillToken && <HarnessSkillAutocomplete skills={harnessSkills} token={skillToken} activeIndex={skillMenuIndex} onActiveIndexChange={setSkillMenuIndex} onSelect={selectHarnessSkill} onClose={() => setSkillToken(undefined)} />}
                </div>
                <footer><button ref={assistantSettingsButtonRef} className={`button quiet chat-runtime-summary chat-settings-trigger${runtimeReady ? "" : " needs-attention"}`} type="button" aria-label="Assistant settings" aria-expanded={assistantSettingsOpen} aria-controls="assistant-settings-popover" title={runtimeReady ? `${assistantSource}${runtimeConfiguration ? ` · ${runtimeConfiguration}` : ""}` : "Choose an assistant runtime"} onClick={() => setAssistantSettingsOpen((open) => !open)}><Settings2 size={15} aria-hidden="true" /><span><strong>{assistantSource}</strong><small> · {runtimeConfiguration || "Choose a model"}</small></span></button>{api && runtimeKind === "provider" && <EnvironmentTargetPicker api={api} value={environmentTarget} onChange={setEnvironmentTarget} disabled={composerBusy} />}{sessionId && <button className={`button quiet chat-context-meter status-${activeContextStatus?.status ?? "loading"}`} type="button" aria-label={contextPercent === undefined ? "Open context details" : `Open context details, ${contextPercent} percent of target input used`} title={activeContextStatus?.status === "runtime_managed" ? "Context is managed by the harness runtime" : contextPercent === undefined ? "Read authoritative context status" : `${activeContextStatus?.estimatedInputTokens.toLocaleString()} of ${activeContextStatus?.targetInputTokens.toLocaleString()} target input tokens`} onClick={() => { localStorage.setItem("nebula.session-inspector.open", "true"); setSessionInspectorOpen(true); }}><span aria-hidden="true" style={contextPercent === undefined ? undefined : { "--context-percent": `${contextPercent}%` } as CSSProperties}>{contextPercent === undefined ? <Gauge size={16} aria-hidden="true" /> : contextPercent}</span></button>}<button className="button quiet chat-composer-icon" type="button" aria-label="Results" title="Results" disabled={!sessionId} onClick={() => updateSearchParams(next => {next.set("drawer", "results");})}><Files size={18} aria-hidden="true" /></button><button className={`button quiet chat-composer-icon${publishedUnseen > 0 ? " needs-attention" : ""}`} type="button" aria-label={publishedUnseen > 0 ? `Agent view, ${publishedUnseen} new` : "Agent view"} title="Visuals the assistant published for this conversation" aria-expanded={drawerTab === "visuals" && sessionInspectorOpen} disabled={!sessionId} onClick={() => updateSearchParams(next => {next.set("drawer", drawerTab === "visuals" && sessionInspectorOpen ? "context" : "visuals");})}><Sparkles size={18} aria-hidden="true" />{publishedUnseen > 0 && <span className="chat-composer-badge">{publishedUnseen > 9 ? "9+" : publishedUnseen}</span>}</button><input ref={imageInputRef} className="sr-only" type="file" aria-label="Choose image attachments" accept="image/png,image/jpeg,image/webp" multiple onChange={(event) => void attachImages(event)} />{api && engagement && <ChatAttachments key={engagement.id} api={api} projectId={engagement.id} onAttach={request => requestChatContext(request, view === "browser" ? "browser" : "chat")} onImages={() => imageInputRef.current?.click()} imagesEnabled={imageInputEnabled && !composerBusy} />}{canSteerCurrentHarness && draft.trim() && <button className="button primary square chat-composer-submit" type="submit" disabled={harnessControlBusy} aria-label="Guide current turn" title="Guide the current turn"><Send size={16} /></button>}{canStopAndSend && draft.trim() && <><button className="button quiet square chat-composer-submit" type="button" onClick={() => void submit(undefined, undefined, {})} aria-label="Queue follow-up message" title="Send next after the active response"><ListTodo size={16} /></button><button className="button primary chat-composer-send-now" type="button" aria-label="Stop and send" title="Stop the current turn and send this message next" onClick={() => void stopAndSend()}><Send size={15} /><span className="chat-composer-send-now-label">Stop and send</span></button></>}{(queueMode || canSteerCurrentHarness) && !canStopAndSend && draft.trim() && <button className="button primary square chat-composer-submit" type="button" onClick={() => void submit(undefined, undefined, {})} aria-label="Queue follow-up message" title="Send next after the active response"><ListTodo size={16} /></button>}{(sending || authoritativeProviderBusy) && <button className="button secondary square chat-composer-submit" type="button" aria-label="Stop response" disabled={runtimeKind === "harness" && selectedHarness?.capabilities?.interruption === false} title={runtimeKind === "harness" && selectedHarness?.capabilities?.interruption === false ? "This harness does not advertise turn interruption" : undefined} onClick={() => void stopCurrentResponse()}><Square size={15} /></button>}{sessionId && draft.trim() && !composerBusy && <button type="button" className="button quiet square chat-composer-submit" aria-label="Queue for later" title="Queue for later" disabled={coreQueue.busy} onClick={() => void submit(undefined, undefined, {paused: true})}><ListTodo size={18} aria-hidden="true" /></button>}{!composerBusy && <button className="button primary square chat-composer-submit" type="submit" onPointerDown={(event) => { if (view === "browser") event.preventDefault(); }} disabled={!canSend} aria-label="Send message"><Send size={16} /></button>}</footer>
              </form>
              {showHarnessProgress && visibleHarnessProgress && <div className={`chat-harness-progress phase-${visibleHarnessProgress.phase}`} role="status" aria-live="polite"><span className={`status-dot ${visibleHarnessProgress.phase === "failed" || visibleHarnessProgress.phase === "status_unavailable" ? "unavailable" : "pending"}`} /><div><strong>{harnessPhaseLabel(visibleHarnessProgress.phase)}</strong><small>{visibleHarnessProgress.detail}</small>{visibleHarnessProgress.sessionId && <code title={visibleHarnessProgress.sessionId}>Session {visibleHarnessProgress.sessionId.slice(0, 8)}{visibleHarnessProgress.previousSessionId ? visibleHarnessProgress.phase === "command_runtime_session_created" ? " · current command runtime" : " · independent parallel session" : ""}</code>}</div>{canSteerCurrentHarness && <button className="button quiet harness-steer-button" type="button" disabled={harnessControlBusy} onClick={() => composerRef.current?.focus()}><Plus size={13} aria-hidden="true" /> Add guidance</button>}</div>}
            </div>
  );

  const newChatAction = view === "chat" ? <PageHeaderAction className="button primary compact-new-chat" label="New chat" icon={<Plus size={18} />} disabled={!engagement} title={!engagement ? "Create or select a project before starting chat" : "New chat"} onClick={newConversation} /> : undefined;
  const workbenchToolbar = (
      <Toolbar className={`session-toolbar compact-workbench-toolbar${fullScreen ? "" : " in-shell-header"}`} label="Workbench controls" primaryAction={fullScreen ? newChatAction : undefined}>
        <TabBar className="session-tabs workbench-view-tabs" label="Workbench views" value={view} onChange={setView} items={[
          { id: "chat", label: "Assistant", ariaLabel: "Analyst chat", className: "workbench-primary-tab", icon: <MessageSquare size={18} /> },
          { id: "terminal", label: "Terminal", className: "workbench-primary-tab", icon: <SquareTerminal size={18} /> },
          { id: "code", label: "Code", ariaLabel: "Workspace code editor", className: "workbench-primary-tab", icon: <Braces size={18} /> },
          { id: "browser", label: "Browser", ariaLabel: "Project browser", className: "workbench-primary-tab", icon: <Globe2 size={18} /> },
          { id: "workspace", label: "Files", ariaLabel: "Workspace files", iconOnly: true, className: "workbench-resource-start", icon: <FolderOpen size={18} /> },
          { id: "notes", label: "Notes", ariaLabel: "Project notes", iconOnly: true, icon: <NotebookPen size={18} /> },
          { id: "missions", label: "Missions", ariaLabel: "Autonomous missions", iconOnly: true, icon: <Bot size={18} /> },
          { id: "activity", label: "Activity", ariaLabel: "Activity history", iconOnly: true, icon: <History size={18} /> },
        ] as const} />
        {view !== "chat" && <div className="session-toolbar-actions">
          {view === "missions" && <NewMissionButton className="icon-button subtle toolbar-icon-action" showSetupGuidance={false} />}
          {toolAssistanceAction}
          {focusAction}
        </div>}
      </Toolbar>
  );

  return (
    <div className={`page sessions-page${view === "chat" ? " chat-active" : ""}${screenFitViews.has(view) ? " screen-fit" : ""}${fullScreen ? " full-screen" : ""}`}>
      <PageHeader
        title="Workbench"
        description="Start in Terminal, edit shared code, browse a target, ask the assistant, or open your project files."
        showIntroduction={false}
        actions={fullScreen ? undefined : workbenchToolbar}
        trailingActions={fullScreen ? undefined : newChatAction}
      />
      {fullScreen && workbenchToolbar}

      <div className={`session-layout ${view}${mobileListOpen ? " mobile-list-open" : ""}${view === "chat" && conversationPanelOpen ? " conversation-panel-open" : ""}${view === "chat" && sessionInspectorOpen ? " inspector-open" : ""}`}>
        {compact && view === "chat" && mobileListOpen && <button className="mobile-drawer-scrim" type="button" aria-label="Close conversations" onClick={() => setMobileListOpen(false)} />}
        {view === "chat" && (conversationPanelOpen || mobileListOpen) && <aside className="session-list" id="workbench-conversations" aria-label="Conversations">
          {compact && mobileListOpen && <MobileDrawerProject onNavigate={() => setMobileListOpen(false)} />}
          <header><div><span>Conversations</span><strong>{sessionQuery ? `${visibleSessions.length} of ${sessions.length}` : `${sessions.length} saved`}</strong></div><div className="session-list-header-actions"><details ref={conversationMenuRef} className="conversation-list-menu"><summary className="icon-button subtle" role="button" aria-label="More conversation actions" aria-haspopup="menu" title="More conversation actions"><MoreHorizontal size={17} /></summary><div role="menu"><button className="danger" type="button" role="menuitem" title={sending || pendingResponse ? "Wait for the active response to finish" : "Delete all conversations"} disabled={!sessions.length || Boolean(deletingSessionId) || deletingAllSessions || sending || Boolean(pendingResponse)} onClick={() => { if (conversationMenuRef.current) conversationMenuRef.current.open = false; void deleteAllConversations(); }}>{deletingAllSessions ? <LoaderCircle className="spin" size={14} /> : <Trash2 size={14} />} Delete all conversations</button></div></details><button className="icon-button subtle conversation-pane-close" type="button" aria-label="Hide conversations" title="Hide conversations" aria-expanded="true" onClick={closeConversationPanel}><PanelLeftClose size={16} /></button></div></header>
          <button className={conversationOpen && !sessionId ? "session-new-chat active" : "session-new-chat"} type="button" onClick={newConversation}><Plus size={16} /><span><strong>New chat</strong><small>{runtimeKind === "harness" ? selectedHarness?.name ?? "Choose a harness" : selectedProvider?.name ?? "Choose a provider"}</small></span></button>
          <label className="session-list-search"><Search size={14} aria-hidden="true" /><span className="sr-only">Search conversations</span><input type="search" aria-label="Search conversations" value={sessionQuery} placeholder="Search conversations" onChange={(event) => setSessionQuery(event.target.value)} />{sessionQuery && <button className="icon-button subtle" type="button" aria-label="Clear conversation search" onClick={() => setSessionQuery("")}><X size={13} /></button>}</label>
          <nav>
            {groupedSessions.map(group => <section className="session-list-group" aria-labelledby={`conversation-group-${group.label.replaceAll(" ", "-").toLowerCase()}`} key={group.label}><h3 id={`conversation-group-${group.label.replaceAll(" ", "-").toLowerCase()}`}>{group.label === "Archived" ? <button className="session-list-group-toggle" type="button" aria-expanded={archivedGroupOpen || Boolean(sessionQuery)} onClick={() => setArchivedGroupOpen((current) => !current)}><ChevronDown size={12} aria-hidden="true" className={archivedGroupOpen || sessionQuery ? undefined : "collapsed"} /> Archived <span>{group.sessions.length}</span></button> : group.label}</h3>{(group.label !== "Archived" || archivedGroupOpen || sessionQuery) && group.sessions.map((session) => {
              const actionsOpen = sessionActionsId === session.id;
              const activityState = sessionActivity[session.id] ?? "idle";
              const actionsDisabled = deletingAllSessions || deletingSessionId === session.id || exportingSessionId === session.id || archivingSessionId === session.id || (session.id === sessionId && (sending || Boolean(pendingResponse)));
              const actionsDisabledReason = session.id === sessionId && (sending || pendingResponse) ? "Wait for the active response to finish" : undefined;
              return <div className={`session-list-item${session.id === sessionId ? " active" : ""}${renamingSessionId === session.id ? " renaming" : ""}${actionsOpen ? " actions-open" : ""}`} key={session.id}>{renamingSessionId === session.id ? <form className="session-rename-form" onSubmit={(event) => void renameConversation(event, session)}><label className="sr-only" htmlFor={`conversation-name-${session.id}`}>Conversation name</label><input id={`conversation-name-${session.id}`} aria-label={`Rename conversation ${session.title}`} autoFocus maxLength={300} value={renameDraft} onKeyDown={(event) => { if (event.key === "Escape") cancelRenamingConversation(); }} onChange={(event) => setRenameDraft(event.target.value)} /><button className="icon-button subtle" type="submit" aria-label="Save conversation name" disabled={renamingBusy || !renameDraft.trim()}><Check size={14} /></button><button className="icon-button subtle" type="button" aria-label={`Cancel renaming ${session.title}`} onClick={cancelRenamingConversation}><X size={14} /></button></form> : <><button className="session-select" data-session-id={session.id} type="button" onClick={() => { setSessionActionsId(undefined); void selectSession(session.id); }}><span className={`conversation-activity-marker ${activityState}`} role="img" aria-label={activityState === "working" ? "Working" : activityState === "waiting" ? "Waiting for you" : "Idle"} title={activityState === "working" ? "Working" : activityState === "waiting" ? "Waiting for you" : "Idle"} /><span><strong title={session.title}>{session.title}</strong><small title={session.model || undefined}>{session.model || "Saved conversation"}</small></span></button><div className="session-item-actions"><button
                ref={actionsOpen ? sessionActionsButtonRef : undefined}
                id={`conversation-actions-trigger-${session.id}`}
                className="icon-button subtle session-actions-trigger"
                type="button"
                aria-label={`More actions for ${session.title}`}
                aria-haspopup="menu"
                aria-expanded={actionsOpen}
                aria-controls={actionsOpen ? `conversation-actions-${session.id}` : undefined}
                disabled={actionsDisabled}
                title={actionsDisabledReason ?? `More actions for ${session.title}`}
                onClick={(event) => {
                  sessionActionsButtonRef.current = event.currentTarget;
                  if (sessionActionsId === session.id) {
                    setSessionActionsId(undefined);
                    return;
                  }
                  const bounds = event.currentTarget.getBoundingClientRect();
                  const menuWidth = 196;
                  const menuHeight = 212;
                  const openAbove = window.innerHeight - bounds.bottom < menuHeight + 8 && bounds.top > menuHeight + 8;
                  setSessionActionsPosition({
                    left: Math.max(8, Math.min(bounds.right - menuWidth, window.innerWidth - menuWidth - 8)),
                    openAbove,
                    top: openAbove ? bounds.top - 5 : bounds.bottom + 5,
                  });
                  setSessionActionsId(session.id);
                }}
              >{deletingSessionId === session.id || archivingSessionId === session.id ? <LoaderCircle className="spin" size={15} /> : <MoreHorizontal size={18} />}</button>{actionsOpen && sessionActionsPosition && createPortal(<div
                ref={sessionActionsMenuRef}
                id={`conversation-actions-${session.id}`}
                className="session-actions-menu"
                role="menu"
                aria-label={`Actions for ${session.title}`}
                style={{ left: sessionActionsPosition.left, top: sessionActionsPosition.top, transform: sessionActionsPosition.openAbove ? "translateY(-100%)" : undefined }}
                onKeyDown={(event) => {
                  if (!['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) return;
                  event.preventDefault();
                  const items = [...event.currentTarget.querySelectorAll<HTMLButtonElement>('[role="menuitem"]:not(:disabled)')];
                  if (!items.length) return;
                  const current = items.indexOf(document.activeElement as HTMLButtonElement);
                  const next = event.key === 'Home' ? 0 : event.key === 'End' ? items.length - 1 : event.key === 'ArrowDown' ? (current + 1) % items.length : (current - 1 + items.length) % items.length;
                  items[next]?.focus();
                }}
              ><button type="button" role="menuitem" hidden={session.backend !== "harness"} disabled={sending || session.id !== sessionId} onClick={() => { setSessionActionsId(undefined); void continueAsMission(); }} data-guide="continue-mission"><Bot size={15} /> Continue as mission</button><button type="button" role="menuitem" onClick={() => void copyConversationLink(session)}><Copy size={15} /> Copy link</button><button type="button" role="menuitem" disabled={Boolean(exportingSessionId)} onClick={() => void exportConversation(session)}>{exportingSessionId === session.id ? <LoaderCircle className="spin" size={15} /> : <Download size={15} />} Export transcript</button><button type="button" role="menuitem" onClick={() => startRenamingConversation(session)}><Pencil size={15} /> Rename</button><button type="button" role="menuitem" onClick={() => void setConversationArchived(session, !session.archivedAt)}>{session.archivedAt ? <><ArchiveRestore size={15} /> Unarchive</> : <><Archive size={15} /> Archive</>}</button><button className="danger" type="button" role="menuitem" onClick={() => { setSessionActionsId(undefined); void deleteConversation(session); }}><Trash2 size={15} /> Delete</button></div>, document.body)}</div></>}</div>;
            })}</section>)}
            {sessionQuery && !visibleSessions.length && <div className="empty-state mini"><Search size={18} /><p>No conversations match “{sessionQuery}”.</p></div>}
            {renameError && <DiagnosticErrorNotice error={renameError} fallback="The session could not be renamed." compact />}
          </nav>
          {compact && mobileListOpen && <MobileDrawerFooter onNavigate={() => setMobileListOpen(false)} />}
        </aside>}
        <section className={`session-workspace${chatTerminalVisible ? ` chat-terminal-open${chatTerminalStacked ? " stacked" : ""}` : ""}`}>
          {compact && (view === "activity" || view === "workspace" || view === "notes" || view === "missions") && <header className="mobile-view-header"><h2>{({ activity: "Activity", workspace: "Files", notes: "Notes", missions: "Missions" } as const)[view]}</h2></header>}
          {view === "chat" && compact && <header className="conversation-toolbar mobile-conversation-header">
            <button className="icon-button subtle mobile-drawer-toggle" type="button" aria-label="Open conversations" title="Open conversations" aria-expanded={mobileListOpen} aria-controls="workbench-conversations" onClick={() => { setMobileMoreOpen(false); setMobileListOpen(true); }}><PanelLeft size={20} aria-hidden="true" /></button>
            <button className="mobile-conversation-title" type="button" data-assistant-settings-trigger aria-haspopup="dialog" aria-expanded={assistantSettingsOpen} aria-controls="assistant-settings-popover" onClick={() => setAssistantSettingsOpen((open) => !open)}>
              <strong>{conversationTitle}</strong>
              <small><span>{assistantSource}{runtimeConfiguration ? ` · ${runtimeConfiguration}` : ""}</span><ChevronDown size={13} aria-hidden="true" /></small>
            </button>
            {fullScreen ? focusAction : <div className="mobile-conversation-menu" ref={mobileConversationMenuRef}>
              <button ref={mobileConversationActionsRef} className="icon-button subtle" type="button" aria-label="Conversation actions" title="Conversation actions" aria-haspopup="menu" aria-expanded={mobileConversationMenuOpen} aria-controls={mobileConversationMenuOpen ? "mobile-conversation-actions" : undefined} onClick={() => setMobileConversationMenuOpen((open) => !open)}><MoreHorizontal size={20} aria-hidden="true" /></button>
              {mobileConversationMenuOpen && <div className="mobile-conversation-menu-panel" id="mobile-conversation-actions" role="menu" aria-label="Conversation actions">
                {conversationOpen && <button className="workbench-menu-item" type="button" role="menuitem" onClick={() => { setMobileConversationMenuOpen(false); setTranscriptSearchOpen(true); }}><Search size={17} aria-hidden="true" /><span><strong>Search messages</strong><small>Messages and bookmarks</small></span></button>}
                <button className="workbench-menu-item" type="button" role="menuitem" onClick={() => { setMobileConversationMenuOpen(false); setSessionInspectorOpen(true); }}><PanelRight size={17} aria-hidden="true" /><span><strong>Session details</strong><small>Context and results</small></span></button>
                {api && engagement && <PostToolAssistant api={api} engagementId={engagement.id} providers={providers} harnesses={harnesses} onRun={setRunCandidate} triggerVariant="menu" />}
                <button className="workbench-menu-item" type="button" role="menuitem" onClick={() => { setMobileConversationMenuOpen(false); setFullScreen(true); }}><Maximize2 size={17} aria-hidden="true" /><span><strong>Focus mode</strong><small>Hide navigation</small></span></button>
              </div>}
            </div>}
            <button className="icon-button subtle mobile-new-chat" type="button" aria-label="New chat" title="New chat" disabled={!engagement} onClick={newConversation}><SquarePen size={20} aria-hidden="true" /></button>
          </header>}
          {view === "chat" && !compact && <header className="conversation-toolbar">
          {!conversationPanelOpen && !mobileListOpen && <button
            className="icon-button subtle session-conversations-toggle"
            type="button"
            aria-label="Show conversations"
            title="Show conversations"
            aria-expanded="false"
            aria-controls="workbench-conversations"
            onClick={toggleConversationPanel}
          >
            <PanelLeft size={18} aria-hidden="true" />
          </button>}
            <strong className="conversation-toolbar-title" title={conversationTitle}>{conversationTitle}</strong>
            {conversationActions}
          </header>}

          {api && engagement && <div ref={(element) => { chatTerminalSize.panelRef.current = element; }} id="chat-side-terminal" aria-label={chatTerminalVisible ? "Terminal beside chat" : undefined} role={chatTerminalVisible ? "region" : undefined} style={chatTerminalVisible ? chatTerminalSize.panelStyle : undefined} className={`persistent-terminal integrated-browser-layout${terminalAssistantOpen && view === "terminal" ? " assistant-open" : ""}${chatTerminalVisible ? " chat-side-terminal" : ""}`} hidden={view !== "terminal" && !chatTerminalVisible}>
            {chatTerminalVisible && chatTerminalSize.resizeHandle}
            <div className="integrated-browser-page terminal-companion-page"><header className="browser-workspace-toolbar terminal-companion-toolbar"><div ref={setTerminalToolbarHost} className="terminal-toolbar-host" />{chatTerminalVisible ? <><button className="button quiet managed-browser-icon" type="button" aria-label="Open Terminal tab" title="Open the full Terminal tab" onClick={() => setView("terminal")}><Maximize2 size={16} aria-hidden="true" /></button><button className="button quiet managed-browser-icon" type="button" aria-label="Hide terminal" title="Hide terminal" onClick={() => setChatTerminalOpen(false)}><X size={16} aria-hidden="true" /></button></> : <button className={`button quiet managed-browser-icon${compact ? " mobile-ask-button" : ""}`} type="button" aria-label={compact ? "Ask Assistant" : "Assistant"} title="Toggle Assistant" aria-expanded={terminalAssistantOpen} aria-controls="terminal-assistant-panel" onClick={() => setTerminalAssistantOpen(open => !open)}>{compact ? <><Sparkles size={15} aria-hidden="true" /><span>Ask</span></> : <PanelRight size={18} aria-hidden="true" />}</button>}</header>
            <Suspense fallback={<div className="empty-state compact"><LoaderCircle className="spin" size={20} /><strong>Loading Terminal…</strong></div>}><ContainerTerminalPanel toolbarHost={terminalToolbarHost} active={view === "terminal" || chatTerminalVisible} api={api} capturedBy={activeOperator?.id} engagementId={engagement.id} engagementName={engagement.name} onUploadEvidence={uploadEvidence} setupTerminalStatus={setupStatus?.terminal.status} setupTerminalDetail={setupStatus?.terminal.detail} commandRequest={terminalCommandRequest} onCommandAccepted={(id) => setTerminalCommandRequest(current => current?.id === id ? undefined : current)} /></Suspense></div>
            {view === "terminal" && terminalAssistantOpen && <BrowserAssistantPanel panelId="terminal-assistant-panel" label="Terminal Assistant" onActionContainer={() => undefined} header={<><strong>Assistant</strong>{transcriptSearchAction}<button className="button quiet managed-browser-icon" type="button" aria-label="New conversation" title="New conversation" disabled={sending || Boolean(pendingResponse)} onClick={newConversation}><Plus size={18} aria-hidden="true" /></button><button className="button quiet" type="button" aria-label="Collapse terminal Assistant" title="Collapse Assistant" onClick={() => setTerminalAssistantOpen(false)}><X size={16} /></button></>}>
              {assistantPanel}
            </BrowserAssistantPanel>}
          </div>}
          {api && engagement && <div className="persistent-code-editor" hidden={view !== "code"}>
            <Suspense fallback={<div className="empty-state compact"><LoaderCircle className="spin" size={20} /><strong>Loading Code editor…</strong></div>}><CodeEditorPanel active={view === "code"} api={api} engagementId={engagement.id} workspacePath={engagement.workspacePath} providers={providers} harnesses={harnesses} initialWorkspaceSearch={searchParams.get("workspaceSearch") ?? undefined} initialOpenPath={searchParams.get("openFile") ?? undefined} onRun={setRunCandidate} onOpenTerminal={() => setView("terminal")} onCreateFindingDraft={requestFindingDraft} onUseWithAssistant={requestNebulaDraft} /></Suspense>
          </div>}
          {api && engagement && <div className={`persistent-browser integrated-browser-layout${browserAssistantOpen ? " assistant-open" : ""}`} hidden={view !== "browser"}>
            <div className="integrated-browser-page">
            <header className="browser-workspace-toolbar">{browserEngine === "managed" && <button className="button quiet managed-browser-icon" type="button" aria-label={browserControlsOpen ? "Hide browser controls" : "Show browser controls"} title={browserControlsOpen ? "Hide browser controls" : "Show browser controls"} aria-expanded={browserControlsOpen} aria-controls="managed-browser-controls" onClick={() => setBrowserControlsOpen(open => !open)}><Settings2 size={18} aria-hidden="true" /></button>}<button className="button quiet managed-browser-icon" type="button" aria-label="Assistant" title="Toggle Assistant" aria-expanded={browserAssistantOpen} aria-controls="browser-assistant-panel" onClick={() => setBrowserAssistantOpen((open) => !open)}><PanelRight size={18} aria-hidden="true" /></button>
            <label><Globe2 size={16} aria-hidden="true" /><select aria-label="Browser engine" value={browserEngine} onChange={event => setBrowserEngine(event.target.value as "managed" | "native")}><option value="managed">Assistant browser</option><option value="native">Native browser</option></select></label></header>
            {browserEngine === "managed" ? <ManagedAssistantBrowser key={engagement.id} api={api} projectId={engagement.id} active={view === "browser"} controlsOpen={browserControlsOpen}
              conversationId={sessionId || undefined} onConversation={(id) => void openAttachedChat(id)}
              onContext={(request) => { setBrowserAssistantOpen(true); requestChatContext(request, "browser"); }}
              actionContainer={browserActionContainer} onControlChange={setBrowserControlEnabled} imageSupported={imageInputEnabled} onImage={(file) => { setBrowserAssistantOpen(true); void attachImageFiles([file]); }} /> : <WorkbenchBrowser
              active={view === "browser"}
              api={api}
              operatorId={activeOperator?.id}
              projectId={engagement.id}
              scope={browserScope}
              scopeLoading={browserScopeLoading}
              onAddKnowledgeUrl={(url) => ingestKnowledgeUrlSource({ engagementId: engagement.id, url })}
              onAskNebula={requestNebulaDraft}
              onAttachContext={attachBrowserContext}
              assistantRuntimeLabel={runtimeReady && model.trim() ? `${assistantSource} · ${runtimeConfiguration}` : undefined}
              onContinueConversation={(id) => void openAttachedChat(id)}
              onOpenFiles={() => setView("workspace")}
              onScopeUpdated={setBrowserScope}
              onUploadEvidence={uploadEvidence}
            />}
            </div>
            {view === "browser" && browserAssistantOpen && <BrowserAssistantPanel onActionContainer={setBrowserActionContainer} header={<><strong>Assistant</strong>{transcriptSearchAction}<button className="button quiet managed-browser-icon" type="button" aria-label="New conversation" title="New conversation" disabled={sending || Boolean(pendingResponse)} onClick={newConversation}><Plus size={18} aria-hidden="true" /></button><button className="button quiet" type="button" aria-label="Collapse browser Assistant" title="Collapse Assistant" onClick={collapseBrowserAssistant}><X size={16} /></button></>}>
              {assistantPanel}
            </BrowserAssistantPanel>}
          </div>}
          {(view === "terminal" || view === "code") && (!api || !engagement) ? (
            <div className="empty-state"><FolderOpen size={24} /><strong>Preparing your project</strong><p>Terminal and Code become available as soon as Nebula finishes creating or loading a project.</p></div>
          ) : view === "terminal" || view === "code" || (view === "browser" && engagement) ? null : view === "missions" && api && engagement ? (
            <AgentsPage embedded />
          ) : view === "activity" && api && engagement ? (
            <div className="workbench-activity-stack">
              {compact && <MobileApprovals />}
              <ExecutionHistory api={api} engagementId={engagement.id} refreshKey={executionRefresh} onRerun={setRunCandidate} providers={providers} harnesses={harnesses} onChatAttached={openAttachedChat} />
              <TerminalCommandHistoryPanel api={api} engagementId={engagement.id} />
            </div>
          ) : view === "workspace" && api && engagement ? (
            <WorkspacePanel api={api} engagementId={engagement.id} engagementName={engagement.name} onUseWithAssistant={requestNebulaDraft} onOpenTerminal={() => setView("terminal")} onOpenActivity={() => setView("activity")} />
          ) : view === "notes" && api && engagement ? (
            <NotesPanel
              api={api}
              engagementId={engagement.id}
              evidenceOptions={evidence.map((item) => ({ id: item.id, label: item.title }))}
              assetOptions={assets.map((item) => ({ id: item.id, label: item.displayName }))}
              providers={providers}
              harnesses={harnesses}
              initialDraft={noteDraft}
              onInitialDraftConsumed={clearNoteDraft}
              createObservation={createObservation}
              updateObservation={updateObservation}
              deleteObservation={deleteObservation}
              onAskNebula={requestNebulaDraft}
            />
          ) : view !== "chat" ? (
            <div className="empty-state"><FolderOpen size={24} /><strong>Select a project</strong><p>Terminal, execution history, and workspace files are project-scoped.</p></div>
          ) : !conversationOpen ? (
            <div className="empty-state chat-empty-state"><MessageSquare size={24} /><strong>No conversation open</strong><p>Select a saved conversation or start a new chat when you are ready.</p><button className="button primary" type="button" disabled={!engagement} onClick={newConversation}><Plus size={15} /> Start new chat</button></div>
          ) : (
            assistantPanel
          )}
        </section>

        {(view === "chat" || view === "browser") && sessionInspectorOpen && <ChatWorkspaceDrawer overlay={view === "browser"} tab={drawerTab} onTab={tab => updateSearchParams(next => {next.set("drawer", tab);})} onClose={() => setSessionInspectorOpen(false)}>
          {drawerTab === "subagents" ? api && sessionId ? <ChatSubagentPane
            key={`subagents:${sessionId}`}
            api={api}
            sessionId={sessionId}
            subagents={subagentState.subagents}
            error={subagentState.error}
            compact={matchMedia("(max-width: 1100px)").matches}
            onClose={() => setSessionInspectorOpen(false)}
            onChanged={subagentState.refresh}
            onOpenConversation={id => void selectSession(id)}
          /> : <p>Subagents appear after the first saved turn.</p>
          : drawerTab === "visuals" ? api && engagement && sessionId ? <ChatResultStream key={`visuals:${sessionId}`} api={api} projectId={engagement.id} sessionId={sessionId} /> : <p>Published snapshots appear after the first saved turn.</p>
          : drawerTab === "results" ? api && sessionId ? <ChatResults key={sessionId} api={api} sessionId={sessionId} onMessage={openDrawerMessage} onAttach={request => requestChatContext(request, view === "browser" ? "browser" : "chat")} /> : <p>Results appear after the first saved turn.</p> : <>
          {api && sessionId && searchParams.get("turn") && <ChatTurnDetails api={api} sessionId={sessionId} turnId={searchParams.get("turn")!} onMessage={openDrawerMessage} />}
          <section className="session-context-health"><h3>Working context</h3>{!sessionId ? <p>Context becomes durable after the first saved turn.</p> : contextStatusLoading && !activeContextStatus ? <div className="chat-thinking"><LoaderCircle className="spin" size={14} /> Reading Core context…</div> : contextStatusError ? <div className="session-context-error"><p>{contextStatusError}</p><button className="button quiet" type="button" onClick={() => setContextRefreshKey((value) => value + 1)}>Retry</button></div> : activeContextStatus ? <><div className="session-context-summary"><span className={`status-dot ${activeContextStatus.status === "failed" ? "unavailable" : activeContextStatus.status === "stale" ? "pending" : "healthy"}`} /><div><strong>{activeContextStatus.status === "runtime_managed" ? "Harness managed" : activeContextStatus.status.replaceAll("_", " ")}</strong><small>{activeContextStatus.status === "runtime_managed" ? "The selected harness owns compaction and reports its usage through activity." : `${activeContextStatus.estimatedInputTokens.toLocaleString()} estimated · ${activeContextStatus.targetInputTokens.toLocaleString()} target input tokens · ${contextCapacityLabel}`}</small></div></div>{contextPercent !== undefined && <div className="session-context-progress" aria-label={`${contextPercent} percent of target input used`}><span style={{ width: `${contextPercent}%` }} /></div>}{activeContextStatus.compactedThrough > 0 && <p>Core compacted through message {activeContextStatus.compactedThrough}; the source transcript remains unchanged.</p>}{activeContextStatus.snapshot?.memory && <details className="session-memory"><summary>Inspect saved memory</summary><div>{activeContextStatus.snapshot.memory.objective && <section><strong>Objective</strong><p>{activeContextStatus.snapshot.memory.objective}</p></section>}<section><strong>Summary</strong><p>{activeContextStatus.snapshot.memory.summary}</p></section>{([
              ["Confirmed facts", activeContextStatus.snapshot.memory.confirmedFacts],
              ["Decisions", activeContextStatus.snapshot.memory.decisions],
              ["Constraints", activeContextStatus.snapshot.memory.constraints],
              ["Corrections", activeContextStatus.snapshot.memory.corrections],
              ["Open questions", activeContextStatus.snapshot.memory.openQuestions],
            ] as const).map(([label, items]) => items.length ? <section key={label}><strong>{label}</strong><ul>{items.map((item, index) => <li key={`${label}-${index}`}>{item.text}</li>)}</ul></section> : null)}<small>{activeContextStatus.snapshot.sourceReferences.length} source reference{activeContextStatus.snapshot.sourceReferences.length === 1 ? "" : "s"} · private reasoning is not stored</small></div></details>}</> : <p>Context status has not been recorded yet.</p>}</section>
          {runtimeKind === "provider" && api && sessionId && <ProviderSessionAdvanced api={api} sessionId={sessionId} goal={providerGoal} onOpenChild={id => void selectSession(id)} />}
          {api && sessionId && <ChatDecisions key={`decisions:${sessionId}`} api={api} sessionId={sessionId} seed={decisionSeed} onSeedConsumed={() => setDecisionSeed(undefined)} onMessage={(id, sourceSession) => {if (sourceSession && sourceSession !== sessionId) {updateSearchParams(next => {next.set("session", sourceSession); next.set("message", id); next.delete("drawer");});} else openDrawerMessage(id);}} />}
          {api && sessionId && <ChatRecordedContext key={`recorded-context:${sessionId}`} api={api} sessionId={sessionId} onMessage={openDrawerMessage} />}



        <details><summary>Technical session details</summary>          <dl><div><dt>Active operator</dt><dd>{activeOperator?.displayName ?? "No active operator"}</dd></div><div><dt>Conversation</dt><dd>{conversationOpen ? sessionId ? sessions.find((session) => session.id === sessionId)?.title ?? "Saved chat" : "Unsaved chat" : "None selected"}</dd></div><div><dt>Runtime</dt><dd>{runtimeKind === "harness" ? selectedHarness?.name ?? "Harness" : selectedProvider?.name ?? "Not selected"}</dd></div>{runtimeKind === "harness" && <div><dt>Model configuration</dt><dd>{model || "Not selected"}{harnessReasoningEffort ? ` · ${harnessReasoningEffort} effort` : ""}{harnessServiceTier ? ` · ${harnessServiceTier} speed` : ""}</dd></div>}{runtimeKind === "harness" && harnessSessionId && <div><dt>Harness session</dt><dd><code title={harnessSessionId}>{harnessSessionId}</code></dd></div>}<div><dt>Code Run</dt><dd><span className={`status-dot ${executionCapabilities?.ready ? "healthy" : "unavailable"}`} /> {executionCapabilities?.ready ? "Review available" : "Unavailable"}</dd></div></dl></details></>}
        </ChatWorkspaceDrawer>}
      </div>
      <nav className="mobile-companion-nav" aria-label="Mobile operator navigation">
        <button type="button" aria-label="Chat" aria-current={!mobileMoreOpen && view === "chat" ? "page" : undefined} onClick={() => { setMobileMoreOpen(false); setMobileListOpen(false); setView("chat"); }}><MessageSquare size={21} aria-hidden="true" /><span>Chat</span></button>
        <button type="button" aria-label="Terminal" aria-current={!mobileMoreOpen && view === "terminal" ? "page" : undefined} onClick={() => { setMobileMoreOpen(false); setMobileListOpen(false); setView("terminal"); }}><SquareTerminal size={21} aria-hidden="true" /><span>Terminal</span></button>
        <button type="button" aria-label="Activity" aria-describedby={approvals.length ? "mobile-activity-waiting" : undefined} aria-current={!mobileMoreOpen && view === "activity" ? "page" : undefined} onClick={() => { setMobileMoreOpen(false); setMobileListOpen(false); setView("activity"); }}><Activity size={21} aria-hidden="true" /><span>Activity</span>{approvals.length > 0 && <i className="mobile-nav-badge" id="mobile-activity-waiting"><span className="sr-only">{approvals.length} waiting for you</span></i>}</button>
        <button type="button" aria-label="More workbench views" aria-expanded={mobileMoreOpen} aria-controls="mobile-workbench-more" aria-current={mobileMoreOpen || (["workspace", "notes", "missions", "code", "browser"] as SessionView[]).includes(view) ? "page" : undefined} onClick={() => { setMobileListOpen(false); setMobileMoreOpen((value) => !value); }}><LayoutGrid size={21} aria-hidden="true" /><span>More</span></button>
      </nav>
      {mobileMoreOpen && <MobileMorePanel view={view} onSelectView={setView} onFocusMode={() => setFullScreen(true)} onClose={() => setMobileMoreOpen(false)} />}
      {artifactInspector && <ModalSurface as="section" className="provider-dialog resource-dialog" labelledBy="artifact-inspector-title" onClose={() => setArtifactInspector(undefined)}><header><div><small>Untrusted tool data · bounded retrieval</small><h2 id="artifact-inspector-title">{artifactInspector.capability} artifacts</h2></div><button className="icon-button subtle" type="button" aria-label="Close artifact inspector" onClick={() => setArtifactInspector(undefined)}><X size={17} /></button></header>{artifactInspector.receipt && <div className="knowledge-status" role="status"><ShieldCheck size={15} /><span>Receipt {String(artifactInspector.receipt.status ?? artifactInspector.status)} · parser {String((artifactInspector.receipt.parser as Record<string, unknown> | undefined)?.state ?? "not configured")}{Array.isArray(artifactInspector.receipt.warnings) && artifactInspector.receipt.warnings.length ? ` · ${artifactInspector.receipt.warnings.join(" · ")}` : ""}</span></div>}<div className="runtime-resource-list">{artifactInspector.artifacts.length ? artifactInspector.artifacts.map((artifact) => <article className="runtime-resource-card" key={artifact.artifactId}><header><div><strong>{artifact.filename ?? artifact.kind}</strong><code title={artifact.sha256}>{artifact.sha256.slice(0, 16)}…</code></div><span>{artifact.truncated ? "truncated" : artifact.searchable ? "searchable" : "binary"}</span></header><small>{artifact.byteCount.toLocaleString()} retained byte{artifact.byteCount === 1 ? "" : "s"}{artifact.observedByteCount !== artifact.byteCount ? ` · ${artifact.observedByteCount.toLocaleString()} observed` : ""} · {artifact.mediaType}</small><footer>{artifact.searchable && <button className="button quiet" type="button" onClick={() => void readArtifact(artifact.artifactId)} disabled={artifactBusy}>Read excerpt</button>}<button className="button quiet" type="button" onClick={() => void saveRawArtifact(artifact)}>Save acknowledged raw</button></footer></article>) : <p>Artifact references are available through search for this historical or gateway result.</p>}</div><form className="chat-composer" onSubmit={(event) => void searchArtifacts(event)}><label>Search all searchable artifacts<input value={artifactQuery} maxLength={512} placeholder="open 443/tcp" onChange={(event) => setArtifactQuery(event.target.value)} /></label><button className="button primary" type="submit" disabled={artifactBusy || !artifactQuery.trim()}><Search size={14} /> {artifactBusy ? "Searching…" : "Search"}</button></form>{artifactError && <DiagnosticErrorNotice error={artifactError} fallback="Artifact retrieval failed." compact />}{artifactSearch && <section><h3>Search matches</h3>{artifactSearch.matches.length ? artifactSearch.matches.map((match, index) => <article className="panel" key={`${match.artifactId}-${match.line}-${index}`}><header><strong>{match.filename ?? match.artifactId}</strong><button className="button quiet" type="button" onClick={() => void readArtifact(match.artifactId, Math.max(1, match.line - 10))}>Read around line {match.line}</button></header><pre>{match.context.map((line) => `${line.line}: ${line.text}${line.lineTruncated ? "…" : ""}`).join("\n")}</pre></article>) : <p>No matching lines.</p>}{artifactSearch.truncated && <small>More matches are available with the continuation cursor.</small>}</section>}{artifactRead && <section><h3>{artifactRead.filename ?? artifactRead.artifactId}</h3>{artifactRead.searchable ? <pre>{artifactRead.lines.map((line) => `${line.line}: ${line.text}${line.lineTruncated ? "…" : ""}`).join("\n")}</pre> : <p>This binary artifact is retained but not searchable.</p>}{artifactRead.continuationStartingLine && <button className="button quiet" type="button" onClick={() => void readArtifact(artifactRead.artifactId, artifactRead.continuationStartingLine)}>Read next lines</button>}</section>}<footer><span>Excerpts are redacted, line-numbered, and capped at 8 KiB.</span><button className="button secondary" type="button" onClick={() => setArtifactInspector(undefined)}>Close</button></footer></ModalSurface>}
      {runCandidate && api && engagement && <ExecutionReviewDialog api={api} engagementId={engagement.id} candidate={runCandidate} capabilities={executionCapabilities} onClose={() => setRunCandidate(undefined)} onStarted={() => { setExecutionRefresh((value) => value + 1); setView("activity"); }} />}
    </div>
  );
}
