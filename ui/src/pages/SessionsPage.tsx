import { lazy, Suspense, useEffect, useMemo, useRef, useState, type FormEvent, type KeyboardEvent } from "react";
import {
  Bot,
  Braces,
  Check,
  FileClock,
  FolderOpen,
  LoaderCircle,
  MessageSquare,
  NotebookPen,
  PanelLeftClose,
  PanelLeftOpen,
  Pencil,
  Plus,
  Send,
  ShieldCheck,
  Square,
  SquareTerminal,
  Trash2,
  X,
} from "lucide-react";
import { Link, useSearchParams } from "react-router-dom";
import { providerModelVerification } from "../api/providerCapabilities";
import { selectProjectTools } from "../api/toolAutomation";
import type {
  ChatCitation,
  ChatCompletionRequest,
  ChatMessage,
  ChatSessionSummary,
  ChatStreamEvent,
  ChatUsage,
  ExecutionCapabilities,
  ExecutionLanguage,
  HarnessProfile,
  HarnessSessionSummary,
  McpServerProfile,
  PersistedChatMessage,
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
import { useWorkbenchDrafts } from "../state/WorkbenchDraftContext";
import { useWorkspace } from "../state/WorkspaceContext";
import { AgentsPage } from "./AgentsPage";

type SessionView = "chat" | "terminal" | "missions" | "activity" | "workspace" | "notes";
type MessageState = "complete" | "streaming" | "waiting_approval" | "error" | "cancelled";

interface ToolLifecycleCard {
  assistantId: string;
  toolCallId: string;
  capability: string;
  status: string;
  summary?: string;
  evidenceIds: string[];
}

interface PendingChatResponse {
  turnId: string;
  assistantId: string;
  userId: string;
  request: ChatCompletionRequest;
  approval: Record<string, unknown>;
}

const ContainerTerminalPanel = lazy(() => import("../components/ContainerTerminalPanel").then((module) => ({ default: module.ContainerTerminalPanel })));

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
  const initialView = requestedView === "chat" || requestedView === "terminal" || requestedView === "missions" || requestedView === "activity" || requestedView === "workspace" || requestedView === "notes"
    ? requestedView
    : requestedView === "executions" ? "activity"
      : requestedView === "files" ? "workspace"
        : localStorage.getItem("nebula.workbench.view") as SessionView | null;
  const [view, setViewState] = useState<SessionView>(initialView === "chat" || initialView === "missions" || initialView === "activity" || initialView === "workspace" || initialView === "notes" ? initialView : "terminal");
  const setView = (next: SessionView) => {
    setViewState(next);
    localStorage.setItem("nebula.workbench.view", next);
    const params = new URLSearchParams(searchParams);
    params.set("view", next);
    setSearchParams(params, { replace: true });
  };
  const [mobileListOpen, setMobileListOpen] = useState(false);
  const [conversationPanelExpanded, setConversationPanelExpanded] = useState(
    () => localStorage.getItem("nebula.conversations.expanded") === "true",
  );
  const {
    api,
    activeOperator,
    approvals,
    assets,
    coreState,
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
  } = useWorkspace();
  const [executionCapabilities, setExecutionCapabilities] = useState<ExecutionCapabilities>();
  const [runCandidate, setRunCandidate] = useState<FencedRunCandidate>();
  const [executionRefresh, setExecutionRefresh] = useState(0);
  const [sessions, setSessions] = useState<ChatSessionSummary[]>([]);
  const [deletingSessionId, setDeletingSessionId] = useState<string>();
  const [renamingSessionId, setRenamingSessionId] = useState<string>();
  const [renameDraft, setRenameDraft] = useState("");
  const [renameError, setRenameError] = useState<string>();
  const [sessionId, setSessionId] = useState("");
  const [providerId, setProviderId] = useState("");
  const [runtimeKind, setRuntimeKind] = useState<"provider" | "harness">("provider");
  const [harnesses, setHarnesses] = useState<HarnessProfile[]>([]);
  const [harnessSessions, setHarnessSessions] = useState<HarnessSessionSummary[]>([]);
  const [mcpServers, setMcpServers] = useState<McpServerProfile[]>([]);
  const [harnessId, setHarnessId] = useState("");
  const [harnessSessionId, setHarnessSessionId] = useState("");
  const [selectedMcpIds, setSelectedMcpIds] = useState<string[]>([]);
  const [model, setModel] = useState("");
  const [includeKnowledge, setIncludeKnowledge] = useState(true);
  const [assignedToolCount, setAssignedToolCount] = useState(0);
  const [toolRuntimeReason, setToolRuntimeReason] = useState<string>();
  const [toolCards, setToolCards] = useState<ToolLifecycleCard[]>([]);
  const [pendingResponse, setPendingResponse] = useState<PendingChatResponse>();
  const [messages, setMessages] = useState<ConversationMessage[]>([]);
  const [draft, setDraft] = useState("");
  const [sending, setSending] = useState(false);
  const [loadingHistory, setLoadingHistory] = useState(false);
  const [chatError, setChatError] = useState<string>();
  const [discoveringProviderId, setDiscoveringProviderId] = useState<string>();
  const abortRef = useRef<AbortController | undefined>(undefined);
  const composerRef = useRef<HTMLTextAreaElement>(null);
  const lastModelDiscoveryProviderIdRef = useRef<string | undefined>(undefined);
  const attemptedToolVerificationRef = useRef(new Set<string>());
  const scrollRef = useRef<HTMLDivElement>(null);
  const enabledProviders = useMemo(() => providers.filter((provider) => provider.enabled), [providers]);
  const selectedProvider = enabledProviders.find((provider) => provider.id === providerId);
  const selectedHarness = harnesses.find((harness) => harness.id === harnessId);
  const providerIsLocal = selectedProvider?.kind === "local" || selectedProvider?.privacy === "local_only";
  const harnessIsLocal = selectedHarness?.localOnly === true;
  const runtimePermitsKnowledge = runtimeKind === "harness"
    ? harnessIsLocal || selectedHarness?.permitsSensitiveData === true
    : providerIsLocal || selectedProvider?.permitsSensitiveData === true;
  const canUseKnowledge = knowledgeSources.length > 0 && runtimePermitsKnowledge;
  const modelVerification = providerModelVerification(selectedProvider, model);
  const modelVerified = modelVerification?.status === "verified";
  const toolboxAvailable = Boolean(modelVerified && assignedToolCount > 0 && !toolRuntimeReason);
  const toolboxReason = !modelVerified
    ? model ? `Tool calling is unverified for ${model}.` : "Select a model to verify tool calling."
    : toolRuntimeReason ?? (assignedToolCount === 0 ? "No Toolbox capabilities are assigned to this project." : undefined);
  const canUseTools = toolboxAvailable;
  const toolboxUnavailableReason = toolboxReason;

  useEffect(() => {
    if (runtimeKind !== "provider" || coreState !== "online" || view !== "chat" || !selectedProvider || !model.trim() || modelVerification) return;
    const key = `${selectedProvider.id}:${model.trim()}`;
    if (attemptedToolVerificationRef.current.has(key)) return;
    attemptedToolVerificationRef.current.add(key);
    void reverifyProvider(selectedProvider.id, model).catch(() => undefined);
  }, [coreState, model, modelVerification, reverifyProvider, runtimeKind, selectedProvider, view]);

  useEffect(() => {
    const next = requestedView === "executions" ? "activity" : requestedView === "files" ? "workspace" : requestedView;
    if (next === "chat" || next === "terminal" || next === "missions" || next === "activity" || next === "workspace" || next === "notes") {
      setViewState(next);
      localStorage.setItem("nebula.workbench.view", next);
    }
  }, [requestedView]);

  useEffect(() => {
    if (!assistantDraft) return;
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
      setAssignedToolCount(0);
      setToolRuntimeReason("Toolbox configuration is unavailable.");
      return;
    }
    let active = true;
    void selectProjectTools(api, engagement.id).then(({ tools, unavailableReason }) => {
      if (!active) return;
      setAssignedToolCount(tools.length);
      setToolRuntimeReason(unavailableReason);
    }).catch(() => {
      if (!active) return;
      setAssignedToolCount(0);
      setToolRuntimeReason("Toolbox configuration is unavailable.");
    });
    return () => { active = false; };
  }, [api, coreState, engagement]);

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
    }).catch(() => {
      if (active) { setHarnesses([]); setHarnessSessions([]); setMcpServers([]); }
    });
    return () => { active = false; };
  }, [api, coreState, engagement]);

  useEffect(() => {
    if (runtimeKind !== "harness" || sessionId) return;
    const attached = harnessSessions.find((item) => item.id === harnessSessionId);
    const profile = harnesses.find((item) => item.id === (attached?.harnessProfileId ?? harnessId));
    if (attached) setHarnessId(attached.harnessProfileId);
    setModel(attached?.model ?? profile?.defaultModel ?? "");
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
    abortRef.current?.abort();
    setSending(false);
    setSessions([]);
    setSessionId("");
    setHarnessSessionId("");
    setMessages([]);
    setDraft(assistantDraft ? "" : "");
    setChatError(undefined);
    setRunCandidate(undefined);
    setToolCards([]);
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
    setHarnessSessionId("");
    setMessages([]);
    setDraft("");
    setChatError(undefined);
    setView("chat");
    setMobileListOpen(false);
    setToolCards([]);
    setPendingResponse(undefined);
  };

  const deleteConversation = async (session: ChatSessionSummary) => {
    if (!api || deletingSessionId) return;
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
      if (sessionId === session.id) newConversation();
    } catch (error) {
      setChatError(error instanceof Error ? error.message : "Could not delete the conversation.");
    } finally {
      setDeletingSessionId(undefined);
    }
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
      const [history, pendingTurn] = await Promise.all([
        api.listChatMessages(id),
        api.getPendingChatTurn(id).catch(() => undefined),
      ]);
      setSessionId(id);
      setMessages(history.map(persistedMessage));
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
          capability: "Toolbox capability",
          status: pendingTurn.status === "waiting_approval" ? "waiting_approval" : "running",
          evidenceIds: [],
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
            setChatError(error instanceof Error ? error.message : "Could not restore the pending response.");
          }).finally(() => setSending(false));
        }
      } else {
        setPendingResponse(undefined);
        setToolCards([]);
      }
      setView("chat");
      setMobileListOpen(false);
    } catch (error) {
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
    try {
      const [page, history] = await Promise.all([
        api.listChatSessions(engagement.id),
        api.listChatMessages(id),
      ]);
      const ordered = page.items.sort((left, right) => right.updatedAt.localeCompare(left.updatedAt));
      const summary = ordered.find((session) => session.id === id);
      setSessions(ordered);
      setSessionId(id);
      setMessages(history.map(persistedMessage));
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
    }
    if ((streamEvent.type === "delta" || streamEvent.type === "message_delta") && streamEvent.delta) {
      setMessages((current) => current.map((message) => message.id === assistantId
        ? { ...message, content: message.content + streamEvent.delta }
        : message));
    }
    if (streamEvent.type === "tool_started") {
      setToolCards((current) => [...current.filter((item) => item.toolCallId !== streamEvent.toolCallId), {
        assistantId,
        toolCallId: streamEvent.toolCallId,
        capability: streamEvent.capability,
        status: "running",
        evidenceIds: [],
      }]);
    }
    if (streamEvent.type === "tool_completed") {
      setToolCards((current) => current.map((item) => item.toolCallId === streamEvent.toolCallId
        ? { ...item, status: streamEvent.status, summary: streamEvent.summary, evidenceIds: streamEvent.evidenceIds }
        : item));
    }
    if (streamEvent.type === "approval_required") {
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
      setPendingResponse(undefined);
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
      : canUseTools;
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
    const controller = new AbortController();
    abortRef.current = controller;
    const initialSessionId = sessionId || undefined;
    let returnedSessionId = initialSessionId;
    const chatRequest: ChatCompletionRequest = {
      backend: runtimeKind,
      providerId: providerRuntime?.id,
      harnessProfileId: harnessRuntime?.id,
      harnessSessionId: !initialSessionId && harnessSessionId ? harnessSessionId : undefined,
      mcpServerIds: !initialSessionId && !harnessSessionId ? selectedMcpIds : [],
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
      toolsEnabled: wantsTools,
      allowCloudToolResults,
    };

    try {
      const response = await api.streamChat(chatRequest, (streamEvent) => {
        if (streamEvent.type === "started") returnedSessionId = streamEvent.sessionId ?? returnedSessionId;
        if (streamEvent.type === "done") returnedSessionId = streamEvent.sessionId ?? returnedSessionId;
        applyChatEvent(streamEvent, assistantId, userId, chatRequest);
      }, controller.signal);
      returnedSessionId = response?.sessionId ?? returnedSessionId;
      if (response && returnedSessionId) {
        await refreshSessions(returnedSessionId);
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
      setChatError(error instanceof Error ? error.message : "Could not resume the response.");
    } finally {
      setSending(false);
    }
  };

  const onComposerKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      event.currentTarget.form?.requestSubmit();
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
      setChatError(error instanceof Error ? error.message : "Could not continue this chat as a mission.");
    }
  };

  const runtimeReady = runtimeKind === "provider" ? Boolean(selectedProvider) : Boolean(selectedHarness);
  const canSend = Boolean(api && coreState === "online" && engagement && runtimeReady && model.trim() && draft.trim() && !sending);
  const runnableLanguages = useMemo(() => new Set<ExecutionLanguage>(
    executionCapabilities?.runtimes
      .filter((runtime) => runtime.offline && runtime.scopedNetwork)
      .map((runtime) => runtime.language) ?? [],
  ), [executionCapabilities]);

  return (
    <div className="page sessions-page">
      <PageHeader
        title="Workbench"
        description="Start in Terminal, ask the assistant, or open your project files."
        actions={view === "chat" ? <button className="button primary" type="button" disabled={!engagement} title={!engagement ? "Create or select a project before starting chat" : undefined} onClick={newConversation}><Plus size={16} /> New chat</button> : view === "missions" ? <NewMissionButton /> : undefined}
      />

      <div className="session-toolbar">
        <div className="session-tabs" role="tablist" aria-label="Workbench views">
          <button type="button" role="tab" aria-selected={view === "terminal"} onClick={() => setView("terminal")}><SquareTerminal size={16} /> Terminal</button>
          <button type="button" role="tab" aria-label="Analyst chat" aria-selected={view === "chat"} onClick={() => setView("chat")}><MessageSquare size={16} /> Assistant</button>
          <button type="button" role="tab" aria-label="Workspace files" aria-selected={view === "workspace"} onClick={() => setView("workspace")}><FolderOpen size={16} /> Files</button>
          <button type="button" role="tab" aria-label="Project notes" aria-selected={view === "notes"} onClick={() => setView("notes")}><NotebookPen size={16} /> Notes</button>
          <button type="button" role="tab" aria-label="Autonomous missions" aria-selected={view === "missions"} onClick={() => setView("missions")}><Bot size={16} /> Missions</button>
          <button type="button" role="tab" aria-label="Activity history" aria-selected={view === "activity"} onClick={() => setView("activity")}><FileClock size={16} /> Activity</button>
        </div>
        {view === "chat" && <button className="session-mobile-list" type="button" aria-pressed={mobileListOpen} onClick={() => setMobileListOpen((value) => !value)}><MessageSquare size={15} /> {mobileListOpen ? "Current chat" : "Conversations"}</button>}
        <div className="session-scope"><ShieldCheck size={15} /> Human controlled · {engagement?.name ?? "no project"}</div>
      </div>

      <div className={`session-layout ${view}${mobileListOpen ? " mobile-list-open" : ""}${view === "chat" && conversationPanelExpanded ? " conversation-panel-expanded" : ""}`}>
        {view === "chat" && <aside className="session-list" aria-label="Conversations">
          <header><div><span>Conversations</span><strong>{sessions.length} saved</strong></div><div className="session-list-header-actions"><button className="icon-button subtle" type="button" aria-label={conversationPanelExpanded ? "Collapse conversations panel" : "Expand conversations panel"} aria-pressed={conversationPanelExpanded} onClick={toggleConversationPanel}>{conversationPanelExpanded ? <PanelLeftClose size={16} /> : <PanelLeftOpen size={16} />}</button><button className="icon-button subtle" type="button" aria-label="New conversation" disabled={!engagement} onClick={newConversation}><Plus size={16} /></button></div></header>
          <nav>
            <button className={!sessionId ? "active" : undefined} type="button" onClick={newConversation}><MessageSquare size={16} /><span><strong>New conversation</strong><small>{runtimeKind === "harness" ? selectedHarness?.name ?? "Choose a harness" : selectedProvider?.name ?? "Choose a provider"}</small></span></button>
            {sessions.map((session) => <div className={`session-list-item${session.id === sessionId ? " active" : ""}${renamingSessionId === session.id ? " renaming" : ""}`} key={session.id}>{renamingSessionId === session.id ? <form className="session-rename-form" onSubmit={(event) => void renameConversation(event, session)}><label className="sr-only" htmlFor={`conversation-name-${session.id}`}>Conversation name</label><input id={`conversation-name-${session.id}`} aria-label={`Rename conversation ${session.title}`} autoFocus maxLength={300} value={renameDraft} onKeyDown={(event) => { if (event.key === "Escape") cancelRenamingConversation(); }} onChange={(event) => setRenameDraft(event.target.value)} /><button className="icon-button subtle" type="submit" aria-label="Save conversation name" disabled={!renameDraft.trim()}><Check size={14} /></button><button className="icon-button subtle" type="button" aria-label={`Cancel renaming ${session.title}`} onClick={cancelRenamingConversation}><X size={14} /></button></form> : <><button className="session-select" type="button" onClick={() => void selectSession(session.id)}><MessageSquare size={16} /><span><strong title={session.title}>{session.title}</strong><small title={session.model || undefined}>{session.model || "Saved conversation"}</small></span></button><button className="icon-button subtle" type="button" aria-label={`Rename conversation ${session.title}`} disabled={deletingSessionId === session.id || (session.id === sessionId && (sending || Boolean(pendingResponse)))} title={session.id === sessionId && (sending || pendingResponse) ? "Wait for the active response to finish" : `Rename ${session.title}`} onClick={() => startRenamingConversation(session)}><Pencil size={14} /></button><button className="icon-button subtle" type="button" aria-label={`Delete conversation ${session.title}`} disabled={deletingSessionId === session.id || (session.id === sessionId && (sending || Boolean(pendingResponse)))} title={session.id === sessionId && (sending || pendingResponse) ? "Wait for the active response to finish" : `Delete ${session.title}`} onClick={() => void deleteConversation(session)}>{deletingSessionId === session.id ? <LoaderCircle className="spin" size={14} /> : <Trash2 size={14} />}</button></>}</div>)}
            {renameError && <p className="session-list-error" role="alert">{renameError}</p>}
          </nav>
        </aside>}
        <section className="session-workspace">
          {api && engagement && <div className="persistent-terminal" hidden={view !== "terminal"}>
            <Suspense fallback={<div className="empty-state compact"><LoaderCircle className="spin" size={20} /><strong>Loading Terminal…</strong></div>}><ContainerTerminalPanel active={view === "terminal"} api={api} capturedBy={activeOperator?.id} engagementId={engagement.id} engagementName={engagement.name} onUploadEvidence={uploadEvidence} setupTerminalStatus={setupStatus?.terminal.status} setupTerminalDetail={setupStatus?.terminal.detail} /></Suspense>
          </div>}
          {view === "terminal" && (!api || !engagement) ? (
            <div className="empty-state"><FolderOpen size={24} /><strong>Preparing your project</strong><p>Terminal becomes available as soon as Nebula finishes creating or loading a project.</p></div>
          ) : view === "terminal" ? null : view === "missions" && api && engagement ? (
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
              initialDraft={noteDraft}
              onInitialDraftConsumed={clearNoteDraft}
              onAskNebula={requestNebulaDraft}
            />
          ) : view !== "chat" ? (
            <div className="empty-state"><FolderOpen size={24} /><strong>Select a project</strong><p>Terminal, execution history, and workspace files are project-scoped.</p></div>
          ) : (
            <div className="chat-panel">
              <details className="chat-settings" open={!runtimeReady}>
                <summary>Assistant settings</summary>
                <div className="chat-context-bar">
                <label><span>Runtime</span><select aria-label="Chat runtime" value={runtimeKind} disabled={sending || Boolean(sessionId)} onChange={(event) => { const next = event.target.value as "provider" | "harness"; setRuntimeKind(next); setHarnessSessionId(""); setSelectedMcpIds([]); if (next === "provider") selectProvider(providerId || enabledProviders[0]?.id || ""); else { setModel(selectedHarness?.defaultModel ?? ""); } }}><option value="provider">Provider</option><option value="harness">Agent harness</option></select></label>
                {runtimeKind === "provider" ? <label><span>Provider</span><select aria-label="Chat provider" value={providerId} disabled={sending || Boolean(sessionId)} onChange={(event) => selectProvider(event.target.value)}><option value="">Select provider</option>{enabledProviders.map((provider) => <option value={provider.id} key={provider.id}>{provider.name} · {provider.state}</option>)}</select></label> : <><label><span>Harness</span><select aria-label="Chat harness" value={harnessId} disabled={sending || Boolean(sessionId) || Boolean(harnessSessionId)} onChange={(event) => setHarnessId(event.target.value)}><option value="">Select harness</option>{harnesses.map((harness) => <option value={harness.id} key={harness.id}>{harness.name}</option>)}</select></label><label><span>Session</span><select aria-label="Chat harness session" value={harnessSessionId} disabled={sending || Boolean(sessionId)} onChange={(event) => setHarnessSessionId(event.target.value)}><option value="">New session</option>{harnessSessions.filter((item) => item.harnessProfileId === harnessId || item.id === harnessSessionId).map((item) => <option value={item.id} key={item.id}>{item.model} · {item.status}</option>)}</select></label></>}
                {runtimeKind === "provider" ? <label title={selectedProvider?.message}><span>Model</span><select aria-label="Chat model" aria-busy={modelDiscoveryInProgress} value={model} disabled={sending || Boolean(sessionId) || modelDiscoveryInProgress || !selectedProvider?.models.length} onChange={(event) => setModel(event.target.value)}><option value="">{modelPlaceholder}</option>{selectedModelIsUnavailable && <option value={model}>{model} · saved model</option>}{selectedProvider?.models.map((item) => <option value={item} key={item}>{item}</option>)}</select></label> : <label><span>Model</span><input aria-label="Chat harness model" value={model} disabled={sending || Boolean(sessionId) || Boolean(harnessSessionId)} placeholder="Exact harness model" onChange={(event) => setModel(event.target.value)} /></label>}
                {runtimeKind === "provider" ? <><label className="chat-knowledge-toggle"><input type="checkbox" checked={includeKnowledge && canUseKnowledge} disabled={!canUseKnowledge || sending} onChange={(event) => setIncludeKnowledge(event.target.checked)} /><span>Use knowledge<small>{knowledgeSources.length ? runtimePermitsKnowledge ? `${knowledgeSources.length} source${knowledgeSources.length === 1 ? "" : "s"}` : "Profile is text-only" : "No sources loaded"}</small></span></label><div className="chat-knowledge-toggle" role="status" title={toolboxUnavailableReason}><ShieldCheck size={15} /><span>Toolbox automatic<small>{canUseTools ? `${assignedToolCount} assigned ${assignedToolCount === 1 ? "capability" : "capabilities"} enabled` : toolboxUnavailableReason}</small></span></div></> : <><label className="chat-knowledge-toggle"><input type="checkbox" checked={includeKnowledge && canUseKnowledge} disabled={!canUseKnowledge || sending} onChange={(event) => setIncludeKnowledge(event.target.checked)} /><span>Use knowledge<small>{knowledgeSources.length ? runtimePermitsKnowledge ? `${knowledgeSources.length} bounded source${knowledgeSources.length === 1 ? "" : "s"}` : "Harness is text-only" : "No sources loaded"}</small></span></label><div className="chat-harness-mcp"><span>MCP servers</span>{harnessSessionId ? <small>Frozen in selected session</small> : mcpServers.length ? mcpServers.map((server) => <label className="chat-knowledge-toggle" key={server.id}><input type="checkbox" checked={selectedMcpIds.includes(server.id)} disabled={sending || Boolean(sessionId)} onChange={(event) => setSelectedMcpIds((current) => event.target.checked ? [...current, server.id] : current.filter((id) => id !== server.id))} /><span>{server.name}<small>{server.tools.length} tools · {server.defaultApproval.replace("_", " ")}</small></span></label>) : <small>No enabled MCP profiles</small>}</div></>}
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
                    <div><header><strong>{message.role === "user" ? "You" : "Nebula assistant"}</strong><span>{timeLabel(message.createdAt)}</span>{message.usage && <span>{message.usage.totalTokens} tokens</span>}</header>{message.content && (message.role === "assistant" && message.state === "complete" ? <AssistantMarkdown content={message.content} messageId={message.id} durable={message.durable} runnableLanguages={runnableLanguages} onRun={setRunCandidate} /> : <p>{message.content}</p>)}{toolCards.filter((card) => card.assistantId === message.id).map((card) => <div className="chat-tool-card" key={card.toolCallId}><strong>{card.capability}</strong><span>{card.status.replaceAll("_", " ")}</span>{card.summary && <small>{card.summary}</small>}{card.evidenceIds.map((id) => <Link to={`/evidence?id=${encodeURIComponent(id)}`} key={id}>Evidence {id.slice(0, 8)}</Link>)}</div>)}{message.state === "streaming" && !message.content && <div className="chat-thinking"><span /><span /><span /> Waiting for provider</div>}{message.state === "waiting_approval" && pendingResponse?.assistantId === message.id && <div className="chat-approval-card"><strong>Approval required</strong><pre>{JSON.stringify(pendingResponse.approval.exact_request ?? {}, null, 2)}</pre><div><button className="button secondary" type="button" onClick={() => void decideInlineApproval("reject")}>Reject</button><button className="button secondary" type="button" onClick={() => void decideInlineApproval("stop")}>Stop response</button><button className="button primary" type="button" onClick={() => void decideInlineApproval("approve")}>Approve</button></div></div>}{message.detail && <p className="chat-message-error" role="alert">{message.detail}</p>}{message.citations.map((citation) => <Link className="citation-chip" to={`/knowledge?source=${encodeURIComponent(citation.sourceId)}`} title={citation.excerpt} key={`${citation.sourceId}-${citation.chunkId}`}><Braces size={13} /> {citation.name}{citation.page ? ` · p. ${citation.page}` : ""}</Link>)}</div>
                  </article>
                )) : <div className="empty-state compact"><MessageSquare size={23} /><strong>Start an analyst conversation</strong><p>New chats can use project-assigned Toolbox capabilities when the exact model is verified.</p></div>}
              </div>
              {pendingResponse && pendingResponse.request.backend !== "harness" && <div className="chat-inline-approval-actions"><button className="button secondary" type="button" onClick={() => void decideInlineApproval("edit")}>Edit pending request</button></div>}
              {chatError && <p className="chat-error" role="alert">{chatError}</p>}
              <form className="chat-composer" onSubmit={(event) => void submit(event)}>
                {assistantDraft && <div className="chat-context-attachment" role="group" aria-label="Selected context attachment">
                  <div><strong>{assistantDraft.source.label}</strong><small>{assistantDraft.text.length.toLocaleString()} characters{assistantDraft.truncated ? " · truncated to the first 20,000" : ""}</small></div>
                  <p>{assistantDraft.text.slice(0, 180)}{assistantDraft.text.length > 180 ? "…" : ""}</p>
                  <button className="icon-button subtle" type="button" aria-label="Remove selected context" onClick={clearAssistantDraft}><X size={14} /></button>
                </div>}
                <label className="sr-only" htmlFor="analyst-message">Message the analyst assistant</label>
                <textarea ref={composerRef} id="analyst-message" value={draft} disabled={!engagement || !runtimeReady || loadingHistory} placeholder={!engagement ? "Create or select a project to chat…" : runtimeReady ? "Ask about this project…" : "Add a model or harness in Settings…"} rows={3} onKeyDown={onComposerKeyDown} onChange={(event) => setDraft(event.target.value)} />
                <footer><span>{runtimeKind === "harness" ? `${harnessSessionId ? "Resumed" : "New"} harness session · ${selectedMcpIds.length || harnessSessions.find((item) => item.id === harnessSessionId)?.mcpServerIds.length || 0} MCP` : canUseTools ? `Toolbox automatic · ${assignedToolCount} assigned` : includeKnowledge && canUseKnowledge ? providerIsLocal ? "Cited retrieval stays local" : "Cloud excerpts require confirmation" : "Text-only chat"}</span>{sending ? <button className="button secondary square" type="button" aria-label="Stop response" onClick={() => { if (pendingResponse && pendingResponse.request.backend !== "harness") void api?.cancelChatTurn(pendingResponse.turnId); abortRef.current?.abort(); }}><Square size={15} /></button> : <button className="button primary square" type="submit" disabled={!canSend} aria-label="Send message"><Send size={16} /></button>}</footer>
              </form>
            </div>
          )}
        </section>

        {view === "chat" && <aside className="session-inspector" aria-label="Session inspector">
          <header><div><span>Context</span><strong>Session details</strong></div></header>
          <dl><div><dt>Active operator</dt><dd>{activeOperator?.displayName ?? "No active operator"}</dd></div><div><dt>Conversation</dt><dd>{sessionId ? sessions.find((session) => session.id === sessionId)?.title ?? "Saved chat" : "Unsaved chat"}</dd></div><div><dt>Runtime</dt><dd>{runtimeKind === "harness" ? selectedHarness?.name ?? "Harness" : selectedProvider?.name ?? "Not selected"}</dd></div><div><dt>Code Run</dt><dd><span className={`status-dot ${executionCapabilities?.ready ? "healthy" : "unavailable"}`} /> {executionCapabilities?.ready ? "Review available" : "Unavailable"}</dd></div></dl>
          {sessionId && sessions.find((session) => session.id === sessionId)?.backend === "harness" && <button className="button primary full" type="button" disabled={sending} onClick={() => void continueAsMission()}><Bot size={15} /> Continue as mission</button>}
          <section><h3>Knowledge boundary</h3><div className="scope-chip-list"><span>{knowledgeSources.length} source{knowledgeSources.length === 1 ? "" : "s"}</span><span>{providerIsLocal ? "Local retrieval" : includeKnowledge && canUseKnowledge ? "Confirm each cloud request" : "Text only"}</span></div></section>
          <section><h3>Execution boundary</h3><div className="empty-state mini"><Braces size={19} /><p>{canUseTools ? `${assignedToolCount} assigned capabilities run sequentially through the scoped broker; approvals pause this response.` : toolboxUnavailableReason ?? "Toolbox is unavailable for this session."}</p></div></section>
          <section><h3>Session evidence</h3><div className="empty-state mini"><Braces size={19} /><p>Citations identify canonical ingested chunks and transcript messages.</p></div></section>
        </aside>}
      </div>
      {runCandidate && api && engagement && <ExecutionReviewDialog api={api} engagementId={engagement.id} candidate={runCandidate} capabilities={executionCapabilities} onClose={() => setRunCandidate(undefined)} onStarted={() => { setExecutionRefresh((value) => value + 1); setView("activity"); }} />}
    </div>
  );
}
