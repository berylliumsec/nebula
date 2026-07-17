import { lazy, Suspense, useEffect, useMemo, useRef, useState, type FormEvent, type KeyboardEvent } from "react";
import {
  Bot,
  Braces,
  Check,
  FileClock,
  FolderOpen,
  Globe2,
  LoaderCircle,
  Maximize2,
  MessageSquare,
  Minimize2,
  NotebookPen,
  PanelLeftClose,
  PanelLeftOpen,
  Pencil,
  Plus,
  Search,
  Send,
  ShieldCheck,
  Square,
  SquareTerminal,
  Trash2,
  X,
} from "lucide-react";
import { Link, useSearchParams } from "react-router-dom";
import { providerModelVerification } from "../api/providerCapabilities";
import type {
  ChatCompletionRequest,
  ChatSessionSummary,
  ChatStreamEvent,
  ExecutionCapabilities,
  ExecutionLanguage,
  HarnessProfile,
  HarnessActivityEvent,
  HarnessInteraction,
  HarnessSessionActivity,
  HarnessSessionSummary,
  McpServerProfile,
  PersistedChatMessage,
  ToolArtifactReference,
  ToolOutputReadResult,
  ToolOutputSearchResult,
} from "../api/types";
import { AssistantMarkdown, type FencedRunCandidate } from "../components/AssistantMarkdown";
import { sha256 } from "../components/assistantCode";
import { ExecutionHistory } from "../components/ExecutionHistory";
import { ExecutionReviewDialog } from "../components/ExecutionReviewDialog";
import { NewMissionButton } from "../components/MissionControls";
import { NotesPanel } from "../components/NotesPanel";
import { PageHeader } from "../components/PageHeader";
import { TerminalCommandHistoryPanel } from "../components/TerminalCommandHistoryPanel";
import { useConfirmation } from "../components/DialogSystem";
import { createHashedSelectionAttachment } from "../components/selection";
import { WorkspacePanel } from "../components/WorkspacePanel";
import { WorkbenchBrowser } from "../components/WorkbenchBrowser";
import { useWorkbenchDrafts } from "../state/WorkbenchDraftContext";
import { useWorkspace } from "../state/WorkspaceContext";
import { AgentsPage } from "./AgentsPage";
import {
  isTimelineActivity,
  reasoningSummaryState,
  reasoningSummaryText,
  reduceHarnessActivity,
  shouldShowActivityItem,
  shouldShowActivityKind,
  type HarnessActivityItem,
} from "./harnessActivity";
import { detachHarnessStream } from "./chatStreamLifecycle";
import {
  reconcileCompletedAssistantMessage,
  type ReconciledConversationMessage,
} from "./chatMessageReconciliation";
import { DiagnosticErrorNotice, logCaughtDiagnostic } from "../diagnostics";

type SessionView = "chat" | "code" | "terminal" | "browser" | "missions" | "activity" | "workspace" | "notes";
interface ToolLifecycleCard {
  assistantId: string;
  toolCallId: string;
  capability: string;
  status: string;
  summary?: string;
  evidenceIds: string[];
  resultArtifactId?: string;
  artifacts: ToolArtifactReference[];
  receipt?: Record<string, unknown>;
}

interface PendingChatResponse {
  turnId: string;
  assistantId: string;
  userId: string;
  request: ChatCompletionRequest;
  approval: Record<string, unknown>;
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

interface ConversationMessage extends ReconciledConversationMessage {}

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
    case "running": return "Harness is working";
    case "tool": return "Harness is using a tool";
    case "waiting_approval": return "Harness needs approval";
    case "finalizing": return "Saving harness response";
    case "complete": return "Harness turn complete";
    case "interrupted": return "Harness turn interrupted";
    case "failed": return "Harness turn failed";
    default: return "Harness status";
  }
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
    harnessTurnId: message.harnessTurnId,
  };
}

export function SessionsPage() {
  const confirm = useConfirmation();
  const {
    assistantDraft,
    clearAssistantDraft,
    clearExecutionDraft,
    clearNoteDraft,
    executionDraft,
    noteDraft,
    requestNebulaDraft,
  } = useWorkbenchDrafts();
  const [searchParams, setSearchParams] = useSearchParams();
  const requestedView = searchParams.get("view");
  const requestedSessionId = searchParams.get("session") ?? "";
  const initialView = requestedView === "chat" || requestedView === "code" || requestedView === "terminal" || requestedView === "browser" || requestedView === "missions" || requestedView === "activity" || requestedView === "workspace" || requestedView === "notes"
    ? requestedView
    : requestedView === "executions" ? "activity"
      : requestedView === "files" ? "workspace"
        : localStorage.getItem("nebula.workbench.view") as SessionView | null;
  const [view, setViewState] = useState<SessionView>(initialView === "chat" || initialView === "code" || initialView === "browser" || initialView === "missions" || initialView === "activity" || initialView === "workspace" || initialView === "notes" ? initialView : "terminal");
  const setView = (next: SessionView) => {
    setViewState(next);
    localStorage.setItem("nebula.workbench.view", next);
    const params = new URLSearchParams(searchParams);
    params.set("view", next);
    setSearchParams(params, { replace: true });
  };
  const openUnattachedChatView = () => {
    setViewState("chat");
    localStorage.setItem("nebula.workbench.view", "chat");
    const params = new URLSearchParams(searchParams);
    params.set("view", "chat");
    params.delete("session");
    setSearchParams(params, { replace: true });
  };
  const [mobileListOpen, setMobileListOpen] = useState(false);
  const [fullScreen, setFullScreen] = useState(false);
  const [conversationPanelExpanded, setConversationPanelExpanded] = useState(
    () => localStorage.getItem("nebula.conversations.expanded") === "true",
  );
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
    knowledgeSources,
    providers,
    refreshProvider,
    reverifyProvider,
    resolveApproval,
    setupStatus,
    startMission,
    uploadEvidence,
    updateObservation,
  } = useWorkspace();
  const [executionCapabilities, setExecutionCapabilities] = useState<ExecutionCapabilities>();
  const [runCandidate, setRunCandidate] = useState<FencedRunCandidate>();
  const [executionRefresh, setExecutionRefresh] = useState(0);
  const [sessions, setSessions] = useState<ChatSessionSummary[]>([]);
  const [deletingSessionId, setDeletingSessionId] = useState<string>();
  const [deletingAllSessions, setDeletingAllSessions] = useState(false);
  const [renamingSessionId, setRenamingSessionId] = useState<string>();
  const [renameDraft, setRenameDraft] = useState("");
  const [renameError, setRenameError] = useState<string>();
  const [sessionId, setSessionId] = useState("");
  const [conversationOpen, setConversationOpen] = useState(Boolean(requestedSessionId));
  const [providerId, setProviderId] = useState("");
  const [runtimeKind, setRuntimeKind] = useState<"provider" | "harness">("provider");
  const [harnesses, setHarnesses] = useState<HarnessProfile[]>([]);
  const [harnessSessions, setHarnessSessions] = useState<HarnessSessionSummary[]>([]);
  const [harnessActivity, setHarnessActivity] = useState<HarnessSessionActivity>();
  const [harnessActivityError, setHarnessActivityError] = useState<string>();
  const [harnessProgress, setHarnessProgress] = useState<HarnessProgress>();
  const [mcpServers, setMcpServers] = useState<McpServerProfile[]>([]);
  const [harnessId, setHarnessId] = useState("");
  const [harnessSessionId, setHarnessSessionId] = useState("");
  const [selectedMcpIds, setSelectedMcpIds] = useState<string[]>([]);
  const [model, setModel] = useState("");
  const [includeKnowledge, setIncludeKnowledge] = useState(true);
  const [commandRuntimeReady, setCommandRuntimeReady] = useState(false);
  const [toolRuntimeReason, setToolRuntimeReason] = useState<string>();
  const [toolCards, setToolCards] = useState<ToolLifecycleCard[]>([]);
  const [activityItems, setActivityItems] = useState<HarnessActivityItem[]>([]);
  const [harnessInteractions, setHarnessInteractions] = useState<HarnessInteraction[]>([]);
  const [interactionAnswers, setInteractionAnswers] = useState<Record<string, string>>({});
  const [harnessControlBusy, setHarnessControlBusy] = useState(false);
  const [artifactInspector, setArtifactInspector] = useState<ToolLifecycleCard>();
  const [artifactQuery, setArtifactQuery] = useState("");
  const [artifactSearch, setArtifactSearch] = useState<ToolOutputSearchResult>();
  const [artifactRead, setArtifactRead] = useState<ToolOutputReadResult>();
  const [artifactBusy, setArtifactBusy] = useState(false);
  const [artifactError, setArtifactError] = useState<string>();
  const [pendingResponse, setPendingResponse] = useState<PendingChatResponse>();
  const [messages, setMessages] = useState<ConversationMessage[]>([]);
  const [draft, setDraft] = useState("");
  const [sending, setSending] = useState(false);
  const [loadingHistory, setLoadingHistory] = useState(false);
  const [chatError, setChatError] = useState<string>();
  const [discoveringProviderId, setDiscoveringProviderId] = useState<string>();
  const abortRef = useRef<AbortController | undefined>(undefined);
  const streamBackendRef = useRef<ChatCompletionRequest["backend"] | undefined>(undefined);
  const detachedHarnessStreamsRef = useRef(new WeakSet<AbortController>());
  const harnessFollowDetachRef = useRef<(() => void) | undefined>(undefined);
  const composerRef = useRef<HTMLTextAreaElement>(null);
  const lastModelDiscoveryProviderIdRef = useRef<string | undefined>(undefined);
  const attemptedToolVerificationRef = useRef(new Set<string>());
  const scrollRef = useRef<HTMLDivElement>(null);
  const enabledProviders = useMemo(() => providers.filter((provider) => provider.enabled), [providers]);
  const selectedProvider = enabledProviders.find((provider) => provider.id === providerId);
  const selectedHarness = harnesses.find((harness) => harness.id === harnessId);
  const selectedHarnessSession = harnessSessions.find((item) => item.id === harnessSessionId);
  const harnessModelOptions = [...new Set([
    ...(selectedHarness?.models ?? []),
    ...(selectedHarnessSession ? [selectedHarnessSession.model] : []),
    ...(runtimeKind === "harness" && model ? [model] : []),
  ])];
  const providerIsLocal = selectedProvider?.kind === "local" || selectedProvider?.privacy === "local_only";
  const harnessIsLocal = selectedHarness?.localOnly === true;
  const runtimePermitsKnowledge = runtimeKind === "harness"
    ? harnessIsLocal || selectedHarness?.permitsSensitiveData === true
    : providerIsLocal || selectedProvider?.permitsSensitiveData === true;
  const canUseKnowledge = knowledgeSources.length > 0 && runtimePermitsKnowledge;
  const modelVerification = providerModelVerification(selectedProvider, model);
  const modelVerified = modelVerification?.status === "verified";
  const commandRuntimeAvailable = Boolean(modelVerified && commandRuntimeReady && !toolRuntimeReason);

  const detachActiveHarnessStream = () => {
    const controller = abortRef.current;
    if (!detachHarnessStream(
      controller,
      streamBackendRef.current,
      detachedHarnessStreamsRef.current,
    )) return false;
    abortRef.current = undefined;
    streamBackendRef.current = undefined;
    return true;
  };
  const commandRuntimeReason = !modelVerified
    ? model ? `Tool calling is unverified for ${model}.` : "Select a model to verify tool calling."
    : toolRuntimeReason ?? (!commandRuntimeReady ? "The command runtime is not ready." : undefined);
  const canUseTools = commandRuntimeAvailable;
  const commandRuntimeUnavailableReason = commandRuntimeReason;

  useEffect(() => {
    if (!fullScreen) return;
    const exitFullScreen = (event: globalThis.KeyboardEvent) => {
      if (event.key !== "Escape") return;
      event.preventDefault();
      setFullScreen(false);
    };
    window.addEventListener("keydown", exitFullScreen);
    return () => window.removeEventListener("keydown", exitFullScreen);
  }, [fullScreen]);

  useEffect(() => {
    if (runtimeKind !== "provider" || coreState !== "online" || view !== "chat" || !selectedProvider || !model.trim() || modelVerification) return;
    const key = `${selectedProvider.id}:${model.trim()}`;
    if (attemptedToolVerificationRef.current.has(key)) return;
    attemptedToolVerificationRef.current.add(key);
    void reverifyProvider(selectedProvider.id, model).catch((caughtError) => { void logCaughtDiagnostic("interface.sessions_page.caught_failure_01", "A handled interface operation failed.", caughtError, "sessions_page"); return undefined; });
  }, [coreState, model, modelVerification, reverifyProvider, runtimeKind, selectedProvider, view]);

  useEffect(() => {
    const next = requestedView === "executions" ? "activity" : requestedView === "files" ? "workspace" : requestedView;
    if (next === "chat" || next === "code" || next === "terminal" || next === "browser" || next === "missions" || next === "activity" || next === "workspace" || next === "notes") {
      setViewState(next);
      localStorage.setItem("nebula.workbench.view", next);
    }
  }, [requestedView]);

  useEffect(() => {
    if (!assistantDraft) return;
    setConversationOpen(true);
    setDraft((current) => current.trim() ? current : "");
    globalThis.requestAnimationFrame?.(() => composerRef.current?.focus());
  }, [assistantDraft]);

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
    void api.getAutomationRuntime().then((runtime) => {
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
        setHarnessActivity(next);
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
    let active = true;
    if (!api || coreState !== "online" || !engagement) {
      setHarnesses([]);
      setHarnessSessions([]);
      setMcpServers([]);
      return () => { active = false; };
    }
    void Promise.all([
      api.listHarnesses(),
      api.listHarnessSessions(engagement.id),
      api.listMcpServers(),
    ]).then(([nextHarnesses, nextSessions, nextServers]) => {
      if (!active) return;
      const enabled = nextHarnesses.filter((item) => item.enabled);
      setHarnesses(enabled);
      setHarnessSessions(nextSessions.filter((item) => item.status !== "closed"));
      setMcpServers(nextServers.filter((item) => item.enabled));
      setHarnessId((current) => enabled.some((item) => item.id === current) ? current : enabled[0]?.id ?? "");
    }).catch((caughtError) => {
      void logCaughtDiagnostic("interface.sessions_page.caught_failure_03", "A handled interface operation failed.", caughtError, "sessions_page");
      if (active) { setHarnesses([]); setHarnessSessions([]); setMcpServers([]); }
    });
    return () => { active = false; };
  }, [api, coreState, engagement]);

  useEffect(() => {
    if (runtimeKind !== "harness" || sessionId) return;
    const attached = harnessSessions.find((item) => item.id === harnessSessionId);
    const profile = harnesses.find((item) => item.id === (attached?.harnessProfileId ?? harnessId));
    if (attached) setHarnessId(attached.harnessProfileId);
    setModel(attached?.model ?? profile?.defaultModel ?? profile?.models[0] ?? "");
  }, [harnessId, harnessSessionId, harnessSessions, harnesses, runtimeKind, sessionId]);
  useEffect(() => {
    if (runtimeKind !== "provider") return;
    if (!enabledProviders.length) {
      setProviderId("");
      setModel("");
      return;
    }
    if (enabledProviders.some((provider) => provider.id === providerId)) return;
    const provider = enabledProviders[0];
    setProviderId(provider.id);
    setModel(provider.defaultModel ?? provider.models[0] ?? "");
  }, [enabledProviders, providerId, runtimeKind]);

  useEffect(() => {
    if (runtimeKind !== "provider" || !selectedProvider || sessionId) return;
    const models = selectedProvider.models;
    if (!models.length) {
      if (!discoveringProviderId && model) setModel("");
      return;
    }
    if (model && models.includes(model)) return;
    const preferredModel = selectedProvider.defaultModel && models.includes(selectedProvider.defaultModel)
      ? selectedProvider.defaultModel
      : models[0];
    setModel(preferredModel ?? "");
  }, [discoveringProviderId, model, runtimeKind, selectedProvider, sessionId]);

  useEffect(() => {
    if (runtimeKind !== "provider" || !providerId) {
      lastModelDiscoveryProviderIdRef.current = undefined;
      return;
    }
    if (coreState !== "online" || sessionId) return;
    if (lastModelDiscoveryProviderIdRef.current === providerId) return;
    lastModelDiscoveryProviderIdRef.current = providerId;
    setDiscoveringProviderId(providerId);
    void refreshProvider(providerId).finally(() => {
      setDiscoveringProviderId((current) => current === providerId ? undefined : current);
    });
  }, [coreState, providerId, refreshProvider, runtimeKind, sessionId]);

  useEffect(() => {
    if (coreState !== "online" || (runtimeKind === "provider" ? !selectedProvider : !selectedHarness)) return;
    setIncludeKnowledge(canUseKnowledge);
  }, [canUseKnowledge, coreState, runtimeKind, selectedHarness, selectedProvider]);

  useEffect(() => {
    if (!detachActiveHarnessStream()) abortRef.current?.abort();
    harnessFollowDetachRef.current?.();
    harnessFollowDetachRef.current = undefined;
    setSending(false);
    setSessions([]);
    setSessionId("");
    setConversationOpen(Boolean(assistantDraft || requestedSessionId));
    setHarnessSessionId("");
    setHarnessActivity(undefined);
    setHarnessActivityError(undefined);
    setHarnessProgress(undefined);
    setMessages([]);
    setDraft(assistantDraft ? "" : "");
    setChatError(undefined);
    setRunCandidate(undefined);
    setToolCards([]);
    setActivityItems([]);
    setHarnessInteractions([]);
    setPendingResponse(undefined);
  }, [engagement?.id]);

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
      if (!detachActiveHarnessStream()) abortRef.current?.abort();
    };
  }, []);

  useEffect(() => {
    return () => harnessFollowDetachRef.current?.();
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

  const resetConversation = (open: boolean) => {
    if (!detachActiveHarnessStream()) abortRef.current?.abort();
    harnessFollowDetachRef.current?.();
    harnessFollowDetachRef.current = undefined;
    setSending(false);
    setSessionId("");
    setConversationOpen(open);
    setHarnessSessionId("");
    setHarnessActivity(undefined);
    setHarnessActivityError(undefined);
    setHarnessProgress(undefined);
    setMessages([]);
    setDraft("");
    setChatError(undefined);
    setMobileListOpen(false);
    setToolCards([]);
    setActivityItems([]);
    setHarnessInteractions([]);
    setPendingResponse(undefined);
  };

  const newConversation = () => {
    resetConversation(true);
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
      setSessions((current) => current.filter((item) => item.id !== session.id));
      if (sessionId === session.id) {
        resetConversation(false);
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
    const failures = results.filter((result) => result.status === "rejected");
    setSessions((current) => current.filter((session) => !deletedIds.has(session.id)));
    if (deletedIds.has(sessionId)) {
      resetConversation(false);
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

  const toggleConversationPanel = () => {
    setConversationPanelExpanded((current) => {
      const next = !current;
      localStorage.setItem("nebula.conversations.expanded", String(next));
      return next;
    });
  };

  const startRenamingConversation = (session: ChatSessionSummary) => {
    setRenamingSessionId(session.id);
    setRenameDraft(session.title);
    setRenameError(undefined);
  };

  const cancelRenamingConversation = () => {
    setRenamingSessionId(undefined);
    setRenameDraft("");
    setRenameError(undefined);
  };

  const renameConversation = async (event: FormEvent, session: ChatSessionSummary) => {
    event.preventDefault();
    if (!api || renamingSessionId !== session.id) return;
    const title = renameDraft.trim();
    if (!title) return;
    if (title === session.title) {
      cancelRenamingConversation();
      return;
    }
    setRenameError(undefined);
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
    }
  };

  const selectProvider = (id: string) => {
    const provider = enabledProviders.find((item) => item.id === id);
    setProviderId(id);
    setModel(provider?.defaultModel ?? provider?.models[0] ?? "");
    setIncludeKnowledge(Boolean(knowledgeSources.length && (provider?.kind === "local" || provider?.privacy === "local_only" || provider?.permitsSensitiveData)));
  };

  const modelDiscoveryInProgress = discoveringProviderId === providerId;
  const selectedModelIsUnavailable = Boolean(model && selectedProvider && !selectedProvider.models.includes(model));
  const modelPlaceholder = modelDiscoveryInProgress
    ? "Discovering models…"
    : selectedProvider?.models.length
      ? "Select model"
      : selectedProvider
        ? "No models discovered"
        : "Select provider first";

  const restoreHarnessActivity = async (history: PersistedChatMessage[]) => {
    if (!api) return;
    const owners = history.filter((message) => message.role === "assistant" && message.harnessTurnId);
    if (!owners.length) {
      setActivityItems([]);
      setHarnessInteractions([]);
      return;
    }
    const restored = await Promise.all(owners.map(async (message) => {
      const turnId = message.harnessTurnId as string;
      const [page, interactions] = await Promise.all([
        api.getHarnessTurnEvents(turnId),
        api.listHarnessInteractions(turnId),
      ]);
      return { message, events: page.events, interactions };
    }));
    let reduced: HarnessActivityItem[] = [];
    const interactions: HarnessInteraction[] = [];
    for (const group of restored) {
      for (const event of group.events) {
        if (isTimelineActivity(event)) {
          reduced = reduceHarnessActivity(reduced, event, group.message.id);
        }
      }
      interactions.push(...group.interactions);
    }
    setActivityItems(reduced);
    setHarnessInteractions(interactions);
  };

  const selectSession = async (id: string) => {
    if (!id) {
      newConversation();
      return;
    }
    if (!api) return;
    detachActiveHarnessStream();
    setSending(false);
    setConversationOpen(true);
    setLoadingHistory(true);
    setChatError(undefined);
    setHarnessProgress(undefined);
    setHarnessActivity(undefined);
    harnessFollowDetachRef.current?.();
    harnessFollowDetachRef.current = undefined;
    try {
      const summary = sessions.find((session) => session.id === id);
      const [history, pendingTurn] = await Promise.all([
        api.listChatMessages(id),
        api.getPendingChatTurn(id).catch((caughtError) => { void logCaughtDiagnostic("interface.sessions_page.caught_failure_08", "A handled interface operation failed.", caughtError, "sessions_page"); return undefined; }),
      ]);
      setSessionId(id);
      setMessages(history.map(persistedMessage));
      await restoreHarnessActivity(history);
      if (summary) {
        setRuntimeKind(summary.backend);
        setProviderId(summary.providerId ?? "");
        setHarnessId(summary.harnessProfileId ?? "");
        setHarnessSessionId(summary.harnessSessionId ?? "");
        setModel(summary.model ?? "");
      }
      if (pendingTurn && summary?.backend === "provider") {
        const assistantId = makeId("assistant-pending");
        const approval = approvals.find((item) => item.id === pendingTurn.approvalId);
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
          createdAt: new Date().toISOString(),
          citations: [],
          state: pendingTurn.status === "waiting_approval" ? "waiting_approval" : "streaming",
          durable: false,
        }]);
        setToolCards(pendingTurn.toolCallIds.map((toolCallId) => ({
          assistantId,
          toolCallId,
          capability: "Command runtime",
          status: pendingTurn.status === "waiting_approval" ? "waiting_approval" : "running",
          evidenceIds: [],
          artifacts: [],
        })));
        if (pendingTurn.status === "waiting_approval") {
          setPendingResponse({
            turnId: pendingTurn.id,
            assistantId,
            userId: "",
            request: resumeRequest,
            approval: approval
              ? { ...approval, exact_request: { tool_name: approval.toolName, arguments: approval.arguments } }
              : { id: pendingTurn.approvalId },
          });
        } else {
          setPendingResponse(undefined);
          setSending(true);
          void api.resumeChatTurn(
            pendingTurn.id,
            resumeRequest,
            (streamEvent) => applyChatEvent(streamEvent, assistantId, "", resumeRequest),
          ).then(async (response) => {
            if (response?.sessionId) await refreshSessions(response.sessionId);
          }).catch((error) => {
            void logCaughtDiagnostic("interface.sessions_page.caught_failure_09", "A handled interface operation failed.", error, "sessions_page");
            setChatError(error instanceof Error ? error.message : "Could not restore the pending response.");
          }).finally(() => setSending(false));
        }
      } else if (pendingTurn?.harnessTurnId && summary?.backend === "harness") {
        const assistantId = makeId("assistant-harness-pending");
        const turnId = pendingTurn.harnessTurnId;
        const page = await api.getHarnessTurnEvents(turnId);
        setActivityItems((current) => page.events.reduce(
          (restored, event) => isTimelineActivity(event)
            ? reduceHarnessActivity(restored, event, assistantId)
            : restored,
          current,
        ));
        setHarnessInteractions(await api.listHarnessInteractions(turnId));
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
          phase: pendingTurn.status === "waiting_approval" ? "waiting_approval" : "running",
          detail: pendingTurn.status === "waiting_approval" ? "Harness input or approval is required." : "Reconnected to the active harness turn.",
          sessionId: summary.harnessSessionId,
          turnId,
        });
        setSending(true);
        harnessFollowDetachRef.current = api.followHarnessTurnEvents(
          turnId,
          page.nextSequence,
          (event) => {
            if (event.type === "message_delta" && event.delta) {
              setMessages((current) => current.map((message) => message.id === assistantId ? { ...message, content: message.content + event.delta } : message));
            }
            if (isTimelineActivity(event)) {
              setActivityItems((current) => reduceHarnessActivity(current, event, assistantId));
            }
            if (event.type === "interaction") {
              void api.listHarnessInteractions(turnId).then(setHarnessInteractions)
                .catch((caughtError) => void logCaughtDiagnostic("interface.sessions_page.interaction_follow", "Harness interactions could not be refreshed.", caughtError, "sessions_page"));
              if (event.itemStatus && event.itemStatus !== "waiting_input") {
                setMessages((current) => current.map((message) => message.id === assistantId ? { ...message, state: "streaming" } : message));
              }
            }
          },
          () => {
            harnessFollowDetachRef.current = undefined;
            void api.listChatMessages(id).then(async (authoritative) => {
              setMessages(authoritative.map(persistedMessage));
              await restoreHarnessActivity(authoritative);
              await refreshSessions(id);
            }).catch((error) => {
              void logCaughtDiagnostic("interface.sessions_page.harness_follow_complete", "A completed harness turn could not be restored.", error, "sessions_page");
              setChatError(error instanceof Error ? error.message : "Could not restore the completed harness turn.");
            })
              .finally(() => setSending(false));
          },
          (error) => setChatError(error.message),
        );
        const approval = approvals.find((item) => item.id === pendingTurn.approvalId);
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
          approval: {
            ...approval,
            exact_request: { tool_name: approval.toolName, arguments: approval.arguments },
          },
        } : undefined);
        setToolCards([]);
      } else {
        setPendingResponse(undefined);
        setToolCards([]);
        if (summary?.backend === "harness") {
          const assistantTurnIds = new Set(history.filter((message) => message.role === "assistant").map((message) => message.harnessTurnId));
          const dangling = [...history].reverse().find((message) => message.role === "user" && message.harnessTurnId && !assistantTurnIds.has(message.harnessTurnId));
          if (dangling?.harnessTurnId) {
            const turn = await api.getHarnessTurn(dangling.harnessTurnId);
            if (["failed", "cancelled", "interrupted"].includes(turn.status)) {
              const assistantId = makeId("assistant-harness-recovery");
              const page = await api.getHarnessTurnEvents(turn.id);
              setActivityItems((current) => page.events.reduce(
                (restored, event) => isTimelineActivity(event) ? reduceHarnessActivity(restored, event, assistantId) : restored,
                current,
              ));
              setHarnessInteractions(await api.listHarnessInteractions(turn.id));
              setMessages((current) => [...current, {
                id: assistantId,
                role: "assistant",
                content: page.events.filter((event) => event.type === "message_delta").map((event) => event.delta ?? "").join(""),
                createdAt: turn.error ? new Date().toISOString() : dangling.createdAt,
                citations: [],
                state: turn.status === "cancelled" ? "cancelled" : "error",
                durable: false,
                detail: turn.error ?? "The harness turn was interrupted before its outcome was known.",
                harnessTurnId: turn.id,
              }]);
            }
          }
        }
      }
      setView("chat");
      setMobileListOpen(false);
    } catch (error) {
      void logCaughtDiagnostic("interface.sessions_page.caught_failure_10", "A handled interface operation failed.", error, "sessions_page");
      setChatError(error instanceof Error ? error.message : "Could not load the selected conversation.");
    } finally {
      setLoadingHistory(false);
    }
  };

  useEffect(() => {
    if (!requestedSessionId || requestedSessionId === sessionId || !api) return;
    void selectSession(requestedSessionId);
  }, [api, requestedSessionId, sessionId]);

  const openAttachedChat = async (id: string) => {
    if (!api || !engagement) return;
    setLoadingHistory(true);
    setChatError(undefined);
    setHarnessProgress(undefined);
    setHarnessActivity(undefined);
    try {
      const [page, history] = await Promise.all([
        api.listChatSessions(engagement.id),
        api.listChatMessages(id),
      ]);
      const ordered = page.items.sort((left, right) => right.updatedAt.localeCompare(left.updatedAt));
      const summary = ordered.find((session) => session.id === id);
      setSessions(ordered);
      setSessionId(id);
      setConversationOpen(true);
      setMessages(history.map(persistedMessage));
      await restoreHarnessActivity(history);
      if (summary) {
        setRuntimeKind(summary.backend);
        setProviderId(summary.providerId ?? "");
        setHarnessId(summary.harnessProfileId ?? "");
        setHarnessSessionId(summary.harnessSessionId ?? "");
        setModel(summary.model ?? "");
      }
      setView("chat");
      setMobileListOpen(false);
    } catch (error) {
      void logCaughtDiagnostic("interface.sessions_page.caught_failure_11", "A handled interface operation failed.", error, "sessions_page");
      setChatError(error instanceof Error ? error.message : "Could not open the execution conversation.");
    } finally {
      setLoadingHistory(false);
    }
  };

  const applyChatEvent = (
    streamEvent: ChatStreamEvent,
    assistantId: string,
    userId: string,
    request: ChatCompletionRequest,
  ) => {
    if (streamEvent.type === "started" && streamEvent.sessionId) {
      setSessionId(streamEvent.sessionId);
      void refreshSessions();
    }
    if (streamEvent.type === "started") {
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
      setMessages((current) => current.map((message) => message.id === assistantId
        ? { ...message, content: message.content + streamEvent.delta }
        : message));
    }
    if (streamEvent.type === "tool_started") {
      setHarnessProgress((current) => request.backend === "harness" ? {
        ...current,
        phase: "tool",
        detail: `Running ${streamEvent.capability}.`,
      } : current);
      setToolCards((current) => [...current.filter((item) => item.toolCallId !== streamEvent.toolCallId), {
        assistantId,
        toolCallId: streamEvent.toolCallId,
        capability: streamEvent.capability,
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
          payload: { arguments: streamEvent.arguments },
        }, assistantId));
      }
    }
    if (streamEvent.type === "tool_completed") {
      setToolCards((current) => current.some((item) => item.toolCallId === streamEvent.toolCallId)
        ? current.map((item) => item.toolCallId === streamEvent.toolCallId
          ? { ...item, status: streamEvent.status, summary: streamEvent.summary, evidenceIds: streamEvent.evidenceIds, resultArtifactId: streamEvent.resultArtifactId, artifacts: streamEvent.artifacts, receipt: streamEvent.receipt }
          : item)
        : [...current, {
          assistantId,
          toolCallId: streamEvent.toolCallId,
          capability: streamEvent.capability,
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
          payload: { receipt: streamEvent.receipt ?? {}, result_artifact_id: streamEvent.resultArtifactId },
        }, assistantId));
      }
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
        citations: streamEvent.citations,
        usage: streamEvent.usage,
        harnessTurnId: streamEvent.harnessTurnId,
        createdAt: new Date().toISOString(),
      }));
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

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    const content = draft.trim();
    const providerRuntime = runtimeKind === "provider" ? selectedProvider : undefined;
    const harnessRuntime = runtimeKind === "harness" ? selectedHarness : undefined;
    if (!content || sending || !api || coreState !== "online" || !engagement || (!providerRuntime && !harnessRuntime) || !model.trim()) return;

    const wantsKnowledge = includeKnowledge && knowledgeSources.length > 0;
    let allowCloudKnowledge = false;
    const knowledgeRuntimeIsLocal = runtimeKind === "harness" ? harnessIsLocal : providerIsLocal;
    const knowledgeRuntimeName = runtimeKind === "harness" ? harnessRuntime?.name : providerRuntime?.name;
    const knowledgeRuntimePermitsSensitive = runtimeKind === "harness" ? harnessRuntime?.permitsSensitiveData : providerRuntime?.permitsSensitiveData;
    if (wantsKnowledge && !knowledgeRuntimeIsLocal && knowledgeRuntimeName) {
      if (!knowledgeRuntimePermitsSensitive) {
        setChatError("This runtime profile is text-only. Enable project/document data in Settings or turn off knowledge retrieval.");
        return;
      }
      allowCloudKnowledge = await confirm({
        title: "Share cited excerpts?",
        message: `Allow this request to send bounded excerpts from ${knowledgeSources.length} knowledge source${knowledgeSources.length === 1 ? "" : "s"} to ${knowledgeRuntimeName}? Local-only sources will remain blocked.`,
        confirmLabel: "Allow this request",
      });
      if (!allowCloudKnowledge) {
        setChatError("Message not sent because cloud knowledge transfer was not approved.");
        return;
      }
    }

    const wantsTools = runtimeKind === "harness"
      ? Boolean(harnessSessionId
        ? harnessSessions.find((item) => item.id === harnessSessionId)?.mcpServerIds.length
        : selectedMcpIds.length)
      : canUseTools || selectedMcpIds.length > 0;
    let allowCloudToolResults = false;
    const toolRuntimeIsLocal = runtimeKind === "harness" ? harnessIsLocal : providerIsLocal;
    const toolRuntimeName = runtimeKind === "harness" ? harnessRuntime?.name : providerRuntime?.name;
    const toolRuntimePermitsSensitive = runtimeKind === "harness" ? harnessRuntime?.permitsSensitiveData : providerRuntime?.permitsSensitiveData;
    if (wantsTools && !toolRuntimeIsLocal && toolRuntimeName) {
      if (!toolRuntimePermitsSensitive) {
        setChatError("This runtime profile does not permit tool results to leave the device.");
        return;
      }
      allowCloudToolResults = await confirm({
        title: "Share redacted tool results?",
        message: `Allow this turn to send bounded tool inputs and results to ${toolRuntimeName}? Canonical output remains local and risky calls still require approval.`,
        confirmLabel: "Allow this turn",
      });
      if (!allowCloudToolResults) return;
    }

    let contextAttachments: ChatCompletionRequest["contextAttachments"];
    try {
      contextAttachments = assistantDraft
        ? [await createHashedSelectionAttachment(assistantDraft)]
        : undefined;
    } catch (attachmentError) {
      void logCaughtDiagnostic("interface.sessions_page.caught_failure_12", "A handled interface operation failed.", attachmentError, "sessions_page");
      setChatError(attachmentError instanceof Error ? attachmentError.message : "Could not attach the selected text.");
      return;
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
    clearAssistantDraft();
    setChatError(undefined);
    setSending(true);
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
    const initialSessionId = sessionId || undefined;
    let returnedSessionId = initialSessionId;
    const chatRequest: ChatCompletionRequest = {
      backend: runtimeKind,
      providerId: providerRuntime?.id,
      harnessProfileId: harnessRuntime?.id,
      harnessSessionId: !initialSessionId && harnessSessionId ? harnessSessionId : undefined,
      mcpServerIds: runtimeKind === "provider"
        ? selectedMcpIds
        : !initialSessionId && !harnessSessionId ? selectedMcpIds : [],
      engagementId: engagement.id,
      sessionId: returnedSessionId,
      model: model.trim(),
      messages: returnedSessionId
        ? [{ role: "user", content }]
        : [
            ...durableHistory.map(({ role, content: historyContent }) => ({ role, content: historyContent })),
            { role: "user", content },
          ],
      contextAttachments,
      includeKnowledge: wantsKnowledge,
      allowCloudKnowledge,
      toolsEnabled: runtimeKind === "provider" ? canUseTools : wantsTools,
      allowCloudToolResults,
    };

    try {
      const response = await api.streamChat(chatRequest, (streamEvent) => {
        if (controller.signal.aborted) return;
        if (streamEvent.type === "started") returnedSessionId = streamEvent.sessionId ?? returnedSessionId;
        if (streamEvent.type === "done") returnedSessionId = streamEvent.sessionId ?? returnedSessionId;
        applyChatEvent(streamEvent, assistantId, userId, chatRequest);
      }, controller.signal);
      returnedSessionId = response?.sessionId ?? returnedSessionId;
      if (response && returnedSessionId) {
        await refreshSessions(returnedSessionId);
      }
    } catch (error) {
      const detached = detachedHarnessStreamsRef.current.has(controller);
      if (detached) return;
      void logCaughtDiagnostic("interface.sessions_page.caught_failure_13", "A handled interface operation failed.", error, "sessions_page");
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
        } catch (caughtError) {
          void logCaughtDiagnostic("interface.sessions_page.caught_failure_14", "A handled interface operation failed.", caughtError, "sessions_page");
          // Keep the visible safe error when Core history cannot be refreshed.
          setSessionId(initialSessionId ?? "");
        }
      } else if (!initialSessionId) {
        setSessionId("");
      }
    } finally {
      if (abortRef.current === controller) {
        abortRef.current = undefined;
        streamBackendRef.current = undefined;
        setSending(false);
      }
    }
  };

  const decideInlineApproval = async (decision: "approve" | "edit" | "reject" | "stop") => {
    if (!pendingResponse || !api) return;
    const approvalId = typeof pendingResponse.approval.id === "string"
      ? pendingResponse.approval.id
      : undefined;
    if (!approvalId) {
      setChatError("The pending approval is missing its durable ID.");
      return;
    }
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
      if (pendingResponse.request.backend === "harness") {
        if (decision === "stop") {
          setMessages((current) => current.map((message) => message.id === pendingResponse.assistantId
            ? { ...message, state: "cancelled", detail: "Response stopped by the operator." }
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
        setMessages((current) => current.map((message) => message.id === pendingResponse.assistantId
        ? { ...message, state: "cancelled", detail: "Response stopped by the operator." }
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
        (streamEvent) => applyChatEvent(
          streamEvent,
          pendingResponse.assistantId,
          pendingResponse.userId,
          pendingResponse.request,
        ),
      );
      if (response?.sessionId) {
        await refreshSessions(response.sessionId);
      }
    } catch (error) {
      void logCaughtDiagnostic("interface.sessions_page.caught_failure_15", "A handled interface operation failed.", error, "sessions_page");
      setChatError(error instanceof Error ? error.message : "Could not resume the response.");
    } finally {
      setSending(false);
    }
  };

  const onComposerKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
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

  const stopCurrentResponse = async () => {
    if (runtimeKind === "harness" && api) {
      const turnId = harnessProgress?.turnId ?? harnessActivity?.turnId
        ?? [...messages].reverse().find((message) => message.state === "streaming")?.harnessTurnId;
      if (turnId) {
        try {
          await api.stopHarnessTurn(turnId);
        } catch (error) {
          void logCaughtDiagnostic("interface.sessions_page.harness_stop", "Could not stop harness turn.", error, "sessions_page");
          setChatError(error instanceof Error ? error.message : "Could not stop the harness turn.");
          return;
        }
      }
    } else if (pendingResponse && api) {
      await api.cancelChatTurn(pendingResponse.turnId);
    }
    abortRef.current?.abort();
  };

  const steerCurrentHarness = async () => {
    if (!api || harnessControlBusy) return;
    const turnId = harnessProgress?.turnId ?? harnessActivity?.turnId;
    if (!turnId) return;
    const text = globalThis.prompt("Add guidance to the active harness turn");
    if (!text?.trim()) return;
    setHarnessControlBusy(true);
    try {
      await api.steerHarnessTurn(turnId, text.trim());
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
      message: "Claude will restore tracked files to this checkpoint. This is available only while the session is idle.",
      confirmLabel: "Rewind files",
      tone: "danger",
    });
    if (!approved) return;
    setHarnessControlBusy(true);
    try {
      await api.rewindHarnessCheckpoint(checkpointSessionId, item.itemId);
    } catch (error) {
      void logCaughtDiagnostic("interface.sessions_page.harness_rewind", "The Claude checkpoint could not be rewound.", error, "sessions_page");
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
        objective,
        backend: "harness",
        harnessProfileId: chat.harnessProfileId,
        harnessSessionId: chat.harnessSessionId,
        model: chat.model,
        maxDurationSeconds: 900,
        maxTokens: 32_000,
        maxToolCalls: 100,
        allowCloudToolResults,
      });
      setView("missions");
    } catch (error) {
      void logCaughtDiagnostic("interface.sessions_page.caught_failure_16", "A handled interface operation failed.", error, "sessions_page");
      setChatError(error instanceof Error ? error.message : "Could not continue this chat as a mission.");
    }
  };

  const runtimeReady = runtimeKind === "provider" ? Boolean(selectedProvider) : Boolean(selectedHarness);
  const canSend = Boolean(api && coreState === "online" && engagement && runtimeReady && model.trim() && draft.trim() && !sending);
  const visibleHarnessProgress: HarnessProgress | undefined = runtimeKind !== "harness" || !harnessSessionId
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
  const runnableLanguages = useMemo(() => new Set<ExecutionLanguage>(
    executionCapabilities?.runtimes
      .filter((runtime) => runtime.offline && runtime.scopedNetwork)
      .map((runtime) => runtime.language) ?? [],
  ), [executionCapabilities]);

  return (
    <div className={`page sessions-page${view === "chat" ? " chat-active" : ""}${fullScreen ? " full-screen" : ""}`}>
      <PageHeader
        title="Workbench"
        description="Start in Terminal, edit shared code, browse a target, ask the assistant, or open your project files."
        showIntroduction={false}
        actions={view === "chat" ? <button className="button primary" type="button" disabled={!engagement} title={!engagement ? "Create or select a project before starting chat" : undefined} onClick={newConversation}><Plus size={16} /> New chat</button> : view === "missions" ? <NewMissionButton /> : undefined}
      />

      <div className="session-toolbar">
        <div className="session-tabs" role="tablist" aria-label="Workbench views">
          <button type="button" role="tab" aria-selected={view === "terminal"} onClick={() => setView("terminal")}><SquareTerminal size={16} /> Terminal</button>
          <button type="button" role="tab" aria-label="Workspace code editor" aria-selected={view === "code"} onClick={() => setView("code")}><Braces size={16} /> Code</button>
          <button type="button" role="tab" aria-label="Project browser" aria-selected={view === "browser"} onClick={() => setView("browser")}><Globe2 size={16} /> Browser</button>
          <button type="button" role="tab" aria-label="Analyst chat" aria-selected={view === "chat"} onClick={() => setView("chat")}><MessageSquare size={16} /> Assistant</button>
          <button type="button" role="tab" aria-label="Workspace files" aria-selected={view === "workspace"} onClick={() => setView("workspace")}><FolderOpen size={16} /> Files</button>
          <button type="button" role="tab" aria-label="Project notes" aria-selected={view === "notes"} onClick={() => setView("notes")}><NotebookPen size={16} /> Notes</button>
          <button type="button" role="tab" aria-label="Autonomous missions" aria-selected={view === "missions"} onClick={() => setView("missions")}><Bot size={16} /> Missions</button>
          <button type="button" role="tab" aria-label="Activity history" aria-selected={view === "activity"} onClick={() => setView("activity")}><FileClock size={16} /> Activity</button>
        </div>
        {view === "chat" && <button className="session-mobile-list" type="button" aria-pressed={mobileListOpen} onClick={() => setMobileListOpen((value) => !value)}><MessageSquare size={15} /> {mobileListOpen ? "Current chat" : "Conversations"}</button>}
        <div className="session-toolbar-actions">
          <div className="session-scope"><ShieldCheck size={15} /> Human controlled · {engagement?.name ?? "no project"}</div>
          <button
            className="icon-button subtle workbench-full-screen-toggle"
            type="button"
            aria-label={fullScreen ? "Exit full screen workbench" : "Enter full screen workbench"}
            aria-pressed={fullScreen}
            title={fullScreen ? "Exit full screen (Esc)" : "Open full screen workbench"}
            onClick={() => setFullScreen((value) => !value)}
          >
            {fullScreen ? <Minimize2 size={17} /> : <Maximize2 size={17} />}
          </button>
        </div>
      </div>

      <div className={`session-layout ${view}${mobileListOpen ? " mobile-list-open" : ""}${view === "chat" && conversationPanelExpanded ? " conversation-panel-expanded" : ""}`}>
        {view === "chat" && <aside className="session-list" aria-label="Conversations">
          <header><div><span>Conversations</span><strong>{sessions.length} saved</strong></div><div className="session-list-header-actions"><button className="icon-button subtle" type="button" aria-label="Delete all conversations" title={sending || pendingResponse ? "Wait for the active response to finish" : "Delete all conversations"} disabled={!sessions.length || Boolean(deletingSessionId) || deletingAllSessions || sending || Boolean(pendingResponse)} onClick={() => void deleteAllConversations()}>{deletingAllSessions ? <LoaderCircle className="spin" size={14} /> : <Trash2 size={14} />}</button><button className="icon-button subtle" type="button" aria-label={conversationPanelExpanded ? "Collapse conversations panel" : "Expand conversations panel"} aria-pressed={conversationPanelExpanded} onClick={toggleConversationPanel}>{conversationPanelExpanded ? <PanelLeftClose size={16} /> : <PanelLeftOpen size={16} />}</button><button className="icon-button subtle" type="button" aria-label="New conversation" disabled={!engagement} onClick={newConversation}><Plus size={16} /></button></div></header>
          <nav>
            <button className={conversationOpen && !sessionId ? "active" : undefined} type="button" onClick={newConversation}><MessageSquare size={16} /><span><strong>New conversation</strong><small>{runtimeKind === "harness" ? selectedHarness?.name ?? "Choose a harness" : selectedProvider?.name ?? "Choose a provider"}</small></span></button>
            {sessions.map((session) => <div className={`session-list-item${session.id === sessionId ? " active" : ""}${renamingSessionId === session.id ? " renaming" : ""}`} key={session.id}>{renamingSessionId === session.id ? <form className="session-rename-form" onSubmit={(event) => void renameConversation(event, session)}><label className="sr-only" htmlFor={`conversation-name-${session.id}`}>Conversation name</label><input id={`conversation-name-${session.id}`} aria-label={`Rename conversation ${session.title}`} autoFocus maxLength={300} value={renameDraft} onKeyDown={(event) => { if (event.key === "Escape") cancelRenamingConversation(); }} onChange={(event) => setRenameDraft(event.target.value)} /><button className="icon-button subtle" type="submit" aria-label="Save conversation name" disabled={!renameDraft.trim()}><Check size={14} /></button><button className="icon-button subtle" type="button" aria-label={`Cancel renaming ${session.title}`} onClick={cancelRenamingConversation}><X size={14} /></button></form> : <><button className="session-select" type="button" onClick={() => void selectSession(session.id)}><MessageSquare size={16} /><span><strong title={session.title}>{session.title}</strong><small title={session.model || undefined}>{session.model || "Saved conversation"}</small></span></button><button className="icon-button subtle" type="button" aria-label={`Rename conversation ${session.title}`} disabled={deletingAllSessions || deletingSessionId === session.id || (session.id === sessionId && (sending || Boolean(pendingResponse)))} title={session.id === sessionId && (sending || pendingResponse) ? "Wait for the active response to finish" : `Rename ${session.title}`} onClick={() => startRenamingConversation(session)}><Pencil size={14} /></button><button className="icon-button subtle" type="button" aria-label={`Delete conversation ${session.title}`} disabled={deletingAllSessions || deletingSessionId === session.id || (session.id === sessionId && (sending || Boolean(pendingResponse)))} title={session.id === sessionId && (sending || pendingResponse) ? "Wait for the active response to finish" : `Delete ${session.title}`} onClick={() => void deleteConversation(session)}>{deletingSessionId === session.id ? <LoaderCircle className="spin" size={14} /> : <Trash2 size={14} />}</button></>}</div>)}
            {renameError && <DiagnosticErrorNotice error={renameError} fallback="The session could not be renamed." compact />}
          </nav>
        </aside>}
        <section className="session-workspace">
          {api && engagement && <div className="persistent-terminal" hidden={view !== "terminal"}>
            <Suspense fallback={<div className="empty-state compact"><LoaderCircle className="spin" size={20} /><strong>Loading Terminal…</strong></div>}><ContainerTerminalPanel active={view === "terminal"} api={api} capturedBy={activeOperator?.id} engagementId={engagement.id} engagementName={engagement.name} onUploadEvidence={uploadEvidence} setupTerminalStatus={setupStatus?.terminal.status} setupTerminalDetail={setupStatus?.terminal.detail} /></Suspense>
          </div>}
          {api && engagement && <div className="persistent-code-editor" hidden={view !== "code"}>
            <Suspense fallback={<div className="empty-state compact"><LoaderCircle className="spin" size={20} /><strong>Loading Code editor…</strong></div>}><CodeEditorPanel active={view === "code"} api={api} engagementId={engagement.id} /></Suspense>
          </div>}
          {engagement && <div className="persistent-browser" hidden={view !== "browser"}>
            <WorkbenchBrowser active={view === "browser"} projectId={engagement.id} onOpenFiles={() => setView("workspace")} />
          </div>}
          {(view === "terminal" || view === "code") && (!api || !engagement) ? (
            <div className="empty-state"><FolderOpen size={24} /><strong>Preparing your project</strong><p>Terminal and Code become available as soon as Nebula finishes creating or loading a project.</p></div>
          ) : view === "terminal" || view === "code" || (view === "browser" && engagement) ? null : view === "missions" && api && engagement ? (
            <AgentsPage embedded />
          ) : view === "activity" && api && engagement ? (
            <div className="workbench-activity-stack">
              <ExecutionHistory api={api} engagementId={engagement.id} refreshKey={executionRefresh} onRerun={setRunCandidate} providers={providers} onChatAttached={openAttachedChat} />
              <TerminalCommandHistoryPanel api={api} engagementId={engagement.id} />
            </div>
          ) : view === "workspace" && api && engagement ? (
            <WorkspacePanel api={api} engagementId={engagement.id} engagementName={engagement.name} onUseWithAssistant={requestNebulaDraft} />
          ) : view === "notes" && api && engagement ? (
            <NotesPanel
              api={api}
              engagementId={engagement.id}
              evidenceOptions={evidence.map((item) => ({ id: item.id, label: item.title }))}
              assetOptions={assets.map((item) => ({ id: item.id, label: item.displayName }))}
              providers={providers}
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
            <div className="chat-panel">
              <details className="chat-settings" open={!runtimeReady}>
                <summary>Assistant settings</summary>
                <div className="chat-context-bar">
                <label><span>Runtime</span><select aria-label="Chat runtime" value={runtimeKind} disabled={sending || Boolean(sessionId)} onChange={(event) => { const next = event.target.value as "provider" | "harness"; setRuntimeKind(next); setHarnessSessionId(""); setSelectedMcpIds([]); if (next === "provider") selectProvider(providerId || enabledProviders[0]?.id || ""); else { setModel(selectedHarness?.defaultModel ?? selectedHarness?.models[0] ?? ""); } }}><option value="provider">Provider</option><option value="harness">Agent harness</option></select></label>
                {runtimeKind === "provider" ? <label><span>Provider</span><select aria-label="Chat provider" value={providerId} disabled={sending || Boolean(sessionId)} onChange={(event) => selectProvider(event.target.value)}><option value="">Select provider</option>{enabledProviders.map((provider) => <option value={provider.id} key={provider.id}>{provider.name} · {provider.state}</option>)}</select></label> : <><label><span>Harness</span><select aria-label="Chat harness" value={harnessId} disabled={sending || Boolean(sessionId) || Boolean(harnessSessionId)} onChange={(event) => setHarnessId(event.target.value)}><option value="">Select harness</option>{harnesses.map((harness) => <option value={harness.id} key={harness.id}>{harness.name}</option>)}</select></label><label><span>Session</span><select aria-label="Chat harness session" value={harnessSessionId} disabled={sending || Boolean(sessionId)} onChange={(event) => setHarnessSessionId(event.target.value)}><option value="">New session</option>{harnessSessions.filter((item) => item.harnessProfileId === harnessId || item.id === harnessSessionId).map((item) => <option value={item.id} key={item.id}>{item.model} · {item.status}</option>)}</select></label></>}
                {runtimeKind === "provider" ? <label title={selectedProvider?.message}><span>Model</span><select aria-label="Chat model" aria-busy={modelDiscoveryInProgress} value={model} disabled={sending || Boolean(sessionId) || modelDiscoveryInProgress || !selectedProvider?.models.length} onChange={(event) => setModel(event.target.value)}><option value="">{modelPlaceholder}</option>{selectedModelIsUnavailable && <option value={model}>{model} · saved model</option>}{selectedProvider?.models.map((item) => <option value={item} key={item}>{item}</option>)}</select></label> : <label><span>Model</span><select aria-label="Chat harness model" value={model} disabled={sending || Boolean(sessionId) || Boolean(harnessSessionId) || !harnessModelOptions.length} onChange={(event) => setModel(event.target.value)}><option value="">{harnessModelOptions.length ? "Select model" : "Run a harness check to discover models"}</option>{harnessModelOptions.map((item) => <option value={item} key={item}>{item}</option>)}</select></label>}
                {runtimeKind === "provider" ? <><label className="chat-knowledge-toggle"><input type="checkbox" checked={includeKnowledge && canUseKnowledge} disabled={!canUseKnowledge || sending} onChange={(event) => setIncludeKnowledge(event.target.checked)} /><span>Use knowledge<small>{knowledgeSources.length ? runtimePermitsKnowledge ? `${knowledgeSources.length} source${knowledgeSources.length === 1 ? "" : "s"}` : "Profile is text-only" : "No sources loaded"}</small></span></label><div className="chat-knowledge-toggle" role="status" title={commandRuntimeUnavailableReason}><ShieldCheck size={15} /><span>Command runtime<small>{canUseTools ? "run_command and process_io ready" : commandRuntimeUnavailableReason}</small></span></div><div className="chat-harness-mcp"><span>MCP servers</span>{mcpServers.length ? mcpServers.map((server) => <label className="chat-knowledge-toggle" key={server.id}><input type="checkbox" checked={selectedMcpIds.includes(server.id)} disabled={sending} onChange={(event) => setSelectedMcpIds((current) => event.target.checked ? [...current, server.id] : current.filter((id) => id !== server.id))} /><span>{server.name}<small>{server.tools.length} tools · Core-captured</small></span></label>) : <small>No enabled MCP profiles</small>}</div></> : <><label className="chat-knowledge-toggle"><input type="checkbox" checked={includeKnowledge && canUseKnowledge} disabled={!canUseKnowledge || sending} onChange={(event) => setIncludeKnowledge(event.target.checked)} /><span>Use knowledge<small>{knowledgeSources.length ? runtimePermitsKnowledge ? `${knowledgeSources.length} bounded source${knowledgeSources.length === 1 ? "" : "s"}` : "Harness is text-only" : "No sources loaded"}</small></span></label><div className="chat-harness-mcp"><span>MCP servers</span>{harnessSessionId ? <small>Frozen in selected session</small> : mcpServers.length ? mcpServers.map((server) => <label className="chat-knowledge-toggle" key={server.id}><input type="checkbox" checked={selectedMcpIds.includes(server.id)} disabled={sending || Boolean(sessionId)} onChange={(event) => setSelectedMcpIds((current) => event.target.checked ? [...current, server.id] : current.filter((id) => id !== server.id))} /><span>{server.name}<small>{server.tools.length} tools · {server.defaultApproval.replace("_", " ")}</small></span></label>) : <small>No enabled MCP profiles</small>}</div></>}
                </div>
              </details>
              <div className="chat-scroll" ref={scrollRef} aria-live="polite">
                {loadingHistory ? <div className="chat-thinking"><LoaderCircle className="spin" size={14} /> Loading conversation…</div> : messages.length ? messages.map((message) => (
                  <article
                    className={`chat-message ${message.role === "user" ? "operator" : "assistant"}`}
                    data-sequence={message.sequence}
                    data-selection-source-kind={message.role === "assistant" ? "assistant_message" : "chat_message"}
                    data-selection-source-id={message.id}
                    data-selection-source-label={message.role === "assistant" ? "Assistant response" : "Chat message"}
                    key={message.id}
                    tabIndex={-1}
                  >
                    <span className="chat-avatar">{message.role === "user" ? "You" : "N"}</span>
                    <div>
                      <header><strong>{message.role === "user" ? "You" : "Nebula assistant"}</strong><span>{timeLabel(message.createdAt)}</span>{message.usage && <span>{message.usage.totalTokens} tokens</span>}</header>
                      {message.content && (message.role === "assistant" && message.state === "complete" ? <AssistantMarkdown content={message.content} messageId={message.id} durable={message.durable} runnableLanguages={runnableLanguages} onRun={setRunCandidate} /> : <p>{message.content}</p>)}
                      {activityItems.some((item) => item.assistantId === message.id && shouldShowActivityItem(item)) && <section className="harness-timeline" aria-label="Harness activity">
                        {activityItems.filter((item) => item.assistantId === message.id && shouldShowActivityItem(item)).map((item) => <details className={`harness-activity-card kind-${item.kind ?? "notice"}${item.parentItemId ? " nested" : ""}`} open={["running", "streaming", "waiting_approval", "waiting_input", "failed", "interrupted"].includes(item.status ?? "") || item.kind === "plan"} key={item.key}>
                          <summary><span className={`status-dot ${["completed", "complete", "success"].includes(item.status ?? "") ? "healthy" : ["failed", "error", "cancelled"].includes(item.status ?? "") ? "unavailable" : "pending"}`} /><strong>{item.title}</strong>{shouldShowActivityKind(item) && <code>{item.kind?.replaceAll("_", " ")}</code>}{item.status && <span>{item.status.replaceAll("_", " ")}</span>}</summary>
                          <div className="harness-activity-body">
                            {item.summary && <p>{item.summary}</p>}
                            {reasoningSummaryText(item) && <p className="harness-reasoning-summary">{reasoningSummaryText(item)}</p>}
                            {reasoningSummaryState(item) === "pending" && !reasoningSummaryText(item) && <p>Codex is reasoning. A display-safe summary will appear if one is provided.</p>}
                            {reasoningSummaryState(item) === "not_provided" && <p>No display-safe reasoning summary was provided by Codex.</p>}
                            {reasoningSummaryState(item) && <small className="harness-reasoning-note">Provider-safe summary only. Private reasoning traces are not captured or retained.</small>}
                            {item.kind === "plan" && Array.isArray(item.payload.plan) && <ol className="harness-plan">{item.payload.plan.map((step, index) => <li key={index}>{typeof step === "string" ? step : JSON.stringify(step)}</li>)}</ol>}
                            {item.kind === "file_change" && Array.isArray(item.payload.files) && <ul className="harness-file-list">{item.payload.files.map((file, index) => <li key={index}><FileClock size={13} /> {typeof file === "string" ? file : JSON.stringify(file)}</li>)}</ul>}
                            {Object.entries(item.streams).filter(([stream]) => stream !== "reasoning_summary").map(([stream, output]) => <div className="harness-output" key={stream}><small>{stream}</small><pre tabIndex={0}>{output}</pre></div>)}
                            {typeof item.payload.diff === "string" && item.payload.diff && <div className="harness-output diff"><small>Unified diff</small><pre tabIndex={0}>{item.payload.diff}</pre></div>}
                            {item.kind && ["command", "tool", "web_search", "browser", "image", "skill", "hook", "review", "subagent"].includes(item.kind) && Object.keys(item.payload).length > 0 && <details className="harness-structured-details"><summary>Arguments and result details</summary><pre className="harness-structured" tabIndex={0}>{JSON.stringify(item.payload, null, 2)}</pre></details>}
                            {item.usage && <small>{item.usage.totalTokens.toLocaleString()} tokens{item.usage.reasoningTokens ? ` · ${item.usage.reasoningTokens.toLocaleString()} reasoning` : ""}{item.usage.costUsd ? ` · $${item.usage.costUsd.toFixed(4)}` : ""}{item.usage.durationMs ? ` · ${(item.usage.durationMs / 1000).toFixed(1)}s` : ""}</small>}
                            {item.artifactIds.length > 0 && <div className="scope-chip-list">{item.artifactIds.map((id) => <span title={id} key={id}>Artifact {id.slice(0, 8)}</span>)}</div>}
                            {item.kind === "subagent" && item.status === "running" && selectedHarness?.capabilities?.subagentControl && <button className="button quiet" type="button" disabled={harnessControlBusy} onClick={() => void stopSubagent(item)}>Stop subagent</button>}
                            {item.type === "checkpoint" && selectedHarness?.capabilities?.checkpointRewind && <button className="button quiet" type="button" disabled={harnessControlBusy || (item.sessionId === harnessSessionId && harnessActivity?.busy)} title={item.sessionId === harnessSessionId && harnessActivity?.busy ? "Checkpoint rewind is available while the session is idle" : undefined} onClick={() => void rewindCheckpoint(item)}>Rewind files here</button>}
                          </div>
                        </details>)}
                      </section>}
                      {runtimeKind !== "harness" && toolCards.filter((card) => card.assistantId === message.id).map((card) => <div className="chat-tool-card" key={card.toolCallId}><strong>{card.capability}</strong><span>{card.status.replaceAll("_", " ")}</span>{card.summary && <small>{card.summary}</small>}{card.evidenceIds.map((id) => <Link to={`/evidence?id=${encodeURIComponent(id)}`} key={id}>Evidence {id.slice(0, 8)}</Link>)}{card.status !== "running" && <button className="button quiet" type="button" onClick={() => void openArtifacts(card)}><Search size={13} /> Artifacts</button>}</div>)}
                      {harnessInteractions.filter((interaction) => interaction.harnessTurnId === message.harnessTurnId && interaction.status === "pending").map((interaction) => <div className="chat-approval-card harness-interaction" key={interaction.id}>
                        <strong>{interaction.prompt}</strong>
                        {interaction.kind === "user_input" ? interaction.questions.map((question, index) => {
                          const questionId = typeof question.id === "string" ? question.id : String(index);
                          const answerKey = `${interaction.id}:${questionId}`;
                          return <label key={questionId}><span>{typeof question.question === "string" ? question.question : `Question ${index + 1}`}</span>{Array.isArray(question.options) ? <select value={interactionAnswers[answerKey] ?? ""} onChange={(event) => setInteractionAnswers((current) => ({ ...current, [answerKey]: event.target.value }))}><option value="">Select an answer</option>{question.options.map((option, optionIndex) => <option value={typeof option === "object" && option && "label" in option ? String(option.label) : String(option)} key={optionIndex}>{typeof option === "object" && option && "label" in option ? String(option.label) : String(option)}</option>)}</select> : <input type={interaction.containsSecret ? "password" : "text"} value={interactionAnswers[answerKey] ?? ""} onChange={(event) => setInteractionAnswers((current) => ({ ...current, [answerKey]: event.target.value }))} autoComplete="off" />}</label>;
                        }) : <label><span>JSON response</span>{interaction.containsSecret ? <input type="password" value={interactionAnswers[interaction.id] ?? ""} onChange={(event) => setInteractionAnswers((current) => ({ ...current, [interaction.id]: event.target.value }))} autoComplete="off" /> : <textarea rows={3} value={interactionAnswers[interaction.id] ?? ""} onChange={(event) => setInteractionAnswers((current) => ({ ...current, [interaction.id]: event.target.value }))} autoComplete="off" />}</label>}
                        {interaction.containsSecret && <small>Secret answer is forwarded in memory and will not be persisted.</small>}
                        <div><button className="button secondary" type="button" disabled={harnessControlBusy} onClick={() => void decideHarnessInteraction(interaction, "decline")}>Decline</button><button className="button primary" type="button" disabled={harnessControlBusy} onClick={() => void decideHarnessInteraction(interaction, "answer")}>Submit</button></div>
                      </div>)}
                      {message.state === "streaming" && !message.content && <div className="chat-thinking"><span /><span /><span /> {runtimeKind === "harness" ? visibleHarnessProgress?.detail ?? "Waiting for harness" : "Waiting for provider"}</div>}
                      {message.state === "waiting_approval" && pendingResponse?.assistantId === message.id && <div className="chat-approval-card"><strong>Approval required</strong><pre>{JSON.stringify(pendingResponse.approval.exact_request ?? {}, null, 2)}</pre><div><button className="button secondary" type="button" onClick={() => void decideInlineApproval("reject")}>Reject</button><button className="button secondary" type="button" onClick={() => void decideInlineApproval("stop")}>Stop response</button><button className="button primary" type="button" onClick={() => void decideInlineApproval("approve")}>Approve</button></div></div>}
                      {message.detail && <DiagnosticErrorNotice error={message.detail} fallback="The response could not be completed." compact />}
                      {runtimeKind === "harness" && ["error", "cancelled"].includes(message.state) && message.harnessTurnId && <button className="button quiet" type="button" disabled={harnessControlBusy} onClick={() => void retryHarnessMessage(message)}>Retry as linked turn</button>}
                      {message.citations.map((citation) => <Link className="citation-chip" to={`/knowledge?source=${encodeURIComponent(citation.sourceId)}`} title={citation.excerpt} key={`${citation.sourceId}-${citation.chunkId}`}><Braces size={13} /> {citation.name}{citation.page ? ` · p. ${citation.page}` : ""}</Link>)}
                    </div>
                  </article>
                )) : <div className="empty-state compact"><MessageSquare size={23} /><strong>Start an analyst conversation</strong><p>New chats can use the session-scoped command runtime when the exact model is verified.</p></div>}
              </div>
              {pendingResponse && pendingResponse.request.backend !== "harness" && <div className="chat-inline-approval-actions"><button className="button secondary" type="button" onClick={() => void decideInlineApproval("edit")}>Edit pending request</button></div>}
              {chatError && <DiagnosticErrorNotice error={chatError} fallback="The chat operation could not be completed." compact />}
              {runtimeKind === "harness" && visibleHarnessProgress && <div className={`chat-harness-progress phase-${visibleHarnessProgress.phase}`} role="status" aria-live="polite"><span className={`status-dot ${visibleHarnessProgress.phase === "complete" || visibleHarnessProgress.phase === "ready" ? "healthy" : visibleHarnessProgress.phase === "failed" || visibleHarnessProgress.phase === "status_unavailable" ? "unavailable" : "pending"}`} /><div><strong>{harnessPhaseLabel(visibleHarnessProgress.phase)}</strong><small>{visibleHarnessProgress.detail}</small>{visibleHarnessProgress.sessionId && <code title={visibleHarnessProgress.sessionId}>Session {visibleHarnessProgress.sessionId.slice(0, 8)}{visibleHarnessProgress.previousSessionId ? " · independent parallel session" : ""}</code>}</div>{sending && selectedHarness?.capabilities?.steering && <button className="button quiet harness-steer-button" type="button" disabled={harnessControlBusy} onClick={() => void steerCurrentHarness()}><Plus size={13} aria-hidden="true" /> Add guidance</button>}</div>}
              <form className="chat-composer" onSubmit={(event) => void submit(event)}>
                {assistantDraft && <div className="chat-context-attachment" role="group" aria-label="Selected context attachment">
                  <div><strong>{assistantDraft.source.label}</strong><small>{assistantDraft.text.length.toLocaleString()} characters{assistantDraft.truncated ? " · truncated to the first 20,000" : ""}</small></div>
                  <p>{assistantDraft.text.slice(0, 180)}{assistantDraft.text.length > 180 ? "…" : ""}</p>
                  <button className="icon-button subtle" type="button" aria-label="Remove selected context" onClick={clearAssistantDraft}><X size={14} /></button>
                </div>}
                <label className="sr-only" htmlFor="analyst-message">Message the analyst assistant</label>
                <textarea ref={composerRef} id="analyst-message" value={draft} disabled={!engagement || !runtimeReady || loadingHistory} placeholder={!engagement ? "Create or select a project to chat…" : runtimeReady ? "Ask about this project…" : "Add a model or harness in Settings…"} rows={3} onKeyDown={onComposerKeyDown} onChange={(event) => setDraft(event.target.value)} />
                <footer><span>{runtimeKind === "harness" ? sending ? visibleHarnessProgress?.detail ?? "Harness is working" : harnessActivity?.busy ? "Active work detected · sending starts an independent session" : `${harnessSessionId ? "Resumed" : "New"} harness session · ${selectedMcpIds.length || harnessSessions.find((item) => item.id === harnessSessionId)?.mcpServerIds.length || 0} MCP` : canUseTools || selectedMcpIds.length ? `${canUseTools ? "Command runtime" : "No command runtime"} · ${selectedMcpIds.length} MCP` : includeKnowledge && canUseKnowledge ? providerIsLocal ? "Cited retrieval stays local" : "Cloud excerpts require confirmation" : "Text-only chat"}</span>{sending ? <button className="button secondary square" type="button" aria-label="Stop response" disabled={runtimeKind === "harness" && selectedHarness?.capabilities?.interruption === false} title={runtimeKind === "harness" && selectedHarness?.capabilities?.interruption === false ? "This harness does not advertise turn interruption" : undefined} onClick={() => void stopCurrentResponse()}><Square size={15} /></button> : <button className="button primary square" type="submit" disabled={!canSend} aria-label="Send message"><Send size={16} /></button>}</footer>
              </form>
            </div>
          )}
        </section>

        {view === "chat" && <aside className="session-inspector" aria-label="Session inspector">
          <header><div><span>Context</span><strong>Session details</strong></div></header>
          <dl><div><dt>Active operator</dt><dd>{activeOperator?.displayName ?? "No active operator"}</dd></div><div><dt>Conversation</dt><dd>{conversationOpen ? sessionId ? sessions.find((session) => session.id === sessionId)?.title ?? "Saved chat" : "Unsaved chat" : "None selected"}</dd></div><div><dt>Runtime</dt><dd>{runtimeKind === "harness" ? selectedHarness?.name ?? "Harness" : selectedProvider?.name ?? "Not selected"}</dd></div><div><dt>Code Run</dt><dd><span className={`status-dot ${executionCapabilities?.ready ? "healthy" : "unavailable"}`} /> {executionCapabilities?.ready ? "Review available" : "Unavailable"}</dd></div></dl>
          {sessionId && sessions.find((session) => session.id === sessionId)?.backend === "harness" && <button className="button primary full" type="button" disabled={sending} onClick={() => void continueAsMission()}><Bot size={15} /> Continue as mission</button>}
          <section><h3>Knowledge boundary</h3><div className="scope-chip-list"><span>{knowledgeSources.length} source{knowledgeSources.length === 1 ? "" : "s"}</span><span>{providerIsLocal ? "Local retrieval" : includeKnowledge && canUseKnowledge ? "Confirm each cloud request" : "Text only"}</span></div></section>
          <section><h3>Execution boundary</h3><div className="empty-state mini"><Braces size={19} /><p>{canUseTools ? "Bash commands run in this session's isolated container; configured approvals pause this response." : commandRuntimeUnavailableReason ?? "Command runtime is unavailable for this session."}</p></div></section>
          <section><h3>Session evidence</h3><div className="empty-state mini"><Braces size={19} /><p>Citations identify canonical ingested chunks and transcript messages.</p></div></section>
        </aside>}
      </div>
      {artifactInspector && <div className="dialog-backdrop"><section className="provider-dialog resource-dialog" role="dialog" aria-modal="true" aria-labelledby="artifact-inspector-title"><header><div><small>Untrusted tool data · bounded retrieval</small><h2 id="artifact-inspector-title">{artifactInspector.capability} artifacts</h2></div><button className="icon-button subtle" type="button" aria-label="Close artifact inspector" onClick={() => setArtifactInspector(undefined)}><X size={17} /></button></header>{artifactInspector.receipt && <div className="knowledge-status" role="status"><ShieldCheck size={15} /><span>Receipt {String(artifactInspector.receipt.status ?? artifactInspector.status)} · parser {String((artifactInspector.receipt.parser as Record<string, unknown> | undefined)?.state ?? "not configured")}{Array.isArray(artifactInspector.receipt.warnings) && artifactInspector.receipt.warnings.length ? ` · ${artifactInspector.receipt.warnings.join(" · ")}` : ""}</span></div>}<div className="runtime-resource-list">{artifactInspector.artifacts.length ? artifactInspector.artifacts.map((artifact) => <article className="runtime-resource-card" key={artifact.artifactId}><header><div><strong>{artifact.filename ?? artifact.kind}</strong><code title={artifact.sha256}>{artifact.sha256.slice(0, 16)}…</code></div><span>{artifact.truncated ? "truncated" : artifact.searchable ? "searchable" : "binary"}</span></header><small>{artifact.byteCount.toLocaleString()} retained byte{artifact.byteCount === 1 ? "" : "s"}{artifact.observedByteCount !== artifact.byteCount ? ` · ${artifact.observedByteCount.toLocaleString()} observed` : ""} · {artifact.mediaType}</small><footer>{artifact.searchable && <button className="button quiet" type="button" onClick={() => void readArtifact(artifact.artifactId)} disabled={artifactBusy}>Read excerpt</button>}<button className="button quiet" type="button" onClick={() => void saveRawArtifact(artifact)}>Save acknowledged raw</button></footer></article>) : <p>Artifact references are available through search for this historical or gateway result.</p>}</div><form className="chat-composer" onSubmit={(event) => void searchArtifacts(event)}><label>Search all searchable artifacts<input value={artifactQuery} maxLength={512} placeholder="open 443/tcp" onChange={(event) => setArtifactQuery(event.target.value)} /></label><button className="button primary" type="submit" disabled={artifactBusy || !artifactQuery.trim()}><Search size={14} /> {artifactBusy ? "Searching…" : "Search"}</button></form>{artifactError && <DiagnosticErrorNotice error={artifactError} fallback="Artifact retrieval failed." compact />}{artifactSearch && <section><h3>Search matches</h3>{artifactSearch.matches.length ? artifactSearch.matches.map((match, index) => <article className="panel" key={`${match.artifactId}-${match.line}-${index}`}><header><strong>{match.filename ?? match.artifactId}</strong><button className="button quiet" type="button" onClick={() => void readArtifact(match.artifactId, Math.max(1, match.line - 10))}>Read around line {match.line}</button></header><pre>{match.context.map((line) => `${line.line}: ${line.text}${line.lineTruncated ? "…" : ""}`).join("\n")}</pre></article>) : <p>No matching lines.</p>}{artifactSearch.truncated && <small>More matches are available with the continuation cursor.</small>}</section>}{artifactRead && <section><h3>{artifactRead.filename ?? artifactRead.artifactId}</h3>{artifactRead.searchable ? <pre>{artifactRead.lines.map((line) => `${line.line}: ${line.text}${line.lineTruncated ? "…" : ""}`).join("\n")}</pre> : <p>This binary artifact is retained but not searchable.</p>}{artifactRead.continuationStartingLine && <button className="button quiet" type="button" onClick={() => void readArtifact(artifactRead.artifactId, artifactRead.continuationStartingLine)}>Read next lines</button>}</section>}<footer><span>Excerpts are redacted, line-numbered, and capped at 8 KiB.</span><button className="button secondary" type="button" onClick={() => setArtifactInspector(undefined)}>Close</button></footer></section></div>}
      {runCandidate && api && engagement && <ExecutionReviewDialog api={api} engagementId={engagement.id} candidate={runCandidate} capabilities={executionCapabilities} onClose={() => setRunCandidate(undefined)} onStarted={() => { setExecutionRefresh((value) => value + 1); setView("activity"); }} />}
    </div>
  );
}
