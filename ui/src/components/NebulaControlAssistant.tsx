import { memo, useCallback, useEffect, useRef, useState, type ChangeEvent, type ClipboardEvent as ReactClipboardEvent, type CSSProperties, type ReactNode, type RefObject } from "react";
import { ChevronDown, GripHorizontal, Image, Minimize2, Send, Settings2, Square, Upload, X } from "lucide-react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { createPortal } from "react-dom";
import { defaultModelRuntime } from "../api/runtimeDefaults";
import { ApiError } from "../api/client";
import { ACCEPTED_LIBRARY_FILES, encodeLibraryFile, MAX_LIBRARY_ITEM_BYTES, startLibraryArtifactDownload } from "../api/libraryFiles";
import { isNebulaShell } from "../hooks/useCompactLayout";
import { startVisiblePoll } from "../api/visiblePoll";
import { logDiagnostic } from "../diagnostics";
import type { ToolSharingRuntime } from "../api/toolSharingConsent";
import { useWorkspace } from "../state/WorkspaceContext";
import { useOptionalWorkbenchDrafts } from "../state/WorkbenchDraftContext";
import { createHashedSelectionAttachment } from "./selection/selectionActions";
import { THEME_OPTIONS, useTheme, type ThemePreference } from "../state/ThemeContext";
import { readStorage, writeStorage } from "../state/browserStorage";
import { settingCatalog, type SettingCatalogEntry } from "../settingsCatalog";
import { assistantDestinations, assistantDestinationPath, destinationToOpen, type AssistantDestination } from "../assistantDestinations";
import { projectSurface, resourcePath } from "../resourceRoutes";
import { sha256BlobHex, sha256Hex } from "../sha256";
import { startChatTranscriptDownload } from "../pages/chatTranscriptExport";
import { REASONING_EFFORTS, type ButlerVoiceSettings, type ChatAssistantConfig, type ChatContentBlock, type ChatMessage, type ChatTurn, type ObservationSummary, type VoicePauseSeconds, type WritingTransformResponse } from "../api/types";
import { assistantTurnOptions, assistantWorkReceipt, headlessSessionChange, isSettingNavigationRequest, isThemeChangeCommand, projectModeChange, projectScopeChange, settingClientAction, settingNavigationCandidates, settingToOpen, themeChange, type ProjectMode, type SettingClientAction } from "./nebulaAssistantActions";
import { saveAssistantProjectScope } from "./assistantSettingsMutations";
import { useConfirmation } from "./DialogSystem";
import { NebulaAssistantLauncherContext } from "./NebulaAssistantLauncher";
import { MicrophoneIcon, NebulaVoice } from "./NebulaVoice";
import { ButlerMemory } from "./ButlerMemory";
import { ButlerOversight } from "./ButlerOversight";
import { AuthenticatedChatImage } from "./AuthenticatedChatImage";
import { HostFolderPicker } from "./HostFolderPicker";
import { applyButlerTerminalStop } from "./butlerTerminalStop";
import { AIWritingDialog } from "./AIWritingDialog";
import { ButlerEvidenceUpload } from "./ButlerEvidenceUpload";
import { readButlerReportTransform, settleButlerReportTransform, type ButlerReportTransformAction } from "./butlerReportTransform";
import { useButlerOverview } from "./useButlerOverview";
import { beginButlerFeedback, feedbackFromButlerEvent, feedbackFromPendingTurn, formatButlerDuration, type ButlerTurnFeedback } from "./butlerTurnFeedback";
import styles from "./NebulaControlAssistant.module.css";

function readActionOutcomes(key: string): Record<string, string> {
  try {
    const value: unknown = JSON.parse(readStorage(key) ?? "{}");
    if (!value || typeof value !== "object" || Array.isArray(value)) return {};
    return Object.fromEntries(Object.entries(value).filter(([id, message]) => id.length > 0 && typeof message === "string"));
  } catch (caught) {
    void logDiagnostic({ level: "warning", eventCode: "interface.nebula_assistant.action_outcomes_invalid", message: "The saved assistant action outcomes could not be read.", outcome: "failure", stage: "history", retryable: false, exception: caught });
    return {};
  }
}

function projectNameKey(name: string): string {
  return name.normalize("NFKC").trim().replace(/\s+/g, " ").toLowerCase();
}

const ButlerTranscript = memo(function ButlerTranscript({ messages, api, projectId, sessionId, transcriptRef, onScroll, onOpenSetting, onNavigate, onHide }: {
  messages: ChatMessage[];
  api: ReturnType<typeof useWorkspace>["api"];
  projectId?: string;
  sessionId?: string;
  transcriptRef: RefObject<HTMLDivElement | null>;
  onScroll: (element: HTMLDivElement) => void;
  onOpenSetting: (entry: SettingCatalogEntry) => void;
  onNavigate: (path: string) => void;
  onHide: (hidden: boolean) => void;
}) {
  return <div ref={transcriptRef} className={styles.transcript} role="log" aria-label="Butler conversation" tabIndex={0} data-selection-source-kind="conversation" data-selection-source-id={sessionId} data-selection-source-label="Butler conversation" onScroll={(event) => onScroll(event.currentTarget)}>{messages.map((message, index) => <div key={`${index}-${message.id ?? ""}`} className={message.role === "user" ? styles.question : styles.answer}><ReactMarkdown remarkPlugins={[remarkGfm]} components={{ table: ({ children, ...props }) => <div className={styles.tableScroll} role="region" aria-label="Butler response table" tabIndex={0}><table {...props}>{children}</table></div>, a: ({ href, children, ...props }) => {
    const setting = settingCatalog.find((entry) => href === `/settings#${entry.target}`);
    const destination = assistantDestinations.find((entry) => href === assistantDestinationPath(entry, projectId));
    const workLink = projectId && href && /^\/projects\/[^/]+\/workbench\?view=(?:chat&session|missions&mission)=/.test(href)
      && href.startsWith(`/projects/${encodeURIComponent(projectId)}/workbench?`) ? href : undefined;
    return setting
      ? <a href={href} onClick={(event) => { event.preventDefault(); onOpenSetting(setting); onHide(setting.target !== "butler-oversight"); }}>{children}</a>
      : destination ? <a href={href} onClick={(event) => { event.preventDefault(); onHide(true); onNavigate(assistantDestinationPath(destination, projectId) ?? "/projects"); }}>{children}</a>
      : workLink ? <a href={workLink} onClick={(event) => { event.preventDefault(); onNavigate(workLink); onHide(true); }}>{children}</a>
      : <a href={href} {...props}>{children}</a>;
  } }}>{message.content}</ReactMarkdown>{api && message.contentBlocks?.filter((block) => block.type === "image").map((block, imageIndex) => <AuthenticatedChatImage key={`${block.artifactId ?? "image"}-${imageIndex}`} api={api} block={block} className={styles.transcriptImage} />)}</div>)}</div>;
});

export function NebulaControlAssistant({ children, onOpenSetting, onNavigate, onCloseSetting, onSavedSetting }: {
  children?: ReactNode;
  onOpenSetting: (entry: SettingCatalogEntry) => void;
  onNavigate: (path: string) => void;
  onCloseSetting?: () => void;
  onSavedSetting?: (entry: SettingCatalogEntry, status: string, mode?: "docker" | "host") => void;
}) {
  const { api, coreState, engagement, providers, harnesses, createEngagement, renameEngagementAtRevision, refreshProjectLifecycle, setEngagementArchived, deleteArchivedEngagement, createReport, updateReport, adoptConfirmedReport, deleteReport, ingestLibraryItem, retryResource } = useWorkspace();
  const nativeShell = isNebulaShell();
  const context = useOptionalWorkbenchDrafts();
  const butlerDrafts = context?.butlerDrafts ?? [];
  const { preference, setPreference } = useTheme();
  const confirm = useConfirmation();
  const [open, setOpen] = useState(false);
  const [hidden, setHidden] = useState(false);
  const [view, setView] = useState<"chat" | "work" | "more">("chat");
  const [query, setQuery] = useState("");
  const [destinationQuery, setDestinationQuery] = useState("");
  const [draft, setDraft] = useState("");
  const [pendingImages, setPendingImages] = useState<ChatContentBlock[]>([]);
  const [uploadingImage, setUploadingImage] = useState(false);
  const [pendingLibraryAdd, setPendingLibraryAdd] = useState<Extract<SettingClientAction, {kind: "library_add"}>>();
  const [uploadingLibrary, setUploadingLibrary] = useState(false);
  const [pendingEvidenceUpload, setPendingEvidenceUpload] = useState<(Extract<SettingClientAction, {kind: "evidence_upload"}> & {sourceMessageId?: string})>();
  const [pendingWorkspaceUpload, setPendingWorkspaceUpload] = useState<Extract<SettingClientAction, {kind: "workspace_upload"}>>();
  const [uploadingWorkspace, setUploadingWorkspace] = useState(false);
  const [pendingNotebookIndex, setPendingNotebookIndex] = useState<(Extract<SettingClientAction, {kind: "notebook_index_connect" | "notebook_index_remove"}> & {sourceMessageId?: string})>();
  const [connectingNotebookIndex, setConnectingNotebookIndex] = useState(false);
  const [pendingLinkedProject, setPendingLinkedProject] = useState<(Extract<SettingClientAction, {kind: "project_create_linked"}> & {sourceMessageId?: string})>();
  const [creatingLinkedProject, setCreatingLinkedProject] = useState(false);
  const [pendingReportTransform, setPendingReportTransform] = useState<(ButlerReportTransformAction & {note: ObservationSummary; sourceMessageId?: string; saveAttempted?: boolean})>();
  const [pendingProjectRelink, setPendingProjectRelink] = useState<(Extract<SettingClientAction, {kind: "project_relink"}> & {sourceMessageId?: string})>();
  const [relinkingProject, setRelinkingProject] = useState(false);
  const [imageHelp, setImageHelp] = useState(false);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [hasNewerMessages, setHasNewerMessages] = useState(false);
  const [memoryRefreshRevision, setMemoryRefreshRevision] = useState(0);
  const [suggestions, setSuggestions] = useState<SettingCatalogEntry[]>([]);
  const [sessionId, setSessionId] = useState<string>();
  const [sessionRuntime, setSessionRuntime] = useState<{ kind: "provider" | "harness"; id: string; model: string }>();
  const [sessionConfig, setSessionConfig] = useState<ChatAssistantConfig>();
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [oversightOpenRevision, setOversightOpenRevision] = useState(0);
  const [settingsBusy, setSettingsBusy] = useState(false);
  const [settingsError, setSettingsError] = useState("");
  const [settingsStatus, setSettingsStatus] = useState("");
  const [settingsRevision, setSettingsRevision] = useState<number>();
  const [voiceSettings, setVoiceSettings] = useState<ButlerVoiceSettings>();
  const [voiceSettingsBusy, setVoiceSettingsBusy] = useState(false);
  const [voiceSettingsError, setVoiceSettingsError] = useState("");
  const [voiceSettingsStatus, setVoiceSettingsStatus] = useState("");
  const [effortDraft, setEffortDraft] = useState("");
  const [cyberDraft, setCyberDraft] = useState<ChatAssistantConfig["harnessCyberAccessProgram"]>("automatic");
  const [busy, setBusy] = useState(false);
  const [loadingHistory, setLoadingHistory] = useState(false);
  const [pendingTurn, setPendingTurn] = useState<ChatTurn>();
  const [pendingCheckedSessionId, setPendingCheckedSessionId] = useState<string>();
  const [pendingStatusError, setPendingStatusError] = useState("");
  const [awaitingTurnResolution, setAwaitingTurnResolution] = useState(false);
  const [turnFeedback, setTurnFeedback] = useState<ButlerTurnFeedback>();
  const [clock, setClock] = useState(() => Date.now());
  const [error, setError] = useState("");
  const [voiceStart, setVoiceStart] = useState<{ microphone?: Promise<MediaStream> }>();
  const [position, setPosition] = useState(() => ({ x: Math.max(12, window.innerWidth - 580), y: 72 }));
  const drag = useRef<{ x: number; y: number; left: number; top: number } | undefined>(undefined);
  const abort = useRef<AbortController | undefined>(undefined);
  const activeTurn = useRef<{ turnId?: string; harnessTurnId?: string }>({});
  const settledTurnId = useRef<string | undefined>(undefined);
  const needsSavedTurnReload = useRef(false);
  const pendingPoll = useRef<() => void>(() => {});
  const historyRead = useRef(0);
  const input = useRef<HTMLTextAreaElement>(null);
  const imageInput = useRef<HTMLInputElement>(null);
  const libraryInput = useRef<HTMLInputElement>(null);
  const workspaceInput = useRef<HTMLInputElement>(null);
  const workspaceUploadAbort = useRef<AbortController | undefined>(undefined);
  const activeImageProject = useRef(engagement?.id);
  activeImageProject.current = engagement?.id;
  const panel = useRef<HTMLDivElement>(null);
  const body = useRef<HTMLDivElement>(null);
  const transcript = useRef<HTMLDivElement>(null);
  const lastScroll = useRef({ visible: false, messages: [] as ChatMessage[], attentionCount: 0, loadingHistory: false });
  const followsLatest = useRef(true);
  const catalogRef = useRef<HTMLDetailsElement>(null);
  const voiceSettingsRequest = useRef(0);
  const settingsProjectId = useRef(engagement?.id);
  const destinationCatalogRef = useRef<HTMLDetailsElement>(null);
  const runtime = sessionRuntime ?? defaultModelRuntime(providers, harnesses);
  const imageInputSupported = runtime?.kind === "provider"
    ? Boolean(providers.find((item) => item.id === runtime.id)?.capabilities?.includes("vision"))
    : Boolean(harnesses.find((item) => item.id === runtime?.id)?.modelOptions?.find((item) => item.model === runtime?.model)?.imageInput);
  const storageKey = engagement ? `nebula.control-assistant.session.${engagement.id}` : undefined;
  const actionOutcomesKey = storageKey ? `${storageKey}.action-outcomes` : undefined;
  const { overview: attentionOverview, refresh: refreshAttention } = useButlerOverview(engagement ? api : undefined, engagement?.id ?? "", coreState);
  const attentionCount = attentionOverview?.summary.openIncidents ?? 0;
  const workAttentionCount = attentionCount
    + (attentionOverview?.actions.filter((action) => action.status === "uncertain" || action.status === "held").length ?? 0)
    + (attentionOverview?.summary.recoveryAttention ?? 0);
  const activePendingTurn = pendingTurn?.id === settledTurnId.current ? undefined : pendingTurn;
  const activeBusy = busy || Boolean(activePendingTurn) || awaitingTurnResolution;
  const pendingReady = !sessionId || !api?.getPendingChatTurn || (pendingCheckedSessionId === sessionId && !pendingStatusError);

  const scrollSurface = useCallback(() => nativeShell || window.matchMedia("(max-width: 760px), (max-height: 500px) and (pointer: coarse)").matches
    ? body.current : transcript.current, [nativeShell]);
  const distanceFromLatest = useCallback((element: HTMLDivElement) => {
    if (element === body.current) {
      const lastMessage = transcript.current?.lastElementChild;
      return lastMessage ? lastMessage.getBoundingClientRect().bottom - element.getBoundingClientRect().bottom : 0;
    }
    return element.scrollHeight - element.scrollTop - element.clientHeight;
  }, []);
  const handleConversationScroll = useCallback((element: HTMLDivElement) => {
    if (view !== "chat") return;
    if (element !== scrollSurface()) return;
    const awayFromLatest = distanceFromLatest(element) > 24;
    followsLatest.current = !awayFromLatest;
    setHasNewerMessages(awayFromLatest);
  }, [distanceFromLatest, scrollSurface, view]);

  useEffect(() => {
    if (!activeBusy) return;
    const timer = window.setInterval(() => setClock(Date.now()), 1_000);
    return () => window.clearInterval(timer);
  }, [activeBusy]);

  const getSavedSessionForTurn = async () => {
    if (!api || !engagement || !runtime) throw new Error("Choose a project and a healthy model runtime to direct work.");
    const options = assistantTurnOptions(runtime, harnesses);
    const selectedProfile = runtime.kind === "provider"
      ? providers.find((item) => item.id === runtime.id)
      : harnesses.find((item) => item.id === runtime.id);
    const cloudConsent = Boolean(selectedProfile
      && !("local" in selectedProfile ? selectedProfile.local : selectedProfile.localOnly)
      && selectedProfile.permitsSensitiveData);
    const session = await api.getOrCreateControlAssistantSession({
      engagementId: engagement.id,
      backend: runtime.kind,
      providerId: runtime.kind === "provider" ? runtime.id : undefined,
      harnessProfileId: runtime.kind === "harness" ? runtime.id : undefined,
      model: options.model,
      harnessReasoningEffort: options.harnessReasoningEffort,
      harnessServiceTier: options.harnessServiceTier,
      allowCloudToolResults: cloudConsent,
    });
    if (session.engagementId !== engagement.id || !session.isControlAssistant) {
      throw new Error("Core returned a conversation outside this project's Butler. Retry after reconnecting.");
    }
    const config = typeof api.getChatAssistantConfig === "function"
      ? (await api.getChatAssistantConfig(session.id)).settings
      : session.assistantConfig;
    setSessionId(session.id);
    setSessionConfig(config);
    const runtimeId = session.backend === "provider" ? session.providerId : session.harnessProfileId;
    if (runtimeId && session.model) setSessionRuntime({ kind: session.backend, id: runtimeId, model: session.model });
    return { session, config };
  };

  useEffect(() => () => { abort.current?.abort(); workspaceUploadAbort.current?.abort(); }, [engagement?.id]);

  useEffect(() => {
    setSessionId(undefined);
    setSessionRuntime(undefined);
    setSessionConfig(undefined);
    if (settingsProjectId.current && engagement?.id && settingsProjectId.current !== engagement.id) setSettingsOpen(false);
    if (engagement?.id) settingsProjectId.current = engagement.id;
    setSettingsRevision(undefined);
    setSettingsError("");
    setSettingsStatus("");
    setMessages([]);
    setHasNewerMessages(false);
    followsLatest.current = true;
    lastScroll.current = { visible: false, messages: [], attentionCount: 0, loadingHistory: false };
    setPendingImages([]);
    setUploadingImage(false);
    setPendingLibraryAdd(undefined);
    setPendingEvidenceUpload(undefined);
    setUploadingLibrary(false);
    workspaceUploadAbort.current?.abort();
    setPendingWorkspaceUpload(undefined);
    setUploadingWorkspace(false);
    setPendingNotebookIndex(undefined);
    setConnectingNotebookIndex(false);
    setPendingReportTransform(undefined);
    setImageHelp(false);
    setPendingTurn(undefined);
    setPendingCheckedSessionId(undefined);
    setPendingStatusError("");
    setAwaitingTurnResolution(false);
    setTurnFeedback(undefined);
    activeTurn.current = {};
    settledTurnId.current = undefined;
    needsSavedTurnReload.current = false;
    historyRead.current += 1;
    setSuggestions([]);
    setError("");
    setLoadingHistory(false);
    if (!storageKey || !api) return;
    setLoadingHistory(true);
    let current = true;
    const request = historyRead.current;
    void api.getControlAssistantSession(engagement!.id).then((session) => {
      if (!current || !session || session.engagementId !== engagement?.id) return;
      localStorage.setItem(storageKey, session.id);
      setSessionId(session.id);
      setSessionConfig(session.assistantConfig);
      if (typeof api.getChatAssistantConfig === "function") {
        void api.getChatAssistantConfig(session.id).then((saved) => {
          if (current) { setSessionConfig(saved.settings); setSettingsRevision(saved.revision); }
        }).catch((caught: unknown) => {
          void logDiagnostic({ level: "warning", eventCode: "interface.nebula_assistant.settings_load_failed", message: "Saved assistant settings could not be loaded.", outcome: "failure", stage: "history", retryable: true, exception: caught });
        });
      }
      const runtimeId = session.backend === "provider" ? session.providerId : session.harnessProfileId;
      if (runtimeId && session.model) setSessionRuntime({ kind: session.backend, id: runtimeId, model: session.model });
      return api.listChatMessages(session.id);
    }).then((history) => {
      if (!current || request !== historyRead.current || !history) return;
      const outcomes = actionOutcomesKey ? readActionOutcomes(actionOutcomesKey) : {};
      setMessages(history.filter((item) => item.role === "user" || item.role === "assistant")
        .flatMap((item) => item.id && outcomes[item.id]
          ? [item, { role: "assistant" as const, content: outcomes[item.id] }]
          : [item]));
    })
      .catch((caught: unknown) => {
        if (!current) return;
        void logDiagnostic({ level: "error", eventCode: "interface.nebula_assistant.history_load_failed", message: "The saved assistant conversation could not be loaded.", outcome: "failure", stage: "history", retryable: true, exception: caught });
        setError("The saved conversation could not be loaded. Retry after Core reconnects.");
      })
      .finally(() => { if (current) setLoadingHistory(false); });
    return () => { current = false; };
  }, [api, engagement?.id, storageKey, actionOutcomesKey]);

  useEffect(() => {
    if (!api || !sessionId || !open || coreState !== "online" || typeof api.getPendingChatTurn !== "function") return;
    const controller = new AbortController();
    let observedTurnId: string | undefined;
    let lastSignature: string | undefined;
    const poll = startVisiblePoll({
      intervalMs: 2_500,
      maxIntervalMs: 8_000,
      signal: controller.signal,
      read: async (signal) => {
        try {
          const turn = await api.getPendingChatTurn(sessionId, signal);
          if (signal.aborted) return "stop";
          setPendingCheckedSessionId(sessionId);
          setPendingStatusError("");
          if (turn) {
            setAwaitingTurnResolution(false);
            if (!observedTurnId || observedTurnId !== turn.id) {
              observedTurnId = turn.id;
              if (!abort.current) needsSavedTurnReload.current = true;
            }
            if (turn.id === settledTurnId.current) return "active";
            const signature = JSON.stringify([turn.id, turn.revision, turn.status, turn.queuePosition,
              turn.waitSummary, turn.toolCalls?.map((item) => [item.toolCallId, item.status, item.summary])]);
            if (signature !== lastSignature) {
              lastSignature = signature;
              setPendingTurn(turn);
              setTurnFeedback((current) => feedbackFromPendingTurn(current, turn));
            }
            return "active";
          }
          observedTurnId = undefined;
          lastSignature = undefined;
          settledTurnId.current = undefined;
          setPendingTurn(undefined);
          setAwaitingTurnResolution(false);
          if (needsSavedTurnReload.current && typeof api.listChatMessages === "function") {
            const request = ++historyRead.current;
            const history = await api.listChatMessages(sessionId, signal);
            if (signal.aborted) return "stop";
            if (request === historyRead.current) {
              const outcomes = actionOutcomesKey ? readActionOutcomes(actionOutcomesKey) : {};
              setMessages(history.filter((item) => item.role === "user" || item.role === "assistant")
                .flatMap((item) => item.id && outcomes[item.id]
                  ? [item, { role: "assistant" as const, content: outcomes[item.id] }]
                  : [item]));
              const savedImageIds = new Set(history.flatMap((item) => item.role === "user"
                ? item.contentBlocks?.flatMap((block) => block.type === "image" && block.artifactId ? [block.artifactId] : []) ?? []
                : []));
              setPendingImages((current) => current.filter((image) => !image.artifactId || !savedImageIds.has(image.artifactId)));
              setTurnFeedback(undefined);
              needsSavedTurnReload.current = false;
            }
          }
          return "idle";
        } catch (caught) {
          if (!signal.aborted) {
            void logDiagnostic({ level: "warning", eventCode: "interface.nebula_assistant.status_load_failed", message: "Butler turn status could not be refreshed.", outcome: "failure", stage: "turn-status", retryable: true, exception: caught });
            setPendingStatusError("Could not verify Butler’s current response. Retry status before sending another message.");
          }
          return "active";
        }
      },
    });
    pendingPoll.current = poll.poke;
    window.addEventListener("online", poll.poke);
    return () => { controller.abort(); pendingPoll.current = () => {}; window.removeEventListener("online", poll.poke); };
  }, [api, sessionId, open, coreState, actionOutcomesKey]);

  useEffect(() => {
    const visible = open && !hidden && view === "chat";
    const previous = lastScroll.current;
    if (visible) {
      const mobile = nativeShell || window.matchMedia("(max-width: 760px), (max-height: 500px) and (pointer: coarse)").matches;
      if (mobile && body.current) {
        if (attentionCount > 0 && (!previous.visible || attentionCount > previous.attentionCount)) {
          body.current.scrollTop = 0;
          followsLatest.current = false;
        } else if (!loadingHistory && followsLatest.current && (previous.messages !== messages || !previous.visible || previous.loadingHistory)) {
          const lastMessage = transcript.current?.lastElementChild;
          if (lastMessage && typeof lastMessage.scrollIntoView === "function") lastMessage.scrollIntoView({ block: "end" });
          else body.current.scrollTop = body.current.scrollHeight;
        }
      } else if (transcript.current && followsLatest.current) transcript.current.scrollTop = transcript.current.scrollHeight;
      const surface = scrollSurface();
      if (surface) setHasNewerMessages(messages.length > 0 && distanceFromLatest(surface) > 24);
    }
    lastScroll.current = { visible, messages, attentionCount, loadingHistory };
  }, [messages, open, hidden, view, attentionCount, loadingHistory, distanceFromLatest, scrollSurface, nativeShell]);

  useEffect(() => {
    if (!open || hidden) return;
    if (nativeShell || window.matchMedia("(max-width: 760px), (max-height: 500px) and (pointer: coarse)").matches) return;
    input.current?.focus();
  }, [open, hidden, nativeShell]);

  const move = (x: number, y: number) => {
    const width = panel.current?.offsetWidth ?? 560;
    const height = panel.current?.offsetHeight ?? 300;
    const viewport = window.visualViewport;
    const left = viewport?.offsetLeft ?? 0;
    const top = viewport?.offsetTop ?? 0;
    setPosition({
      x: Math.max(left + 12, Math.min(x, left + (viewport?.width ?? window.innerWidth) - width - 12)),
      y: Math.max(top + 12, Math.min(y, top + (viewport?.height ?? window.innerHeight) - height - 12)),
    });
  };

  const selectedSettingsRuntime = sessionConfig
    ? { kind: sessionConfig.backend, id: sessionConfig.profileId, model: sessionConfig.model }
    : runtime;
  const selectedHarness = selectedSettingsRuntime?.kind === "harness"
    ? harnesses.find((item) => item.id === selectedSettingsRuntime.id) : undefined;
  const selectedModelOptions = selectedHarness?.modelOptions?.find((item) => item.model === selectedSettingsRuntime?.model);
  const cyberChoices = selectedHarness?.kind === "codex_app_server" ? selectedModelOptions?.cyberAccessPrograms ?? [] : [];

  const loadVoiceSettings = async () => {
    if (!api || typeof api.getButlerVoiceSettings !== "function") return;
    const request = ++voiceSettingsRequest.current;
    setVoiceSettingsBusy(true);
    setVoiceSettingsError("");
    try {
      const saved = await api.getButlerVoiceSettings();
      if (request === voiceSettingsRequest.current) setVoiceSettings(saved);
    } catch (caught) {
      void logDiagnostic({ level: "error", eventCode: "interface.nebula_assistant.voice_settings_load_failed", message: "Voice settings could not be loaded.", outcome: "failure", stage: "settings", retryable: true, exception: caught });
      if (request === voiceSettingsRequest.current) setVoiceSettingsError(caught instanceof Error ? caught.message : "Could not load Voice settings. Reopen Butler settings to retry.");
    } finally {
      if (request === voiceSettingsRequest.current) setVoiceSettingsBusy(false);
    }
  };

  const saveVoiceSettings = async (seconds: VoicePauseSeconds) => {
    if (!api || !voiceSettings || voiceSettingsBusy) return;
    const request = ++voiceSettingsRequest.current;
    setVoiceSettingsBusy(true);
    setVoiceSettingsError("");
    setVoiceSettingsStatus("");
    try {
      const saved = await api.updateButlerVoiceSettings(voiceSettings.revision, seconds);
      const observed = await api.getButlerVoiceSettings();
      if (observed.revision !== saved.revision || observed.autoPauseSeconds !== seconds) throw new Error("Core did not confirm the new Voice timeout. Reopen settings and retry.");
      if (request === voiceSettingsRequest.current) {
        setVoiceSettings(observed);
        setVoiceSettingsStatus("Saved for the next Voice session on this Core.");
      }
    } catch (caught) {
      if (request === voiceSettingsRequest.current) {
        setVoiceSettingsError(caught instanceof Error ? caught.message : "Could not save Voice settings. Choose a timeout again.");
        try {
          const refreshed = await api.getButlerVoiceSettings();
          if (request === voiceSettingsRequest.current) setVoiceSettings(refreshed);
        }
        catch (refreshError) { void logDiagnostic({ level: "warning", eventCode: "interface.nebula_assistant.voice_settings_refresh_failed", message: "Voice settings could not be refreshed after a failed save.", outcome: "failure", stage: "settings", retryable: true, exception: refreshError }); }
      }
    } finally {
      if (request === voiceSettingsRequest.current) setVoiceSettingsBusy(false);
    }
  };

  const openSettings = () => {
    setView("more");
    if (settingsOpen) { setSettingsOpen(false); return; }
    setSettingsOpen(true);
    setSettingsBusy(true);
    setVoiceSettingsStatus("");
    setSettingsError("");
    setSettingsStatus("");
  };

  useEffect(() => {
    if (!settingsOpen) { setSettingsBusy(false); return; }
    if (!api || !engagement?.id) return;
    let current = true;
    setSettingsBusy(true);
    void (async () => {
      try {
        // A settings deep link may open before the selected project loads.
        // Read Core when it becomes available, rather than capturing an empty draft.
        const restored = await api.getControlAssistantSession(engagement.id);
        if (!current) return;
        if (restored && restored.engagementId !== engagement.id) throw new Error("Core returned a conversation outside this project's Butler. Retry after reconnecting.");
        const saved = restored ? await api.getChatAssistantConfig(restored.id) : undefined;
        if (!current) return;
        const config = saved?.settings;
        if (restored) setSessionId(restored.id);
        setSessionConfig(config);
        setSettingsRevision(saved?.revision);
        setEffortDraft(config?.backend === "provider" ? config.reasoningEffort ?? "" : config?.harnessReasoningEffort ?? "");
        setCyberDraft(config?.harnessCyberAccessProgram ?? "automatic");
      } catch (caught) {
        if (!current) return;
        void logDiagnostic({ level: "warning", eventCode: "interface.nebula_assistant.settings_open_failed", message: "Butler settings could not be loaded.", outcome: "failure", stage: "settings", retryable: true, exception: caught });
        setSettingsError(caught instanceof Error ? caught.message : "Could not load Butler settings. Retry by reopening settings.");
      } finally { if (current) setSettingsBusy(false); }
    })();
    return () => { current = false; };
  }, [settingsOpen, api, engagement?.id]);

  useEffect(() => {
    if (settingsOpen) void loadVoiceSettings();
  }, [settingsOpen, api]);

  useEffect(() => {
    const openVoiceSettings = () => {
      setOpen(true);
      setHidden(false);
      if (!settingsOpen) openSettings();
      else void loadVoiceSettings();
    };
    window.addEventListener("nebula:open-butler-settings", openVoiceSettings);
    return () => window.removeEventListener("nebula:open-butler-settings", openVoiceSettings);
  });

  useEffect(() => {
    const openOversight = () => {
      setOpen(true);
      setHidden(false);
      setView("work");
      setOversightOpenRevision((revision) => revision + 1);
    };
    window.addEventListener("nebula:open-butler-oversight", openOversight);
    return () => window.removeEventListener("nebula:open-butler-oversight", openOversight);
  }, []);

  const saveSettings = async () => {
    if (!api || !selectedSettingsRuntime || settingsBusy || busy) return;
    setSettingsBusy(true);
    setSettingsError("");
    setSettingsStatus("");
    try {
      const targetSessionId = sessionId ?? (await getSavedSessionForTurn()).session.id;
      const current = settingsRevision === undefined || !sessionId
        ? await api.getChatAssistantConfig(targetSessionId)
        : { revision: settingsRevision };
      const patch = selectedSettingsRuntime.kind === "provider"
        ? { reasoningEffort: effortDraft || null }
        : { harnessReasoningEffort: effortDraft || null, ...(cyberChoices.length ? { harnessCyberAccessProgram: cyberDraft } : {}) };
      await api.updateChatAssistantConfig(targetSessionId, current.revision, patch);
      const saved = await api.getChatAssistantConfig(targetSessionId);
      setSessionConfig(saved.settings);
      setSettingsRevision(saved.revision);
      setEffortDraft(saved.settings.backend === "provider" ? saved.settings.reasoningEffort ?? "" : saved.settings.harnessReasoningEffort ?? "");
      setCyberDraft(saved.settings.harnessCyberAccessProgram ?? "automatic");
      setSettingsStatus(selectedSettingsRuntime.kind === "harness"
        ? sessionId
          ? "Saved. Applies to Butler’s next message and scheduled check."
          : "Saved for Butler’s first message and scheduled check."
        : sessionId
          ? "Saved. Applies to Butler’s next message."
          : "Saved for Butler’s first message.");
    } catch (caught) {
      void logDiagnostic({ level: "error", eventCode: "interface.nebula_assistant.settings_save_failed", message: "Butler settings could not be saved.", outcome: "failure", stage: "settings", retryable: true, exception: caught });
      setSettingsError(caught instanceof Error ? caught.message : "Could not save Butler settings. Reload and retry.");
      if (sessionId) {
        try {
          const saved = await api.getChatAssistantConfig(sessionId);
          setSessionConfig(saved.settings);
          setSettingsRevision(saved.revision);
        } catch (refreshError) {
          // Preserve the save error in the panel, while retaining the refresh failure in diagnostics.
          void logDiagnostic({ level: "warning", eventCode: "interface.nebula_assistant.settings_refresh_after_save_failed", message: "Butler settings could not be refreshed after a failed save.", outcome: "failure", stage: "settings", retryable: true, exception: refreshError });
        }
      }
    } finally { setSettingsBusy(false); }
  };

  useEffect(() => {
    if (!open || hidden) return;
    const clamp = () => {
      const viewport = window.visualViewport;
      const visualHeight = viewport?.height ?? window.innerHeight;
      panel.current?.style.setProperty("--panel-visual-top", `${viewport?.offsetTop ?? 0}px`);
      panel.current?.style.setProperty("--panel-visual-height", `${visualHeight}px`);
      panel.current?.style.setProperty("--panel-visual-bottom", `${Math.max(0, window.innerHeight - (viewport?.offsetTop ?? 0) - visualHeight)}px`);
      if (nativeShell || window.matchMedia("(max-width: 760px), (max-height: 500px) and (pointer: coarse)").matches) return;
      const rect = panel.current?.getBoundingClientRect();
      if (rect) move(rect.left, rect.top);
    };
    clamp();
    window.addEventListener("resize", clamp);
    window.visualViewport?.addEventListener("resize", clamp);
    window.visualViewport?.addEventListener("scroll", clamp);
    return () => {
      window.removeEventListener("resize", clamp);
      window.visualViewport?.removeEventListener("resize", clamp);
      window.visualViewport?.removeEventListener("scroll", clamp);
    };
  }, [open, hidden, nativeShell]);

  useEffect(() => {
    if (!nativeShell || !open || hidden) return;
    const shell = document.querySelector<HTMLElement>(".assistant-only-shell");
    const wasInert = shell?.inert ?? false;
    const previousFocus = document.activeElement as HTMLElement | null;
    if (shell) shell.inert = true;
    panel.current?.focus({ preventScroll: true });
    return () => {
      if (shell) shell.inert = wasInert;
      const returnFocus = shell?.querySelector<HTMLElement>('[aria-controls="nebula-assistant-panel"]') ?? previousFocus;
      if (returnFocus?.isConnected) returnFocus.focus({ preventScroll: true });
    };
  }, [nativeShell, open, hidden]);

  const stop = async () => {
    if (!api) return;
    try {
      const { turnId, harnessTurnId } = abort.current ? activeTurn.current : { turnId: undefined, harnessTurnId: undefined };
      if (harnessTurnId) await api.stopHarnessTurn(harnessTurnId);
      else if (turnId) await api.cancelChatTurn(turnId);
      else if (activePendingTurn?.harnessTurnId) await api.stopHarnessTurn(activePendingTurn.harnessTurnId);
      else if (activePendingTurn) await api.cancelChatTurn(activePendingTurn.id);
      else if (sessionId && typeof api.getPendingChatTurn === "function") {
        const pending = await api.getPendingChatTurn(sessionId);
        if (pending?.harnessTurnId) await api.stopHarnessTurn(pending.harnessTurnId);
        else if (pending) await api.cancelChatTurn(pending.id);
      }
      settledTurnId.current = turnId ?? activePendingTurn?.id;
      abort.current?.abort();
      abort.current = undefined;
      setPendingTurn(undefined);
      setAwaitingTurnResolution(false);
      setTurnFeedback(undefined);
      setBusy(false);
      pendingPoll.current();
    } catch (caught) {
      void logDiagnostic({ level: "error", eventCode: "interface.nebula_assistant.stop_failed", message: "The assistant response could not be stopped.", outcome: "failure", stage: "stop", retryable: true, exception: caught });
      setError(caught instanceof Error ? caught.message : "Could not stop the response. Try again.");
    }
  };

  const applyDeviceTheme = async (requestedTheme: ThemePreference): Promise<string> => {
    const entry = settingCatalog.find((item) => item.id === "settings.appearance")!;
    const label = THEME_OPTIONS.find((item) => item.value === requestedTheme)!.label;
    setHidden(true);
    onOpenSetting(entry);
    try {
      // Let the Appearance lens appear before selecting the visible theme.
      await new Promise((resolve) => window.setTimeout(resolve, 150));
      setPreference(requestedTheme);
      const applied = await new Promise<boolean>((resolve) => {
        const started = Date.now();
        const interval = window.setInterval(() => {
          if (document.documentElement.dataset.theme === requestedTheme) {
            window.clearInterval(interval);
            resolve(true);
          } else if (Date.now() - started > 3000) {
            window.clearInterval(interval);
            resolve(false);
          }
        }, 25);
      });
      if (!applied) throw new Error(`Could not confirm ${label} on this device. Check the open Appearance setting and retry.`);
      const saved = readStorage("nebula.theme") === requestedTheme;
      await new Promise((resolve) => window.setTimeout(resolve, 900));
      return saved
        ? `Changed this device to ${label}. The choice is saved in this browser.`
        : `Changed this device to ${label} for this session. Browser storage is unavailable, so the choice may reset after reload.`;
    } finally {
      onCloseSetting?.();
      setHidden(false);
    }
  };

  const applyProjectMode = async ({ mode, from }: { mode: ProjectMode; from?: ProjectMode }): Promise<string> => {
    if (!api || !engagement) throw new Error("Choose a project to change its execution mode.");
    const entry = settingCatalog.find((item) => item.id === "settings.network-scope")!;
    const projectId = engagement.id;
    const projectName = engagement.name;
    const modeLabel = mode === "host" ? "Host" : "Docker";
    setHidden(true);
    onOpenSetting(entry);
    try {
      const policy = await api.getAutomationPolicy(projectId);
      const currentMode = policy.executionMode ?? "docker";
      if (from && currentMode !== from) {
        throw new Error(`${projectName} is already in ${currentMode === "host" ? "Host" : "Docker"} mode. Its policy was not changed. Check the open setting and try again.`);
      }
      let confirmation: string;
      if (currentMode === mode) {
        onSavedSetting?.(entry, `${projectName} is already in ${modeLabel} mode.`, mode);
        confirmation = `${projectName} is already in ${modeLabel} mode. No change was needed.`;
      } else {
        if (mode === "host") {
          const approved = await confirm({
            title: `Switch ${projectName} to Host mode?`,
            message: "Agents in new sessions can access and modify host folders, mounted devices, and network destinations allowed by the server's OS account. Docker isolation and container network controls will not apply. Existing sessions keep their current policy.",
            confirmLabel: "Switch to Host mode",
            tone: "danger",
          });
          if (!approved) return `${projectName} remains in Docker mode. No change was saved.`;
        }
        await api.updateAutomationPolicy(projectId, {
          executionMode: mode,
          hostAccessAcknowledged: mode === "host",
          approvalPolicy: policy.approvalPolicy,
          networkEnabled: policy.networkEnabled,
          runnerProfileId: policy.runnerProfileId,
          vpnProfileId: policy.vpnProfileId,
          maxTimeoutMs: policy.maxTimeoutMs,
          expectedRevision: policy.revision,
        });
        const observed = await api.getAutomationPolicy(projectId);
        if (observed.executionMode !== mode) throw new Error("Core did not confirm the new execution mode. Reopen Project Policy before retrying.");
        onSavedSetting?.(entry, `${projectName} saved in ${modeLabel} mode for new sessions.`, mode);
        confirmation = `${projectName} is now in ${modeLabel} mode for new sessions. Existing sessions keep their previous policy.`;
      }
      // Close only after the refreshed control shows the mode Core read back.
      if (document.querySelector(".settings-lens")) {
        const visible = await new Promise<boolean>((resolve) => {
          const started = Date.now();
          const interval = window.setInterval(() => {
            const lens = document.querySelector(".settings-lens");
            const control = lens?.querySelector<HTMLSelectElement>('select[aria-label="Project execution mode"]');
            if (control?.value === mode && !control.disabled) {
              window.clearInterval(interval);
              resolve(true);
            } else if (!lens || Date.now() - started > 8000) {
              window.clearInterval(interval);
              resolve(false);
            }
          }, 50);
        });
        if (!visible) throw new Error(`Core saved ${modeLabel} mode, but the setting did not refresh. Reopen Project Policy to inspect it.`);
      }
      await new Promise((resolve) => window.setTimeout(resolve, 1200));
      return confirmation;
    } finally {
      onCloseSetting?.();
      setHidden(false);
    }
  };

  const applyProjectScope = async (changes: Record<string, unknown>): Promise<string> => {
    if (!api || !engagement) throw new Error("Choose a project to change its scope.");
    const entry = settingCatalog.find((item) => item.id === "settings.network-scope")!;
    const projectId = engagement.id;
    const projectName = engagement.name;
    setHidden(true);
    onOpenSetting(entry);
    try {
      const observed = await saveAssistantProjectScope(api, projectId, changes);
      const mode = observed.bypass_permissions === true ? "Allow all"
        : observed.allow_all_targets === true ? "All destinations" : "Bounded";
      const status = `${projectName} saved in ${mode} mode at scope revision ${observed.revision}.`;
      onSavedSetting?.(entry, status);
      return `${status} New sessions use this scope; existing sessions keep their frozen policy.`;
    } finally {
      onCloseSetting?.();
      setHidden(false);
    }
  };

  const applyHeadlessSession = async (action: "start" | "stop"): Promise<string> => {
    if (!api || !engagement) throw new Error("Choose a project to configure its headless browser.");
    const projectId = engagement.id;
    if (action === "start") await api.startHeadlessSecuritySession(projectId);
    else await api.stopHeadlessSecuritySession(projectId);
    const observed = await api.getHeadlessSecuritySession(projectId);
    if (action === "start" && observed.state !== "active") {
      throw new Error(`Core did not start the headless security session (${observed.state}). ${observed.last_start_error ?? "Retry after checking Core browser readiness."}`);
    }
    if (action === "stop" && observed.state === "active") {
      throw new Error("Core still reports an active headless security session. Retry stopping it.");
    }
    return `${engagement.name} headless security browser: ${observed.state}${observed.session_id ? ` · session ${observed.session_id}` : ""}.`;
  };

  const createButlerProject = async (action: Extract<SettingClientAction, {kind: "project_create" | "project_create_linked"}>, workspacePath?: string) => {
    if (!api || coreState !== "online") throw new Error("Reconnect to Core before changing a project.");
    const matchingProjects = async () => (await api.listEngagements()).items.filter((project) => projectNameKey(project.name) === projectNameKey(action.name));
    const describeMatches = (matches: Awaited<ReturnType<typeof matchingProjects>>) => matches.slice(0, 3).map((project) => `${project.id} (${project.status})`).join(", ");
    let existing;
    try {
      existing = await matchingProjects();
    } catch {
      throw new Error("Core's project list is unavailable. Reconnect and check All projects before asking Butler to create one.");
    }
    if (existing.length) throw new Error(`Core already has a project named ${action.name}: ${describeMatches(existing)}. Ask Butler to read it before creating another.`);
    const created = await createEngagement({name: action.name, description: action.description,
      clientName: action.clientName || undefined, workspacePath,
      hostAccessAcknowledged: false, requestId: action.requestId}, {select: false}).catch(async () => {
      let after;
      try {
        after = await matchingProjects();
      } catch {
        throw new Error(`Core did not confirm whether ${action.name} was created, and its project list is unavailable. Reconnect and check All projects before retrying.`);
      }
      throw new Error(after.length
        ? `Core did not confirm whether ${action.name} was created. It now lists ${describeMatches(after)}. Ask Butler to read that project before retrying.`
        : `Core did not confirm whether ${action.name} was created. Check All projects before retrying.`);
    });
    const observed = await api.getProjectLifecycle(created.id);
    if (observed.name !== action.name || observed.status === "archived"
        || observed.workspacePath !== workspacePath) {
      throw new Error("Core did not confirm the new project and its workspace. Check All projects before retrying.");
    }
    if (workspacePath) {
      const policy = await api.getAutomationPolicy(created.id);
      if (policy.hostAccessAcknowledged) throw new Error("Core unexpectedly acknowledged Host access. Inspect the project's execution policy before use.");
    }
    return observed;
  };

  const applyProjectLifecycle = async (action: Extract<SettingClientAction, { kind: "project_create" | "project_create_linked" | "project_relink" | "project_rename" | "project_archive" | "project_restore" | "project_delete" }>): Promise<string> => {
    if (!api || coreState !== "online") throw new Error("Reconnect to Core before changing a project.");
    if (action.kind === "project_create_linked") {
      if (activeImageProject.current !== action.sourceProjectId) throw new Error("Select the original project before choosing a host folder.");
      setPendingLinkedProject(action);
      setView("chat");
      return "Choose a folder on the Nebula host in Butler's project control. Core will link it to the new project; Host command access still needs operator consent.";
    }
    if (action.kind === "project_create") {
      const observed = await createButlerProject(action);
      setHidden(true);
      onNavigate(`${projectSurface(observed.id, "workbench")}?view=chat`);
      return `Created ${observed.name} in Core. Host access still needs operator consent before use.`;
    }
    const current = await api.getProjectLifecycle(action.projectId);
    if (current.revision !== action.expectedRevision || current.name !== (action.kind === "project_rename" ? action.oldName : action.name)) {
      throw new Error("Project changed. Ask Butler to read it again before retrying.");
    }
    if (action.kind === "project_relink") {
      if (activeImageProject.current !== action.sourceProjectId) throw new Error("Select the original project before choosing a host folder.");
      setPendingProjectRelink(action);
      setView("chat");
      return `Choose a Nebula-host folder for ${action.name}. Core will refuse a relink while existing work can still use the old folder.`;
    }
    if (action.kind === "project_rename") {
      await renameEngagementAtRevision(action.projectId, action.name, action.expectedRevision);
      const observed = await api.getProjectLifecycle(action.projectId);
      if (observed.name !== action.name) throw new Error("Core did not confirm the new project name. Refresh All projects.");
      return `Renamed ${action.oldName} to ${observed.name} in Core.`;
    }
    if (action.kind === "project_delete") {
      if (current.status !== "archived") throw new Error("Archive the project before permanently deleting it.");
      const approved = await confirm({
        title: `Permanently delete ${action.name}?`,
        message: "This removes the archived project, chats, application model and project records. It cannot be undone. Workspace files, including the linked host folder, will not be deleted. Immutable audit logs and stored artifact files are retained.",
        confirmLabel: "Delete permanently",
        tone: "danger",
      });
      if (!approved) return `${action.name} was not deleted. No change was saved.`;
      await deleteArchivedEngagement(action.projectId, action.expectedRevision);
      try {
        await api.getProjectLifecycle(action.projectId);
      } catch (caught) {
        if (caught instanceof ApiError && caught.status === 404) return `Permanently deleted ${action.name} from Core. Workspace files were kept.`;
        throw caught;
      }
      throw new Error("Core still has this project. Check Archived projects before retrying.");
    }
    const archiving = action.kind === "project_archive";
    if (archiving && current.status === "archived") throw new Error("This project is already archived. Read it again.");
    if (!archiving && current.status !== "archived") throw new Error("Choose an archived project to restore.");
    if (archiving) {
      const approved = await confirm({
        title: `Remove ${action.name}?`,
        message: "This archives the project and removes it from your active projects. Files and chat history are kept. Running work continues. You can restore it from Archived projects.",
        confirmLabel: "Remove project",
      });
      if (!approved) return `${action.name} remains active. No change was saved.`;
    }
    const wasSelected = engagement?.id === action.projectId;
    const nextId = await setEngagementArchived(action.projectId, archiving, action.expectedRevision);
    const observed = await api.getProjectLifecycle(action.projectId);
    if (observed.status !== (archiving ? "archived" : "active")) throw new Error("Core did not confirm the project status. Refresh All projects.");
    if (archiving && wasSelected) {
      setHidden(true);
      onNavigate(nextId ? `${projectSurface(nextId, "workbench")}?view=chat` : "/projects");
    }
    return archiving
      ? `Archived ${observed.name} in Core. Running work continues; restore it from Archived projects.`
      : `Restored ${observed.name} in Core. Select it from active projects.`;
  };

  const relinkProject = async (path: string): Promise<void> => {
    const action = pendingProjectRelink;
    if (!action || relinkingProject) return;
    if (!api || coreState !== "online" || activeImageProject.current !== action.sourceProjectId) {
      setPendingProjectRelink(undefined);
      const message = "Reconnect and select the original project before choosing another folder.";
      setError(message);
      if (action.sourceMessageId) appendBrowserActionOutcome(action, message);
      return;
    }
    setRelinkingProject(true);
    setError("");
    try {
      const current = await api.getProjectLifecycle(action.projectId);
      if (current.revision !== action.expectedRevision || current.name !== action.name) {
        throw new Error("Project changed. Ask Butler to read it again before relinking.");
      }
      if (current.workspacePath === path) {
        throw new Error("This project already uses that folder. Choose a different folder or cancel.");
      }
      const approved = await confirm({
        title: `Relink ${action.name}?`,
        message: `Current folder: ${current.workspacePath ?? "Nebula-managed workspace"}. New folder: ${path}. Future Files, Code, Notebook, terminals and agents will use the new folder. Existing files will not be moved or deleted. A connected Notebook file index will be disconnected.`,
        confirmLabel: "Relink project",
      });
      if (!approved) {
        setPendingProjectRelink(undefined);
        appendBrowserActionOutcome(action, `${action.name} was not relinked. No project folder changed.`);
        return;
      }
      let saved: Awaited<ReturnType<typeof api.relinkProjectWorkspace>>;
      try {
        saved = await api.relinkProjectWorkspace(action.projectId, path, action.expectedRevision);
      } catch (saveError) {
        const recovered = await api.getProjectLifecycle(action.projectId);
        if (recovered.revision !== action.expectedRevision + 1 || recovered.workspacePath !== path) throw saveError;
        saved = recovered;
      }
      const observed = await refreshProjectLifecycle(action.projectId);
      if (!saved.workspacePath || observed.workspacePath !== saved.workspacePath || observed.revision !== saved.revision) {
        throw new Error("Core did not confirm the new project folder. Inspect the project before retrying.");
      }
      if (activeImageProject.current !== action.sourceProjectId) throw new Error("The selected project changed. Inspect the target project before retrying.");
      setPendingProjectRelink(undefined);
      appendBrowserActionOutcome(action, `Relinked ${action.name} in Core. The old folder was kept; the new folder is visible in Files.`);
      setHidden(true);
      onNavigate(`${projectSurface(action.projectId, "workbench")}?view=workspace`);
    } catch (caught) {
      void logDiagnostic({level: "error", eventCode: "interface.nebula_assistant.project_relink_failed",
        message: "Butler could not confirm the project folder change.", outcome: "failure", stage: "project-relink", retryable: true, exception: caught});
      setPendingProjectRelink(undefined);
      const message = caught instanceof Error ? caught.message : "Core did not confirm the project folder. Inspect the project before retrying.";
      setError(message);
      if (action.sourceMessageId) appendBrowserActionOutcome(action, "Project folder change was not confirmed. Inspect the project before choosing another folder; Core may already have saved this change.");
    } finally {
      setRelinkingProject(false);
    }
  };

  const createLinkedProject = async (path: string): Promise<void> => {
    const action = pendingLinkedProject;
    if (!action || creatingLinkedProject) return;
    if (!api || coreState !== "online" || activeImageProject.current !== action.sourceProjectId) {
      setPendingLinkedProject(undefined);
      const message = "Reconnect and select the original project before choosing another folder.";
      setError(message);
      if (action.sourceMessageId) appendBrowserActionOutcome(action, message);
      return;
    }
    setCreatingLinkedProject(true);
    setError("");
    try {
      const observed = await createButlerProject(action, path);
      if (activeImageProject.current !== action.sourceProjectId) throw new Error("The selected project changed. Inspect All projects before retrying.");
      setPendingLinkedProject(undefined);
      appendBrowserActionOutcome(action, `Created ${observed.name} with host folder ${observed.workspacePath} in Core. Host command access still needs operator consent.`);
      setHidden(true);
      onNavigate(`${projectSurface(observed.id, "workbench")}?view=chat`);
    } catch (caught) {
      void logDiagnostic({level: "error", eventCode: "interface.nebula_assistant.linked_project_create_failed",
        message: "Butler could not confirm the linked project.", outcome: "failure", stage: "project-create", retryable: true, exception: caught});
      setPendingLinkedProject(undefined);
      const message = caught instanceof Error ? caught.message : "Core did not confirm the project. Inspect All projects before retrying.";
      setError(message);
      if (action.sourceMessageId) appendBrowserActionOutcome(action, message);
    } finally {
      setCreatingLinkedProject(false);
    }
  };

  const openSelectedSetting = (entry: SettingCatalogEntry) => {
    onOpenSetting(entry);
    if (entry.target === "butler-oversight") setView("work");
    setHidden(entry.target !== "butler-oversight");
  };

  const applyChatExport = async (action: Extract<SettingClientAction, {kind: "chat_export"}>): Promise<string> => {
    if (!api || !engagement || engagement.id !== action.projectId || activeImageProject.current !== action.projectId) {
      throw new Error("Select the original project and ask Butler to export the chat again.");
    }
    const current = await api.getChatSession(action.sessionId);
    if (current.engagementId !== action.projectId || current.isSideChat || current.isSubagent || current.isControlAssistant) {
      throw new Error("This chat is not a saved main conversation in the selected project.");
    }
    if (current.revision !== action.expectedRevision) {
      throw new Error("The chat changed. Ask Butler to read its current revision before exporting.");
    }
    const approved = await confirm({
      title: `Export ${current.title}?`,
      message: "The Markdown transcript can contain sensitive prompts, responses, citations, and selected-context hashes. Tool artifacts and raw evidence stay in the project evidence bundle.",
      confirmLabel: "Export transcript",
    });
    if (!approved) return `${current.title} was not exported. No download started.`;
    if (activeImageProject.current !== action.projectId) throw new Error("The selected project changed. Ask Butler to export the chat again.");
    const before = await api.getChatSession(action.sessionId);
    if (before.engagementId !== action.projectId || before.revision !== action.expectedRevision) {
      throw new Error("The chat changed during confirmation. Ask Butler to read it again before exporting.");
    }
    const messages = await api.listChatMessages(action.sessionId);
    const observed = await api.getChatSession(action.sessionId);
    if (activeImageProject.current !== action.projectId || observed.engagementId !== action.projectId || observed.revision !== action.expectedRevision) {
      throw new Error("The chat changed while preparing the transcript. Ask Butler to read it again before exporting.");
    }
    startChatTranscriptDownload({session: observed, engagementName: engagement.name, messages});
    return `Started a Markdown transcript download for ${observed.title} on this device (${messages.length} saved messages).`;
  };

  const applyReportAction = async (action: Extract<SettingClientAction, {kind: "report_create" | "report_update" | "report_delete" | "report_signoff" | "report_export_pdf" | "report_export_bundle"}>): Promise<string> => {
    if (!api || coreState !== "online" || !engagement || engagement.id !== action.projectId || activeImageProject.current !== action.projectId) {
      throw new Error("Reconnect to Core and select the original project before asking Butler to use reports again.");
    }
    const stillSelected = () => activeImageProject.current === action.projectId;
    const sameCreate = (report: Awaited<ReturnType<typeof api.getReport>>) => action.kind === "report_create"
      && report.engagementId === action.projectId && report.id === action.reportId
      && report.title === action.title && report.status === "draft"
      && report.executiveSummary === action.executiveSummary
      && JSON.stringify(report.findingIds) === JSON.stringify(action.findingIds)
      && JSON.stringify(report.observationIds) === JSON.stringify(action.observationIds);
    const currentReport = async (reportId: string, revision: number) => {
      const report = await api.getReport(reportId);
      if (!stillSelected() || report.engagementId !== action.projectId || report.revision !== revision) {
        throw new Error("The selected report changed. Ask Butler to read its current revision before retrying.");
      }
      return report;
    };
    if (action.kind === "report_create") {
      let observed;
      try {
        observed = await api.getReport(action.reportId);
      } catch (caught) {
        if (!(caught instanceof ApiError) || caught.status !== 404) throw caught;
      }
      if (!observed) {
        let createError: unknown;
        try {
          await createReport({engagementId: action.projectId, reportId: action.reportId,
            title: action.title, executiveSummary: action.executiveSummary,
            findingIds: action.findingIds, observationIds: action.observationIds});
        } catch (caught) {
          createError = caught;
          // A lost response may follow a committed write. Read the fixed ID before retrying.
          void logDiagnostic({level: "warning", eventCode: "interface.nebula_assistant.report_create_response_failed",
            message: "Butler could not confirm the report draft response and is checking Core's saved ID.",
            outcome: "failure", stage: "report-create", retryable: true, exception: caught});
        }
        try {
          observed = await api.getReport(action.reportId);
        } catch {
          if (createError instanceof ApiError && createError.status >= 400 && createError.status < 500 && createError.status !== 409) {
            throw createError;
          }
          throw new Error("Core did not confirm report creation. Inspect Reports before asking Butler to create it again.");
        }
      }
      if (!stillSelected() || !sameCreate(observed)) {
        throw new Error("Core did not confirm the exact draft. Inspect Reports before asking Butler to retry.");
      }
      await retryResource("reports");
      setHidden(true);
      onNavigate(resourcePath(action.projectId, "report", observed.id));
      return `Created ${observed.title} as a Core draft at revision ${observed.revision}.`;
    }
    if (action.kind === "report_export_bundle") {
      const before = await api.getProjectLifecycle(action.projectId);
      if (before.revision !== action.expectedProjectRevision) {
        throw new Error("The project changed. Ask Butler to read its current revision before exporting.");
      }
      const approved = await confirm({
        title: `Export ${before.name}'s sensitive bundle?`,
        message: "The bundle contains unredacted evidence, raw execution output, retained selected-tool terminal results, metadata-only terminal records, PDFs and audit records. Scratch workspace files are excluded. Store and share it as sensitive data.",
        confirmLabel: "Export bundle", tone: "danger",
      });
      if (!approved) return `${before.name}'s bundle was not exported. No download started.`;
      const ready = await api.getProjectLifecycle(action.projectId);
      if (!stillSelected() || ready.revision !== action.expectedProjectRevision) {
        throw new Error("The project changed during confirmation. Ask Butler to read it again.");
      }
      const bundle = await api.exportEngagementBundle(action.projectId);
      if (!stillSelected()) throw new Error("The selected project changed during export. Inspect Reports before retrying.");
      const filename = `${ready.name.trim().replace(/[^a-zA-Z0-9._-]+/g, "-").replace(/^-+|-+$/g, "") || "nebula-project"}.nebula.zip`;
      const url = URL.createObjectURL(bundle);
      const anchor = document.createElement("a");
      anchor.href = url; anchor.download = filename; anchor.click();
      setTimeout(() => URL.revokeObjectURL(url), 0);
      return `Started ${ready.name}'s sensitive bundle download on this device.`;
    }
    const report = await currentReport(action.reportId, action.expectedRevision);
    if (action.kind === "report_delete") {
      if (report.status === "final") throw new Error("A signed final report cannot be deleted.");
      if (report.title !== action.title) throw new Error("The report title changed. Ask Butler to read it again before deleting.");
      const approved = await confirm({
        title: `Permanently delete ${report.title}?`,
        message: "This removes the saved draft or review report. Its source findings, notes and evidence remain. A signed final report cannot be deleted.",
        confirmLabel: "Delete report", tone: "danger",
      });
      if (!approved) return `${report.title} was not deleted. No change was saved.`;
      await currentReport(action.reportId, action.expectedRevision);
      await deleteReport(report.id, action.projectId, action.expectedRevision);
      if (!stillSelected()) throw new Error("The selected project changed during deletion. Inspect Reports before retrying.");
      setHidden(true);
      onNavigate(resourcePath(action.projectId, "report"));
      return `Permanently deleted ${report.title} from Core. Its source findings, notes and evidence remain.`;
    }
    if (action.kind === "report_update") {
      if (report.status === "final") throw new Error("The signed report is immutable. Create a new draft.");
      let saved;
      try {
        saved = await updateReport(action.reportId, {
          expectedRevision: action.expectedRevision,
          ...(action.title !== undefined ? {title: action.title} : {}),
          ...(action.status !== undefined ? {status: action.status} : {}),
          ...(action.executiveSummary !== undefined ? {executiveSummary: action.executiveSummary} : {}),
          ...(action.findingIds !== undefined ? {findingIds: action.findingIds} : {}),
          ...(action.observationIds !== undefined ? {observationIds: action.observationIds} : {}),
        });
      } catch {
        throw new Error("Core did not confirm the report edit. Read the exact report before asking Butler to retry; it may already be saved.");
      }
      const observed = await api.getReport(action.reportId);
      if (!stillSelected() || observed.engagementId !== action.projectId || observed.revision !== saved.revision
          || observed.revision !== action.expectedRevision + 1
          || (action.title !== undefined && observed.title !== action.title)
          || (action.status !== undefined && observed.status !== action.status)
          || (action.executiveSummary !== undefined && observed.executiveSummary !== action.executiveSummary)
          || (action.findingIds !== undefined && JSON.stringify(observed.findingIds) !== JSON.stringify(action.findingIds))
          || (action.observationIds !== undefined && JSON.stringify(observed.observationIds) !== JSON.stringify(action.observationIds))) {
        throw new Error("Core did not confirm the exact report edit. Read the report before asking Butler to retry.");
      }
      await retryResource("reports");
      return `Saved ${observed.title} in Core as ${observed.status} at revision ${observed.revision}. [Open report](${resourcePath(action.projectId, "report", observed.id)})`;
    }
    if (action.kind === "report_signoff") {
      if (report.status !== "review") throw new Error("Save the report in review before requesting sign-off.");
      setHidden(true);
      onNavigate(`${resourcePath(action.projectId, "report", report.id)}?butler_signoff=${report.revision}`);
      return `Opened ${report.title}'s sign-off form. The operator must review and attest; Core has not finalized it yet.`;
    }
    const approved = await confirm({
      title: `Export ${report.title} as PDF?`,
      message: "This saved report may contain sensitive findings and evidence. The PDF will download to this device; Butler will not receive its bytes.",
      confirmLabel: "Export PDF",
    });
    if (!approved) return `${report.title} was not exported. No download started.`;
    await currentReport(action.reportId, action.expectedRevision);
    let render = await api.renderReport(action.reportId, action.expectedRevision);
    for (let attempt = 0; attempt < 150 && (render.status === "queued" || render.status === "rendering"); attempt += 1) {
      await new Promise((resolve) => setTimeout(resolve, 200));
      render = await api.getReportRender(render.id);
    }
    if (render.status !== "completed" || render.reportId !== report.id || render.reportRevision !== report.revision) {
      throw new Error(render.errorDetail ?? "Core did not complete the selected report PDF render. Inspect Reports before retrying.");
    }
    const pdf = await api.downloadReportPdf(render.id);
    await currentReport(action.reportId, action.expectedRevision);
    const filename = `${report.title.trim().replace(/[^a-zA-Z0-9._-]+/g, "-").replace(/^-+|-+$/g, "") || "nebula-report"}.pdf`;
    const url = URL.createObjectURL(pdf);
    const anchor = document.createElement("a");
    anchor.href = url; anchor.download = filename; anchor.click();
    setTimeout(() => URL.revokeObjectURL(url), 0);
    return `Started ${report.title} PDF download for saved revision ${report.revision} on this device${render.warnings.length ? `; Core warned: ${render.warnings.join(" ")}` : ""}.`;
  };

  const applyWorkspaceDelete = async (action: Extract<SettingClientAction, {kind: "workspace_delete"}>): Promise<string> => {
    if (!api || !engagement || engagement.id !== action.projectId || activeImageProject.current !== action.projectId) {
      throw new Error("Select the original project and ask Butler to delete the file again.");
    }
    const name = action.path.split("/").at(-1) ?? action.path;
    const approved = await confirm({
      title: `Delete ${name}?`,
      message: "This removes the scratch workspace entry. Promoted evidence remains unchanged.",
      confirmLabel: "Delete",
      tone: "danger",
    });
    if (!approved) return `${action.path} was not deleted.`;
    if (activeImageProject.current !== action.projectId) throw new Error("The selected project changed. Ask Butler to select the file again.");
    await api.deleteWorkspaceEntry(action.projectId, action.path, action.expectedSha256);
    try {
      await api.previewWorkspaceFile(action.projectId, action.path);
    } catch (error) {
      if (error instanceof ApiError && error.status === 404) return `Deleted ${action.path} from the project workspace. Core no longer serves the file.`;
      throw error;
    }
    throw new Error("Core still serves the file after deletion. Refresh Files and inspect its current state.");
  };

  const applyWorkspaceDownload = async (action: Extract<SettingClientAction, {kind: "workspace_download"}>): Promise<string> => {
    if (!api || !engagement || engagement.id !== action.projectId || activeImageProject.current !== action.projectId) {
      throw new Error("Select the original project and ask Butler to download the file again.");
    }
    const blob = await api.downloadWorkspaceFile(action.projectId, action.path);
    const bytes = new Uint8Array(await blob.arrayBuffer());
    if (activeImageProject.current !== action.projectId || bytes.length !== action.size || sha256Hex(bytes) !== action.expectedSha256) {
      throw new Error("The file changed before download. Ask Butler to read the current file and try again.");
    }
    const url = URL.createObjectURL(blob);
    try {
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = action.path.split("/").at(-1) ?? "workspace-file";
      anchor.click();
    } finally {
      globalThis.setTimeout(() => URL.revokeObjectURL(url), 1000);
    }
    return `Started a download of ${action.path} on this device (${bytes.length} bytes).`;
  };

  const applyWorkspaceUpload = async (action: Extract<SettingClientAction, {kind: "workspace_upload"}>): Promise<string> => {
    if (!api || coreState !== "online" || !engagement || engagement.id !== action.projectId || activeImageProject.current !== action.projectId) {
      throw new Error("Reconnect to Core and select the original project before choosing a workspace file.");
    }
    const directory = await api.getWorkspaceEntryState(action.projectId, action.directory);
    if (directory.kind !== "directory" || directory.entryIdentity !== action.expectedDirectoryIdentity) {
      throw new Error("The workspace directory changed. Ask Butler to list it again before uploading.");
    }
    setPendingWorkspaceUpload(action);
    setView("chat");
    return `Choose a file on this device for /workspace/${action.directory}. Core will confirm the saved bytes before Butler reports success.`;
  };

  const applyNotebookIndexAction = async (action: Extract<SettingClientAction, {kind: "notebook_index_connect" | "notebook_index_remove"}>): Promise<string> => {
    if (!api || coreState !== "online" || !engagement || engagement.id !== action.projectId || activeImageProject.current !== action.projectId) {
      throw new Error("Reconnect to Core and select the original project before changing its Notebook file index.");
    }
    const current = await api.notebookFileIndex(action.projectId);
    if (current.project_revision !== action.expectedProjectRevision) {
      throw new Error("The project changed. Ask Butler to read the Notebook file-index status again.");
    }
    if (action.kind === "notebook_index_connect") {
      setPendingNotebookIndex({...action});
      setView("chat");
      return "Choose a host SQLite index in this browser. Core will validate and read back the selected project's index; Butler will not receive the host path.";
    }
    if (!current.path) throw new Error("The file index is already absent. Ask Butler to read its status again.");
    const approved = await confirm({
      title: "Remove Notebook file index?",
      message: "The selected project will scan its folder for notebooks. The host index file will remain on disk.",
      confirmLabel: "Remove index",
    });
    if (!approved) return "Notebook file index was not removed.";
    if (activeImageProject.current !== action.projectId) throw new Error("The selected project changed. Ask Butler to read the file-index status again.");
    const before = await api.notebookFileIndex(action.projectId);
    if (before.project_revision !== action.expectedProjectRevision || before.path !== current.path) {
      throw new Error("The file-index selection changed during confirmation. Ask Butler to read it again.");
    }
    await api.removeNotebookFileIndex(action.projectId, action.expectedProjectRevision);
    const observed = await api.notebookFileIndex(action.projectId);
    if (observed.path !== null || observed.state !== "none" || observed.project_revision <= action.expectedProjectRevision) {
      throw new Error("Core did not confirm file-index removal. Inspect Notebook settings before retrying.");
    }
    return "Removed the selected project's Notebook file-index setting in Core. The host index file remains on disk.";
  };

  const appendBrowserActionOutcome = (action: {sourceMessageId?: string}, content: string): void => {
    if (!action.sourceMessageId || !actionOutcomesKey) throw new Error("Butler's saved reply is unavailable. Inspect the selected project before retrying.");
    const outcomes = readActionOutcomes(actionOutcomesKey);
    outcomes[action.sourceMessageId] = [outcomes[action.sourceMessageId], content].filter(Boolean).join("\n\n");
    writeStorage(actionOutcomesKey, JSON.stringify(Object.fromEntries(Object.entries(outcomes).slice(-50))));
    setMessages((messages) => [...messages, {role: "assistant", content}]);
  };

  const appendNotebookIndexOutcome = appendBrowserActionOutcome;

  const saveReviewedReportTransform = async (result: WritingTransformResponse): Promise<void> => {
    const action = pendingReportTransform;
    if (!action || !api || coreState !== "online" || activeImageProject.current !== action.projectId) {
      throw new Error("Reconnect and select the original project before saving this report section.");
    }
    setPendingReportTransform({...action, saveAttempted: true});
    const {report: observed, refreshFailed} = await settleButlerReportTransform(
      api, action, result, adoptConfirmedReport, () => retryResource("reports"),
    );
    if (activeImageProject.current !== action.projectId) {
      throw new Error("The selected project changed during save. Inspect Reports before retrying.");
    }
    appendBrowserActionOutcome(action, `Saved the reviewed ${action.note.title} section in ${observed.title} at Core revision ${observed.revision}. The source note remains unchanged.${refreshFailed ? " The Reports list could not refresh; use Retry reports to update the list." : ""}`);
    setPendingReportTransform(undefined);
    setHidden(true);
    onNavigate(resourcePath(action.projectId, "report", observed.id));
  };

  const connectNotebookIndex = async (path: string): Promise<void> => {
    const action = pendingNotebookIndex;
    if (!action || action.kind !== "notebook_index_connect") return;
    if (!api || coreState !== "online" || activeImageProject.current !== action.projectId) {
      setPendingNotebookIndex(undefined);
      setError("Reconnect to Core and select the original project before choosing an index again.");
      return;
    }
    setConnectingNotebookIndex(true);
    setError("");
    try {
      const before = await api.notebookFileIndex(action.projectId);
      if (before.project_revision !== action.expectedProjectRevision) {
        throw new Error("The project changed. Ask Butler to read file-index status again.");
      }
      const saved = await api.setNotebookFileIndex(action.projectId, path, action.expectedProjectRevision);
      const observed = await api.notebookFileIndex(action.projectId);
      if (activeImageProject.current !== action.projectId || !saved.path || observed.path !== saved.path
          || observed.project_revision <= action.expectedProjectRevision || observed.state === "none") {
        throw new Error("Core did not confirm the selected file index. Inspect Notebook settings before retrying.");
      }
      setPendingNotebookIndex(undefined);
      appendNotebookIndexOutcome(action, observed.state === "connected"
        ? "Connected the selected host file index in Core. Notebook discovery now uses it."
        : `Core saved the selected host file index, but its state is ${observed.state}. Inspect Notebook settings for details.`);
    } catch (caught) {
      void logDiagnostic({level: "error", eventCode: "interface.nebula_assistant.notebook_index_failed",
        message: "Butler could not confirm the Notebook file index.", outcome: "failure", stage: "notebook-index", retryable: true, exception: caught});
      setPendingNotebookIndex(undefined);
      setError(caught instanceof Error ? caught.message : "Core did not confirm the file index. Inspect Notebook settings before retrying.");
      if (action.sourceMessageId) appendNotebookIndexOutcome(action, "Core did not confirm the Notebook file index. Inspect Notebook settings before retrying.");
    } finally {
      setConnectingNotebookIndex(false);
    }
  };

  const applyWorkspaceDeleteDirectory = async (action: Extract<SettingClientAction, {kind: "workspace_delete_directory"}>): Promise<string> => {
    if (!api || coreState !== "online" || !engagement || engagement.id !== action.projectId || activeImageProject.current !== action.projectId) {
      throw new Error("Reconnect to Core and select the original project before deleting the directory.");
    }
    const before = await api.getWorkspaceEntryState(action.projectId, action.path);
    if (before.kind !== "directory" || before.entryIdentity !== action.expectedEntryIdentity) {
      throw new Error("The directory changed. Ask Butler to list it again before deleting.");
    }
    const approved = await confirm({
      title: `Delete ${action.path.split("/").at(-1)}?`,
      message: "Only an empty project workspace folder can be deleted. Its promoted evidence remains unchanged.",
      confirmLabel: "Delete folder", tone: "danger",
    });
    if (!approved) return `${action.path} was not deleted.`;
    if (activeImageProject.current !== action.projectId) throw new Error("The selected project changed. Ask Butler to list the directory again.");
    await api.deleteWorkspaceEntry(action.projectId, action.path, undefined, action.expectedEntryIdentity);
    try {
      await api.getWorkspaceEntryState(action.projectId, action.path);
    } catch (caught) {
      if (caught instanceof ApiError && caught.status === 404) {
        return `Deleted empty folder ${action.path}. Core no longer lists that entry.`;
      }
      throw caught;
    }
    throw new Error("Core still serves the folder. Refresh Files before retrying.");
  };

  const uploadWorkspaceFile = async (event: ChangeEvent<HTMLInputElement>): Promise<void> => {
    const file = event.target.files?.[0];
    event.target.value = "";
    const action = pendingWorkspaceUpload;
    if (!file || !action) return;
    if (!file.name || file.name === "." || file.name === ".." || file.name.includes("/") || file.name.includes("\\")
        || file.name.includes("\0") || file.name.length > 255) {
      setError("Choose a file with a valid single-segment name.");
      return;
    }
    const destination = action.directory ? `${action.directory}/${file.name}` : file.name;
    if (destination.length > 4096) {
      setError("The selected file path is too long for this workspace.");
      return;
    }
    if (!api || coreState !== "online" || activeImageProject.current !== action.projectId) {
      setPendingWorkspaceUpload(undefined);
      setError("Reconnect to Core and select the original project before choosing the file again.");
      return;
    }
    const controller = new AbortController();
    workspaceUploadAbort.current = controller;
    setUploadingWorkspace(true);
    setError("");
    try {
      const directory = await api.getWorkspaceEntryState(action.projectId, action.directory);
      if (directory.kind !== "directory" || directory.entryIdentity !== action.expectedDirectoryIdentity) {
        throw new Error("The destination directory changed. Ask Butler to list it again.");
      }
      let existing;
      try {
        existing = await api.getWorkspaceEntryState(action.projectId, destination);
      } catch (caught) {
        if (!(caught instanceof ApiError) || caught.status !== 404) throw caught;
      }
      if (existing && (existing.kind !== "file" || !existing.sha256)) {
        throw new Error("An entry with this name exists and is not a regular file. Choose a different file name.");
      }
      if (existing) {
        const approved = await confirm({
          title: `Replace ${file.name}?`,
          message: `This device will replace /workspace/${destination} in the selected project after Core checks its current SHA-256. Butler will not receive the file bytes.`,
          confirmLabel: "Replace file", tone: "danger",
        });
        if (!approved) {
          setPendingWorkspaceUpload(undefined);
          setMessages((current) => [...current, {role: "assistant", content: `${destination} was not replaced.`}]);
          return;
        }
        const current = await api.getWorkspaceEntryState(action.projectId, destination);
        if (current.kind !== "file" || current.sha256 !== existing.sha256) {
          throw new Error("The existing file changed during confirmation. Ask Butler to list the directory again.");
        }
      }
      if (controller.signal.aborted || activeImageProject.current !== action.projectId) {
        throw new DOMException("Upload cancelled", "AbortError");
      }
      const expectedContentSha256 = await sha256BlobHex(file, controller.signal);
      const saved = await api.uploadWorkspaceFile(action.projectId, destination, file,
        Boolean(existing), controller.signal, existing?.sha256, expectedContentSha256,
        action.expectedDirectoryIdentity);
      const observed = await api.getWorkspaceEntryState(action.projectId, destination);
      if (activeImageProject.current !== action.projectId || observed.kind !== "file"
          || saved.path !== destination || saved.size !== file.size || saved.sha256 !== expectedContentSha256
          || observed.size !== file.size || observed.sha256 !== expectedContentSha256) {
        throw new Error("Core did not confirm the exact file bytes. Inspect Files before retrying.");
      }
      setPendingWorkspaceUpload(undefined);
      setMessages((current) => [...current, {role: "assistant",
        content: `${saved.overwritten ? "Replaced" : "Uploaded"} /workspace/${destination} in Core (${file.size} bytes, SHA-256 ${expectedContentSha256}).`}]);
    } catch (caught) {
      void logDiagnostic({level: "error", eventCode: "interface.nebula_assistant.workspace_upload_failed",
        message: "Butler could not confirm the workspace file upload.", outcome: "failure",
        stage: "workspace-upload", retryable: true, exception: caught});
      setPendingWorkspaceUpload(undefined);
      setError(controller.signal.aborted
        ? "Upload cancelled. Inspect Files before choosing it again; Core may already have saved the file."
        : caught instanceof Error && caught.message.startsWith("The ") ? caught.message
        : "Core did not confirm the file upload. Inspect Files before choosing it again; it may already have been saved.");
    } finally {
      if (workspaceUploadAbort.current === controller) workspaceUploadAbort.current = undefined;
      setUploadingWorkspace(false);
    }
  };

  const applyEvidenceUpload = async (action: Extract<SettingClientAction, {kind: "evidence_upload"}>): Promise<string> => {
    if (!api || coreState !== "online" || engagement?.id !== action.projectId || activeImageProject.current !== action.projectId) {
      throw new Error("Reconnect to Core and select the original project before adding evidence.");
    }
    setPendingEvidenceUpload(action);
    setView("chat");
    return "Choose a file on this device in Butler's evidence control, review its project links, then store it in Core.";
  };

  const applyEvidenceDownload = async (action: Extract<SettingClientAction, {kind: "evidence_download"}>): Promise<string> => {
    if (!api || coreState !== "online" || engagement?.id !== action.projectId || activeImageProject.current !== action.projectId) {
      throw new Error("Reconnect to Core and select the original project before downloading evidence.");
    }
    const current = await api.getEvidence(action.evidenceId);
    if (current.engagementId !== action.projectId || current.revision !== action.expectedRevision
        || current.title !== action.title || current.sha256 !== action.expectedSha256 || !current.artifactId) {
      throw new Error("The evidence changed. Ask Butler to read its current record before downloading.");
    }
    const approved = await confirm({
      title: `Download ${current.title}?`,
      message: "The original evidence may contain sensitive data. It will download to this device; Butler will not receive its bytes.",
      confirmLabel: "Download original",
    });
    if (!approved) return `${current.title} was not downloaded. No file transfer started.`;
    if (activeImageProject.current !== action.projectId) throw new Error("The selected project changed. Ask Butler to read the evidence again.");
    const before = await api.getEvidence(action.evidenceId);
    if (before.engagementId !== action.projectId || before.revision !== action.expectedRevision
        || before.artifactId !== current.artifactId || before.sha256 !== action.expectedSha256) {
      throw new Error("The evidence changed during confirmation. Ask Butler to read it again.");
    }
    const blob = await api.getArtifactContent(before.artifactId);
    if (blob.size !== before.metadata.size || await sha256BlobHex(blob) !== action.expectedSha256) {
      throw new Error("The downloaded bytes did not match Core's evidence record. Inspect the evidence before retrying.");
    }
    const observed = await api.getEvidence(action.evidenceId);
    if (activeImageProject.current !== action.projectId || observed.revision !== action.expectedRevision
        || observed.artifactId !== before.artifactId || observed.sha256 !== action.expectedSha256) {
      throw new Error("The evidence changed during download. Ask Butler to read it again.");
    }
    startLibraryArtifactDownload(blob, observed.metadata.filename || observed.title);
    return `Started a verified download of ${observed.title}'s original file on this device.`;
  };

  const applyLibraryAction = async (action: Extract<SettingClientAction, {kind: "library_download" | "library_reindex" | "library_remove"}>): Promise<string> => {
    if (!api || coreState !== "online" || !engagement || engagement.id !== action.projectId || activeImageProject.current !== action.projectId) {
      throw new Error("Reconnect to Core and select the original project before changing the shared Library.");
    }
    const current = await api.getLibraryItem(action.itemId);
    if (current.revision !== action.expectedRevision || current.name !== action.name) {
      throw new Error("The shared Library item changed. Ask Butler to read it again before acting.");
    }
    const removing = action.kind === "library_remove";
    const downloading = action.kind === "library_download";
    if ((downloading || !removing) && !current.artifactId) throw new Error("This Library item has no original file for this action.");
    const approved = await confirm({
      title: `${removing ? "Remove" : downloading ? "Download" : "Reindex"} ${current.name}?`,
      message: downloading
        ? "The original file may contain sensitive data. It will download to this device; Butler will not receive its bytes."
        : removing
        ? "This removes the item from the Library shared by every project. The original artifact remains stored. This change cannot be undone from Butler."
        : "This rebuilds the Library item from its original artifact and changes retrieval for every project.",
      confirmLabel: removing ? "Remove shared item" : downloading ? "Download original" : "Reindex shared item",
      ...(removing ? {tone: "danger" as const} : {}),
    });
    if (!approved) return downloading
      ? `${current.name} was not downloaded. No file transfer started.`
      : `${current.name} was not ${removing ? "removed" : "reindexed"}. No change was saved.`;
    if (activeImageProject.current !== action.projectId) throw new Error("The selected project changed. Ask Butler to read the Library item again.");
    const before = await api.getLibraryItem(action.itemId);
    if (before.revision !== action.expectedRevision || before.name !== action.name) {
      throw new Error("The shared Library item changed during confirmation. Ask Butler to read it again.");
    }
    if (downloading) {
      if (!before.artifactId) throw new Error("This Library item no longer has an original file. Ask Butler to read it again.");
      const blob = await api.getArtifactContent(before.artifactId);
      const observed = await api.getLibraryItem(action.itemId);
      if (activeImageProject.current !== action.projectId || observed.revision !== action.expectedRevision
          || observed.artifactId !== before.artifactId) {
        throw new Error("The shared Library item changed during download. Ask Butler to read it again.");
      }
      if (observed.metadata.size !== undefined && blob.size !== observed.metadata.size) {
        throw new Error("The downloaded file size did not match Core's Library record. Inspect the item before retrying.");
      }
      startLibraryArtifactDownload(blob, observed.metadata.filename || observed.name);
      return `Started a download of ${observed.name}'s original file on this device.`;
    }
    if (removing) {
      try {
        await api.deleteLibraryItemAtRevision(action.itemId, action.expectedRevision);
      } catch {
        throw new Error("Core did not confirm removal. Refresh the shared Library and read this item before retrying.");
      }
      try {
        await api.getLibraryItem(action.itemId);
      } catch (caught) {
        if (caught instanceof ApiError && caught.status === 404) {
          await retryResource("library");
          return `Removed ${action.name} from Core's shared Library. Its original artifact was retained.`;
        }
        throw caught;
      }
      throw new Error("Core still lists this shared Library item. Refresh Library before retrying.");
    }
    try {
      await api.reindexLibraryItemAtRevision(action.itemId, action.expectedRevision);
    } catch {
      throw new Error("Core did not confirm reindexing. Refresh the shared Library and read this item before retrying.");
    }
    const observed = await api.getLibraryItem(action.itemId);
    if (observed.name !== action.name || observed.revision <= action.expectedRevision || observed.status !== "ready") {
      throw new Error("Core has not confirmed a ready Library item. Refresh Library and inspect its current status.");
    }
    await retryResource("library");
    return `Reindexed ${observed.name} in Core's shared Library at revision ${observed.revision}.`;
  };

  const applyLibraryAdd = async (action: Extract<SettingClientAction, {kind: "library_add"}>): Promise<string> => {
    if (!api || coreState !== "online" || !engagement || engagement.id !== action.projectId || activeImageProject.current !== action.projectId) {
      throw new Error("Reconnect to Core and select the original project before adding to the shared Library.");
    }
    setPendingLibraryAdd(action);
    setView("chat");
    return "Choose a file on this device in Butler's Library control. Core will make a confirmed item available to every project.";
  };

  const uploadLibraryFile = async (event: ChangeEvent<HTMLInputElement>): Promise<void> => {
    const file = event.target.files?.[0];
    event.target.value = "";
    const request = pendingLibraryAdd;
    if (!file || !request) return;
    if (file.size > MAX_LIBRARY_ITEM_BYTES) {
      setError(`${file.name} is larger than the 20 MB Library limit. Choose a smaller file.`);
      return;
    }
    if (!api || coreState !== "online" || activeImageProject.current !== request.projectId) {
      setPendingLibraryAdd(undefined);
      setError("Reconnect to Core and select the original project before adding a Library file.");
      return;
    }
    const approved = await confirm({
      title: `Add ${file.name} to the shared Library?`,
      message: "This device will send the file to Core. It becomes available for retrieval from every project. Butler will not receive the file bytes through this control action.",
      confirmLabel: "Add shared file",
    });
    if (!approved) {
      setPendingLibraryAdd(undefined);
      setMessages((current) => [...current, {role: "assistant", content: `${file.name} was not added to the shared Library.`}]);
      return;
    }
    if (activeImageProject.current !== request.projectId) {
      setPendingLibraryAdd(undefined);
      setError("The selected project changed. Ask Butler to add the file again from the original project.");
      return;
    }
    setUploadingLibrary(true);
    setError("");
    try {
      const created = await ingestLibraryItem({
        filename: file.name,
        mediaType: file.type || undefined,
        contentBase64: encodeLibraryFile(await file.arrayBuffer()),
      });
      const observed = await api.getLibraryItem(created.id);
      const listed = await api.listLibraryItems();
      if (observed.id !== created.id || observed.revision < created.revision
          || !listed.items.some((item) => item.id === created.id)) {
        throw new Error("Core did not confirm the new Library item in its current list.");
      }
      await retryResource("library");
      setPendingLibraryAdd(undefined);
      if (activeImageProject.current === request.projectId) {
        setMessages((current) => [...current, {role: "assistant", content: observed.status === "ready"
          ? `Added ${observed.name} to Core's shared Library. It is available to every project.`
          : `Core saved ${observed.name}, but its index is ${observed.status}. Inspect it in Library and reindex when ready.`}]);
      }
    } catch (caught) {
      void logDiagnostic({level: "error", eventCode: "interface.nebula_assistant.library_add_failed", message: "Butler could not confirm the shared Library upload.", outcome: "failure", stage: "library-upload", retryable: true, exception: caught});
      setError("Core did not confirm the upload. Inspect the shared Library before choosing the file again; it may already have been saved.");
    } finally {
      setUploadingLibrary(false);
    }
  };

  const applyClientAction = async (action: SettingClientAction): Promise<string> => {
    if (action.kind === "terminal_stop") {
      if (!api || coreState !== "online" || !engagement || engagement.id !== action.projectId || activeImageProject.current !== action.projectId) {
        throw new Error("Reconnect to Core and select the original Project before stopping its terminal.");
      }
      return applyButlerTerminalStop(api, action, () => activeImageProject.current, async (session) => confirm({
        title: `${session.state === "stopping" ? "Retry stopping" : "Stop"} terminal ${session.sessionId.slice(0, 8)}?`,
        message: session.state === "stopping"
          ? "Core could not verify this terminal's earlier removal. Retry the exact container cleanup and read its result."
          : "This stops the live terminal and ends running commands. Packages installed only in its container will be lost. Project workspace files remain.",
        confirmLabel: session.state === "stopping" ? "Retry stop" : "Stop terminal",
        tone: "danger",
      }));
    }
    if (action.kind === "report_transform_note") {
      if (!api || coreState !== "online" || engagement?.id !== action.projectId || activeImageProject.current !== action.projectId) {
        throw new Error("Reconnect to Core and select the original project before transforming its report note.");
      }
      if (pendingReportTransform) throw new Error("Review or close the open report section before requesting another transform.");
      const {note} = await readButlerReportTransform(api, action);
      if (activeImageProject.current !== action.projectId) throw new Error("The selected project changed. Ask Butler to read the report again.");
      setPendingReportTransform({...action, note});
      setView("chat");
      return `Opened ${note.title} for an AI report-section draft. The requesting browser will choose a permitted runtime and review the text before Core saves it.`;
    }
    if (action.kind === "report_create" || action.kind === "report_update" || action.kind === "report_delete" || action.kind === "report_signoff"
        || action.kind === "report_export_pdf" || action.kind === "report_export_bundle") return applyReportAction(action);
    if (action.kind === "library_add") return applyLibraryAdd(action);
    if (action.kind === "library_download" || action.kind === "library_reindex" || action.kind === "library_remove") return applyLibraryAction(action);
    if (action.kind === "evidence_upload") return applyEvidenceUpload(action);
    if (action.kind === "evidence_download") return applyEvidenceDownload(action);
    if (action.kind === "chat_export") return applyChatExport(action);
    if (action.kind === "workspace_delete") return applyWorkspaceDelete(action);
    if (action.kind === "workspace_download") return applyWorkspaceDownload(action);
    if (action.kind === "workspace_upload") return applyWorkspaceUpload(action);
    if (action.kind === "workspace_delete_directory") return applyWorkspaceDeleteDirectory(action);
    if (action.kind === "notebook_index_connect" || action.kind === "notebook_index_remove") return applyNotebookIndexAction(action);
    if (action.kind === "project_create" || action.kind === "project_create_linked" || action.kind === "project_relink" || action.kind === "project_rename" || action.kind === "project_archive"
      || action.kind === "project_restore" || action.kind === "project_delete") return applyProjectLifecycle(action);
    if (action.kind === "device_theme") return applyDeviceTheme(action.theme);
    if (action.kind === "project_execution_mode") return applyProjectMode(action);
    if (action.kind === "project_scope") return applyProjectScope(action.changes);
    if (action.kind === "headless_session") return applyHeadlessSession(action.action);
    if (action.kind === "navigate") return openDestination(action.destination);
    if (action.kind === "open_setting") {
      openSelectedSetting(action.setting);
      return `Opened ${action.setting.label}.`;
    }
    throw new Error("Unsupported Butler browser action.");
  };

  const openDestination = (destination: AssistantDestination): string => {
    const path = assistantDestinationPath(destination, engagement?.id);
    setHidden(true);
    onNavigate(path ?? "/projects");
    return path
      ? `Opened ${destination.label}.`
      : `Choose a project in All projects to open ${destination.label}.`;
  };

  const attachImageFiles = async (files: File[]) => {
    if (!files.length || uploadingImage || busy || loadingHistory) return;
    if (!api || !engagement) { setError("Choose a project before attaching an image."); return; }
    if (!imageInputSupported) { setImageHelp(true); return; }
    const projectId = engagement.id;
    const remaining = 4 - pendingImages.length;
    if (remaining <= 0) { setError("Butler can receive up to four images in one message. Remove one to add another."); return; }
    setUploadingImage(true);
    setError("");
    try {
      for (const file of files.slice(0, remaining)) {
        if (!["image/png", "image/jpeg", "image/webp"].includes(file.type)) throw new Error(`${file.name} must be a PNG, JPEG, or WebP image.`);
        if (file.size > 20 * 1024 * 1024) throw new Error(`${file.name} exceeds 20 MiB.`);
        const dataUrl = await new Promise<string>((resolve, reject) => {
          const reader = new FileReader();
          reader.onerror = () => reject(new Error(`Could not read ${file.name}.`));
          reader.onload = () => resolve(String(reader.result));
          reader.readAsDataURL(file);
        });
        if (activeImageProject.current !== projectId) return;
        const saved = await api.uploadChatImage({
          engagementId: projectId,
          filename: file.name,
          mediaType: file.type as "image/png" | "image/jpeg" | "image/webp",
          contentBase64: dataUrl.slice(dataUrl.indexOf(",") + 1),
        });
        if (activeImageProject.current !== projectId) return;
        setPendingImages((current) => [...current, {
          type: "image", artifactId: saved.artifactId, mediaType: saved.mediaType,
          alt: file.name, metadata: {preview_artifact_id: saved.previewArtifactId, width: saved.width, height: saved.height},
        }]);
      }
      if (activeImageProject.current === projectId && files.length > remaining) setError(`Attached ${remaining} images. Butler can receive up to four in one message.`);
    } catch (caught) {
      if (activeImageProject.current === projectId) {
        void logDiagnostic({level: "error", eventCode: "interface.nebula_assistant.image_upload_failed", message: "A Butler image could not be attached.", outcome: "failure", stage: "chat-media", retryable: true, exception: caught});
        setError(caught instanceof Error ? caught.message : "The image could not be attached. Choose it again to retry.");
      }
    } finally { if (activeImageProject.current === projectId) setUploadingImage(false); }
  };

  const pasteImages = (event: ReactClipboardEvent<HTMLTextAreaElement>) => {
    const files = [...event.clipboardData.files].filter((file) => file.type.startsWith("image/"));
    if (!files.length) return;
    event.preventDefault();
    void attachImageFiles(files);
  };

  const openImageRuntimeChoice = async () => {
    if (!api || !engagement) return;
    try {
      const saved = await getSavedSessionForTurn();
      setHidden(true);
      onNavigate(`/projects/${encodeURIComponent(engagement.id)}/workbench?view=chat&session=${encodeURIComponent(saved.session.id)}`);
    } catch (caught) {
      void logDiagnostic({ level: "error", eventCode: "interface.nebula_assistant.image_runtime_navigation_failed", message: "Butler could not open the image model selector.", outcome: "failure", stage: "chat-media", retryable: true, exception: caught });
      setError(caught instanceof Error ? caught.message : "Could not open Butler's model selector. Retry after reconnecting.");
    }
  };

  const send = async (currentText = draft) => {
    const prompt = currentText.trim() || (pendingImages.length ? "Attached image" : "");
    if (!prompt || activeBusy || loadingHistory || uploadingImage || !pendingReady) return;
    setView("chat");
    const hasImages = pendingImages.length > 0;
    if (hasImages && !imageInputSupported) { setImageHelp(true); return; }
    setSuggestions([]);
    const requestedTheme = themeChange(prompt);
    if (!hasImages && requestedTheme) {
      setDraft("");
      setError("");
      setBusy(true);
      setTurnFeedback(beginButlerFeedback("Changing this device’s appearance"));
      setMessages((current) => [...current, { role: "user", content: prompt }]);
      try {
        const confirmation = await applyDeviceTheme(requestedTheme);
        setMessages((current) => [...current, { role: "assistant", content: confirmation }]);
      } catch (caught) {
        void logDiagnostic({ level: "error", eventCode: "interface.nebula_assistant.theme_change_failed", message: "The assistant could not change this device's theme.", outcome: "failure", stage: "device-theme", retryable: true, exception: caught });
        setError(caught instanceof Error ? caught.message : "Could not change this device's theme. Check the open Appearance setting and retry.");
        setDraft(prompt);
      } finally {
        setBusy(false);
        setTurnFeedback(undefined);
      }
      return;
    }
    if (!hasImages && isThemeChangeCommand(prompt)) {
      setError("I couldn't identify that theme. Choose one in Settings → Appearance.");
      return;
    }
    const modeChange = projectModeChange(prompt);
    if (!hasImages && modeChange) {
      if (!api || !engagement) { setError("Choose a project to change its execution mode."); return; }
      setDraft("");
      setError("");
      setBusy(true);
      setTurnFeedback(beginButlerFeedback("Checking the project’s execution policy"));
      setMessages((current) => [...current, { role: "user", content: prompt }]);
      try {
        const confirmation = await applyProjectMode(modeChange);
        setMessages((current) => [...current, { role: "assistant", content: confirmation }]);
      } catch (caught) {
        void logDiagnostic({ level: "error", eventCode: "interface.nebula_assistant.project_mode_failed", message: "The assistant could not change the project execution mode.", outcome: "failure", stage: "project-policy", retryable: true, exception: caught });
        setError(caught instanceof Error ? caught.message : "The project policy could not be changed. Check the open setting and retry.");
        setDraft(prompt);
      } finally {
        setBusy(false);
        setTurnFeedback(undefined);
      }
      return;
    }
    const scopeChange = projectScopeChange(prompt);
    const headlessChange = headlessSessionChange(prompt);
    if (!hasImages && (scopeChange || headlessChange)) {
      if (!api || !engagement) { setError("Choose a project before changing its policy or browser session."); return; }
      setDraft("");
      setError("");
      setBusy(true);
      setTurnFeedback(beginButlerFeedback("Updating the project setting"));
      setMessages((current) => [...current, { role: "user", content: prompt }]);
      try {
        if (scopeChange) {
          const saved = await applyProjectScope(scopeChange);
          setMessages((current) => [...current, { role: "assistant", content: saved }]);
        }
        if (headlessChange) {
          const observed = await applyHeadlessSession(headlessChange);
          setMessages((current) => [...current, { role: "assistant", content: observed }]);
        }
      } catch (caught) {
        void logDiagnostic({ level: "error", eventCode: "interface.nebula_assistant.settings_change_failed", message: "The assistant could not save the requested setting.", outcome: "failure", stage: "settings", retryable: true, exception: caught });
        setError(caught instanceof Error ? caught.message : "Core could not save the setting. Retry after checking the selected project.");
        setDraft(prompt);
      } finally {
        setBusy(false);
        setTurnFeedback(undefined);
      }
      return;
    }
    const destination = destinationToOpen(prompt);
    if (!hasImages && destination) {
      setDraft("");
      const confirmation = openDestination(destination);
      setMessages((current) => [...current, { role: "user", content: prompt }, { role: "assistant", content: confirmation }]);
      return;
    }
    const setting = settingToOpen(prompt);
    if (!hasImages && setting) {
      setDraft("");
      openSelectedSetting(setting);
      return;
    }
    if (!hasImages && isSettingNavigationRequest(prompt) && (!api || !engagement || !runtime || settingNavigationCandidates(prompt).length > 0)) {
      const candidates = settingNavigationCandidates(prompt);
      setDraft("");
      setError("");
      setMessages((current) => [...current,
        { role: "user", content: prompt },
        { role: "assistant", content: candidates.length
          ? "Choose the setting you want to open."
          : "I couldn't identify a Nebula destination. Browse the places and settings below." },
      ]);
      setSuggestions(candidates);
      if (!candidates.length) {
        setView("more");
        catalogRef.current?.setAttribute("open", "");
        destinationCatalogRef.current?.setAttribute("open", "");
      }
      return;
    }
    if (!api || !engagement || !runtime) return;
    const controller = new AbortController();
    abort.current = controller;
    setBusy(true);
    setClock(Date.now());
    setTurnFeedback(beginButlerFeedback("Preparing Butler’s conversation"));
    settledTurnId.current = undefined;
    let effectiveSessionId: string;
    let effectiveConfig: ChatAssistantConfig | undefined;
    try {
      const saved = await getSavedSessionForTurn();
      if (controller.signal.aborted) {
        setBusy(false);
        setTurnFeedback(undefined);
        if (abort.current === controller) abort.current = undefined;
        return;
      }
      effectiveSessionId = saved.session.id;
      effectiveConfig = saved.config;
    } catch (caught) {
      void logDiagnostic({ level: "error", eventCode: "interface.nebula_assistant.settings_refresh_failed", message: "The assistant's next-turn settings could not be refreshed.", outcome: "failure", stage: "settings", retryable: true, exception: caught });
      setError(caught instanceof Error ? caught.message : "Could not load Butler settings. Retry after reconnecting.");
      setBusy(false);
      setTurnFeedback(undefined);
      if (abort.current === controller) abort.current = undefined;
      return;
    }
    const selectedRuntime = effectiveConfig
      ? { kind: effectiveConfig.backend, id: effectiveConfig.profileId, model: effectiveConfig.model }
      : runtime;
    const selectedRuntimeAcceptsImages = selectedRuntime.kind === "provider"
      ? Boolean(providers.find((item) => item.id === selectedRuntime.id)?.capabilities?.includes("vision"))
      : Boolean(harnesses.find((item) => item.id === selectedRuntime.id)?.modelOptions?.find((item) => item.model === selectedRuntime.model)?.imageInput);
    if (hasImages && !selectedRuntimeAcceptsImages) {
      setImageHelp(true);
      setError("Butler's saved model changed and cannot receive images. Your attachments are still ready.");
      setBusy(false);
      setTurnFeedback(undefined);
      if (abort.current === controller) abort.current = undefined;
      return;
    }
    const turnOptions = assistantTurnOptions(selectedRuntime, harnesses);
    const selectedToolRuntime: ToolSharingRuntime | undefined = selectedRuntime.kind === "provider"
      ? (() => { const profile = providers.find((item) => item.id === selectedRuntime.id); return profile ? { kind: "provider", profile } : undefined; })()
      : (() => { const profile = harnesses.find((item) => item.id === selectedRuntime.id); return profile ? { kind: "harness", profile } : undefined; })();
    let allowCloudToolResults = false;
    if (selectedToolRuntime && !(selectedToolRuntime.kind === "provider" ? selectedToolRuntime.profile.local : selectedToolRuntime.profile.localOnly)) {
      if (!selectedToolRuntime.profile.permitsSensitiveData) {
        setError(`${selectedToolRuntime.profile.name} is configured for text only. Permit project data in Settings to use settings tools.`);
        setBusy(false);
        setTurnFeedback(undefined);
        if (abort.current === controller) abort.current = undefined;
        return;
      }
      // The profile's saved sensitive-data permission is the standing consent
      // for this dedicated assistant; a settings instruction needs no second dialog.
      allowCloudToolResults = true;
    }
    let contextAttachments;
    try {
      contextAttachments = await Promise.all(butlerDrafts.map(createHashedSelectionAttachment));
    } catch (caught) {
      void logDiagnostic({ level: "error", eventCode: "interface.nebula_assistant.context_attachment_failed", message: "Could not prepare selected Butler context.", outcome: "failure", stage: "context", retryable: true, exception: caught });
      setError(caught instanceof Error ? caught.message : "Could not prepare Butler context. Remove the selection and try again.");
      setBusy(false);
      setTurnFeedback(undefined);
      if (abort.current === controller) abort.current = undefined;
      return;
    }
    activeTurn.current = {};
    setError("");
    setTurnFeedback((current) => current ? { ...current, label: "Starting Butler’s response" } : beginButlerFeedback("Starting Butler’s response"));
    const outgoingUser: ChatMessage = { role: "user", content: prompt,
      ...(hasImages ? {contentBlocks: [{type: "text" as const, text: prompt}, ...pendingImages]} : {}),
    };
    setMessages((current) => [...current, outgoingUser]);
    setDraft("");
    const appliedCallIds = new Set<string>();
    const confirmations: string[] = [];
    const workReceipts: { url: string; content: string }[] = [];
    let notebookIndexConnectRequested = false;
    let linkedProjectRequested = false;
    let reportTransformRequested = false;
    let projectRelinkRequested = false;
    let evidenceUploadRequested = false;
    let actionQueue = Promise.resolve();
    try {
      const result = await api.streamChat({
        controlAssistant: true,
        engagementId: engagement.id,
        sessionId: effectiveSessionId,
        backend: selectedRuntime.kind,
        providerId: selectedRuntime.kind === "provider" ? selectedRuntime.id : undefined,
        harnessProfileId: selectedRuntime.kind === "harness" ? selectedRuntime.id : undefined,
        model: turnOptions.model,
        reasoningEffort: selectedRuntime.kind === "provider" ? effectiveConfig?.reasoningEffort ?? turnOptions.reasoningEffort : undefined,
        harnessReasoningEffort: selectedRuntime.kind === "harness" ? effectiveConfig?.harnessReasoningEffort ?? turnOptions.harnessReasoningEffort : undefined,
        harnessServiceTier: selectedRuntime.kind === "harness" ? effectiveConfig?.harnessServiceTier ?? turnOptions.harnessServiceTier : undefined,
        harnessMode: selectedRuntime.kind === "harness" ? effectiveConfig?.harnessMode : undefined,
        harnessCyberAccessProgram: selectedRuntime.kind === "harness" ? effectiveConfig?.harnessCyberAccessProgram : undefined,
        mcpServerIds: effectiveConfig?.mcpServerIds,
        hookIds: effectiveConfig?.hookIds,
        includeKnowledge: effectiveConfig?.includeKnowledge,
        allowCloudKnowledge: effectiveConfig?.allowCloudKnowledge,
        allowAgentMessaging: effectiveConfig?.allowAgentMessaging,
        allowSubagents: effectiveConfig?.allowSubagents,
        maxActiveSubagents: effectiveConfig?.maxActiveSubagents,
        subagentProviderId: effectiveConfig?.subagentProviderId,
        subagentHarnessId: effectiveConfig?.subagentHarnessProfileId,
        subagentModel: effectiveConfig?.subagentModel,
        subagentReasoningEffort: effectiveConfig?.subagentReasoningEffort,
        messages: [outgoingUser],
        contextAttachments: contextAttachments.length ? contextAttachments : undefined,
        toolsEnabled: effectiveConfig?.toolsEnabled ?? false,
        allowCloudToolResults,
      }, (event) => {
        if (event.type === "started" && event.turnId) activeTurn.current.turnId = event.turnId;
        if ("harnessTurnId" in event && event.harnessTurnId) activeTurn.current.harnessTurnId = event.harnessTurnId;
        setTurnFeedback((current) => feedbackFromButlerEvent(current, event));
        if (event.type === "started" || event.type === "queued") pendingPoll.current();
        const action = settingClientAction(event, engagement.id);
        if (action && event.type === "tool_completed" && !appliedCallIds.has(event.toolCallId)) {
          appliedCallIds.add(event.toolCallId);
          if (action.kind === "notebook_index_connect") notebookIndexConnectRequested = true;
          if (action.kind === "project_create_linked") linkedProjectRequested = true;
          if (action.kind === "report_transform_note") reportTransformRequested = true;
          if (action.kind === "project_relink") projectRelinkRequested = true;
          if (action.kind === "evidence_upload") evidenceUploadRequested = true;
          actionQueue = actionQueue.then(async () => {
            if (!controller.signal.aborted) confirmations.push(await applyClientAction(action));
          });
        }
        const workReceipt = assistantWorkReceipt(event, engagement.id);
        if (workReceipt && event.type === "tool_completed" && !appliedCallIds.has(event.toolCallId)) {
          appliedCallIds.add(event.toolCallId);
          workReceipts.push(workReceipt);
        }
      }, controller.signal);
      await actionQueue;
      if (result?.sessionId && storageKey) {
        localStorage.setItem(storageKey, result.sessionId);
        setSessionId(result.sessionId);
        const runtimeId = result.backend === "harness" ? result.harnessProfileId : result.providerId;
        if (runtimeId) setSessionRuntime({ kind: result.backend === "harness" ? "harness" : "provider", id: runtimeId, model: result.model });
        void api.getControlAssistantSession(engagement.id).then((latest) => {
          if (latest && latest.id === result.sessionId) {
            setSessionConfig(latest.assistantConfig);
            if (typeof api.getChatAssistantConfig === "function") {
              void api.getChatAssistantConfig(latest.id).then((saved) => setSessionConfig(saved.settings));
            }
          }
        });
      }
      if (result?.message) {
        if (notebookIndexConnectRequested) {
          setPendingNotebookIndex((current) => current?.kind === "notebook_index_connect"
            && current.projectId === engagement.id && result.message!.id
            ? {...current, sourceMessageId: result.message!.id} : undefined);
          if (!result.message.id) setError("Butler's saved reply is unavailable. Inspect Notebook settings before choosing an index.");
        }
        if (linkedProjectRequested) {
          setPendingLinkedProject((current) => current?.sourceProjectId === engagement.id && result.message!.id
            ? {...current, sourceMessageId: result.message!.id} : undefined);
          if (!result.message.id) setError("Butler's saved reply is unavailable. Inspect All projects before choosing a folder.");
        }
        if (reportTransformRequested) {
          setPendingReportTransform((current) => current?.projectId === engagement.id && result.message!.id
            ? {...current, sourceMessageId: result.message!.id} : undefined);
          if (!result.message.id) setError("Butler's saved reply is unavailable. Inspect Reports before requesting a transform again.");
        }
        if (projectRelinkRequested) {
          setPendingProjectRelink((current) => current?.sourceProjectId === engagement.id && result.message!.id
            ? {...current, sourceMessageId: result.message!.id} : undefined);
          if (!result.message.id) setError("Butler's saved reply is unavailable. Inspect the project before choosing a folder.");
        }
        if (evidenceUploadRequested) {
          setPendingEvidenceUpload((current) => current?.projectId === engagement.id && result.message!.id
            ? {...current, sourceMessageId: result.message!.id} : undefined);
          if (!result.message.id) setError("Butler's saved reply is unavailable. Inspect Evidence before choosing a file.");
        }
        setPendingImages([]);
        if (contextAttachments.length) context?.consumeButlerDrafts(butlerDrafts);
        settledTurnId.current = activeTurn.current.turnId;
        setPendingTurn(undefined);
        setAwaitingTurnResolution(false);
        setTurnFeedback(undefined);
        confirmations.push(...workReceipts.filter(({ url }) => !result.message.content.includes(url)).map(({ content }) => content));
        if (result.message.id && confirmations.length && actionOutcomesKey) {
          const outcomes = readActionOutcomes(actionOutcomesKey);
          outcomes[result.message.id] = confirmations.join("\n\n");
          writeStorage(actionOutcomesKey, JSON.stringify(Object.fromEntries(Object.entries(outcomes).slice(-50))));
        }
        setMessages((current) => current.some((item) => item.id && item.id === result.message!.id)
          ? current : [...current, result.message!, ...confirmations.map((content) => ({ role: "assistant" as const, content }))]);
        setMemoryRefreshRevision((current) => current + 1);
      }
      else if (!controller.signal.aborted) {
        needsSavedTurnReload.current = true;
        setAwaitingTurnResolution(typeof api.getPendingChatTurn === "function");
        pendingPoll.current();
      }
    } catch (caught) {
      if (!controller.signal.aborted) {
        needsSavedTurnReload.current = true;
        setAwaitingTurnResolution(typeof api.getPendingChatTurn === "function");
        pendingPoll.current();
        void logDiagnostic({ level: "error", eventCode: "interface.nebula_assistant.turn_failed", message: "The assistant could not answer.", outcome: "failure", stage: "turn", retryable: true, exception: caught });
        if (hasImages) {
          try {
            const history = await api.listChatMessages(effectiveSessionId);
            const saved = history.some((message) => message.role === "user" && message.contentBlocks?.some((block) =>
              block.type === "image" && pendingImages.some((image) => image.artifactId === block.artifactId)));
            if (saved) {
              setMessages(history.filter((message) => message.role === "user" || message.role === "assistant"));
              setPendingImages([]);
              setError("Butler saved your image, but the response failed. Reopen this conversation to check its status.");
            } else {
              setMessages((current) => current.filter((message) => message !== outgoingUser));
              setDraft(prompt);
              const detail = caught instanceof Error ? caught.message : "Butler could not send the image.";
              setError(`${detail} The image remains attached. Check Butler's saved conversation before retrying.`);
            }
          } catch (historyError) {
            void logDiagnostic({ level: "error", eventCode: "interface.nebula_assistant.image_send_history_check_failed", message: "Butler could not verify whether the image was saved after the send failed.", outcome: "failure", stage: "chat-media", retryable: true, exception: historyError });
            setDraft(prompt);
            setError("Butler's send failed, and its saved state could not be checked. Reopen this conversation before retrying.");
          }
        } else {
          setDraft(prompt);
          setError(caught instanceof Error ? caught.message : "The assistant could not answer. Try again.");
        }
      }
    } finally {
      if (abort.current === controller) abort.current = undefined;
      activeTurn.current = {};
      setBusy(false);
    }
  };

  const startVoice = () => {
    if (!api || !engagement || !runtime || busy || loadingHistory) return;
    // Begin permission from the actual button activation for mobile browsers.
    const microphone = window.isSecureContext && navigator.mediaDevices?.getUserMedia
      ? navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true } })
      : undefined;
    setVoiceStart({ microphone });
    setOpen(true);
    setHidden(true);
  };

  const delegateVoice = async (prompt: string): Promise<string | undefined> => {
    if (!api || !engagement || !runtime) throw new Error("Choose a project and a healthy model runtime to direct work.");
    const saved = await getSavedSessionForTurn();
    const effectiveSessionId = saved.session.id;
    const effectiveConfig = saved.config;
    const selectedRuntime = effectiveConfig
      ? { kind: effectiveConfig.backend, id: effectiveConfig.profileId, model: effectiveConfig.model }
      : runtime;
    const options = assistantTurnOptions(selectedRuntime, harnesses);
    const selectedToolRuntime: ToolSharingRuntime | undefined = selectedRuntime.kind === "provider"
      ? (() => { const profile = providers.find((item) => item.id === selectedRuntime.id); return profile ? { kind: "provider", profile } : undefined; })()
      : (() => { const profile = harnesses.find((item) => item.id === selectedRuntime.id); return profile ? { kind: "harness", profile } : undefined; })();
    const cloud = selectedToolRuntime && !(selectedToolRuntime.kind === "provider" ? selectedToolRuntime.profile.local : selectedToolRuntime.profile.localOnly);
    if (cloud && !selectedToolRuntime.profile.permitsSensitiveData) throw new Error("Permit project data for this runtime in Settings before using voice work controls.");
    const confirmations: string[] = [];
    const workReceipts: { url: string; content: string }[] = [];
    const applied = new Set<string>();
    let notebookIndexConnectRequested = false;
    let linkedProjectRequested = false;
    let reportTransformRequested = false;
    let projectRelinkRequested = false;
    let evidenceUploadRequested = false;
    let actionQueue = Promise.resolve();
    setMessages((current) => [...current, { role: "user", content: prompt }]);
    const result = await api.streamChat({
      controlAssistant: true,
      engagementId: engagement.id,
      sessionId: effectiveSessionId,
      backend: selectedRuntime.kind,
      providerId: selectedRuntime.kind === "provider" ? selectedRuntime.id : undefined,
      harnessProfileId: selectedRuntime.kind === "harness" ? selectedRuntime.id : undefined,
      model: options.model,
      reasoningEffort: selectedRuntime.kind === "provider" ? effectiveConfig?.reasoningEffort ?? options.reasoningEffort : undefined,
      harnessReasoningEffort: selectedRuntime.kind === "harness" ? effectiveConfig?.harnessReasoningEffort ?? options.harnessReasoningEffort : undefined,
      harnessServiceTier: selectedRuntime.kind === "harness" ? effectiveConfig?.harnessServiceTier ?? options.harnessServiceTier : undefined,
      harnessMode: selectedRuntime.kind === "harness" ? effectiveConfig?.harnessMode : undefined,
      harnessCyberAccessProgram: selectedRuntime.kind === "harness" ? effectiveConfig?.harnessCyberAccessProgram : undefined,
      mcpServerIds: effectiveConfig?.mcpServerIds,
      hookIds: effectiveConfig?.hookIds,
      includeKnowledge: effectiveConfig?.includeKnowledge,
      allowCloudKnowledge: effectiveConfig?.allowCloudKnowledge,
      allowAgentMessaging: effectiveConfig?.allowAgentMessaging,
      allowSubagents: effectiveConfig?.allowSubagents,
      maxActiveSubagents: effectiveConfig?.maxActiveSubagents,
      subagentProviderId: effectiveConfig?.subagentProviderId,
      subagentHarnessId: effectiveConfig?.subagentHarnessProfileId,
      subagentModel: effectiveConfig?.subagentModel,
      subagentReasoningEffort: effectiveConfig?.subagentReasoningEffort,
      messages: [{ role: "user", content: prompt }],
      toolsEnabled: effectiveConfig?.toolsEnabled ?? false,
      allowCloudToolResults: Boolean(cloud),
    }, (event) => {
      const action = settingClientAction(event, engagement.id);
      if (action && event.type === "tool_completed" && !applied.has(event.toolCallId)) {
        applied.add(event.toolCallId);
        if (action.kind === "notebook_index_connect") notebookIndexConnectRequested = true;
        if (action.kind === "project_create_linked") linkedProjectRequested = true;
        if (action.kind === "report_transform_note") reportTransformRequested = true;
        if (action.kind === "project_relink") projectRelinkRequested = true;
        if (action.kind === "evidence_upload") evidenceUploadRequested = true;
        actionQueue = actionQueue.then(async () => { confirmations.push(await applyClientAction(action)); });
      }
      const receipt = assistantWorkReceipt(event, engagement.id);
      if (receipt && event.type === "tool_completed" && !applied.has(event.toolCallId)) {
        applied.add(event.toolCallId);
        workReceipts.push(receipt);
      }
    });
    await actionQueue;
    if (result?.sessionId && storageKey) {
      writeStorage(storageKey, result.sessionId);
      setSessionId(result.sessionId);
    }
    if (!result?.message) throw new Error("The assistant response is still running. Check the saved work before retrying.");
    if (notebookIndexConnectRequested) {
      setPendingNotebookIndex((current) => current?.kind === "notebook_index_connect"
        && current.projectId === engagement.id && result.message!.id
        ? {...current, sourceMessageId: result.message!.id} : undefined);
      if (!result.message.id) throw new Error("Butler's saved reply is unavailable. Inspect Notebook settings before choosing an index.");
    }
    if (linkedProjectRequested) {
      setPendingLinkedProject((current) => current?.sourceProjectId === engagement.id && result.message!.id
        ? {...current, sourceMessageId: result.message!.id} : undefined);
      if (!result.message.id) throw new Error("Butler's saved reply is unavailable. Inspect All projects before choosing a folder.");
    }
    if (reportTransformRequested) {
      setPendingReportTransform((current) => current?.projectId === engagement.id && result.message!.id
        ? {...current, sourceMessageId: result.message!.id} : undefined);
      if (!result.message.id) throw new Error("Butler's saved reply is unavailable. Inspect Reports before requesting a transform again.");
    }
    if (projectRelinkRequested) {
      setPendingProjectRelink((current) => current?.sourceProjectId === engagement.id && result.message!.id
        ? {...current, sourceMessageId: result.message!.id} : undefined);
      if (!result.message.id) throw new Error("Butler's saved reply is unavailable. Inspect the project before choosing a folder.");
    }
    if (evidenceUploadRequested) {
      setPendingEvidenceUpload((current) => current?.projectId === engagement.id && result.message!.id
        ? {...current, sourceMessageId: result.message!.id} : undefined);
      if (!result.message.id) throw new Error("Butler's saved reply is unavailable. Inspect Evidence before choosing a file.");
    }
    confirmations.push(...workReceipts.filter(({ url }) => !result.message!.content.includes(url)).map(({ content }) => content));
    if (result.message.id && confirmations.length && actionOutcomesKey) {
      const outcomes = readActionOutcomes(actionOutcomesKey);
      outcomes[result.message.id] = confirmations.join("\n\n");
      writeStorage(actionOutcomesKey, JSON.stringify(Object.fromEntries(Object.entries(outcomes).slice(-50))));
    }
    setMessages((current) => [...current, result.message!, ...confirmations.map((content) => ({ role: "assistant" as const, content }))]);
    setMemoryRefreshRevision((current) => current + 1);
    return [result.message.content, ...confirmations].filter(Boolean).join("\n\n");
  };

  const showTurnStatus = activeBusy || Boolean(pendingStatusError) || Boolean(sessionId && !pendingReady);
  const turnStatusLabel = pendingStatusError ? "Butler status unavailable"
    : coreState !== "online" ? "Connection lost; Butler may still be working"
    : activePendingTurn?.status === "waiting_approval" ? "Approval needed to continue"
    : turnFeedback?.label ?? (pendingReady ? "Butler is working" : "Checking Butler’s saved work");
  const turnStartedAt = turnFeedback?.startedAt ?? Date.parse(activePendingTurn?.queuedAt ?? activePendingTurn?.startedAt ?? "");
  const lastSignalAt = turnFeedback?.lastSignalAt;
  const needsReview = activePendingTurn?.status === "waiting_approval"
    || activePendingTurn?.status === "failed" || activePendingTurn?.status === "interrupted";
  const canStopActive = Boolean(abort.current || awaitingTurnResolution
    || activePendingTurn && activePendingTurn.status !== "failed" && activePendingTurn.status !== "interrupted");
  const matches = query.trim() ? settingCatalog.filter((entry) =>
    [entry.label, entry.description, ...entry.keywords].join(" ").toLowerCase().includes(query.toLowerCase())) : settingCatalog;
  const destinationMatches = destinationQuery.trim() ? assistantDestinations.filter((entry) =>
    [entry.label, ...entry.aliases].join(" ").toLowerCase().includes(destinationQuery.toLowerCase())) : assistantDestinations;
  const directSettingIds = new Set(["settings.appearance", "settings.providers", "settings.harnesses", "settings.follow-up", "settings.network-scope", "settings.automation-runtime"]);
  const panelContent = open ? <>
    {!nativeShell && <button className={styles.backdrop} type="button" aria-label="Dismiss Butler panel" title="Dismiss Butler panel" hidden={hidden} onClick={() => setHidden(true)} />}
    <div ref={panel} id="nebula-assistant-panel" className={`${styles.panel}${nativeShell ? ` ${styles.shellPanel}` : ""}`} role="dialog" aria-label="Butler" aria-modal={nativeShell ? true : undefined} tabIndex={nativeShell ? -1 : undefined} data-selection-recipient="butler" style={{ "--panel-left": `${position.x}px`, "--panel-top": `${position.y}px` } as CSSProperties} hidden={hidden}>
    <div className={styles.header}>
      <button className={`icon-button subtle ${styles.dragButton}`} type="button" aria-label="Move Butler" title="Drag to move · arrow keys to reposition" onPointerDown={(event) => { if (event.button !== 0) return; event.currentTarget.setPointerCapture(event.pointerId); drag.current = { x: event.clientX, y: event.clientY, left: position.x, top: position.y }; }} onPointerMove={(event) => { if (drag.current) move(drag.current.left + event.clientX - drag.current.x, drag.current.top + event.clientY - drag.current.y); }} onPointerUp={() => { drag.current = undefined; }} onKeyDown={(event) => { const step: Record<string, [number, number]> = { ArrowLeft: [-24, 0], ArrowRight: [24, 0], ArrowUp: [0, -24], ArrowDown: [0, 24] }; const offset = step[event.key]; if (offset) { event.preventDefault(); move(position.x + offset[0], position.y + offset[1]); } }}><GripHorizontal size={18} /></button>
      <div className={styles.identity}><strong>Butler</strong><small>{engagement?.name ?? "Choose a project"}</small></div>
      <button className={`icon-button subtle ${styles.voiceButton}`} type="button" aria-label="Start Butler Voice" title="Start Butler Voice" disabled={!api || !engagement || !runtime || busy || loadingHistory} onClick={startVoice}><MicrophoneIcon size={18} aria-hidden="true" /></button>
      <button className={`icon-button subtle ${styles.settingsButton}`} type="button" aria-label="Butler settings" aria-expanded={settingsOpen && view === "more"} aria-controls="butler-settings" title="Butler settings" disabled={!api || busy} onClick={() => void openSettings()}><Settings2 size={18} /></button>
      <button className={`icon-button subtle ${styles.hideButton}`} type="button" aria-label="Hide Butler" title="Hide Butler" onClick={() => setHidden(true)}><Minimize2 size={18} aria-hidden="true" /></button>
      <button className={`icon-button subtle ${styles.closeButton}`} type="button" aria-label="Close Butler" title="Close Butler; keep history" onClick={() => { setOpen(false); setHidden(false); setView("chat"); setSettingsOpen(false); setPendingCheckedSessionId(undefined); }}><X size={18} /></button>
    </div>
    <nav className={styles.viewTabs} aria-label="Butler views">
      <button id="butler-tab-chat" type="button" className={styles.viewTab} aria-current={view === "chat" ? "page" : undefined} aria-controls="butler-view-chat" onClick={() => setView("chat")}>Chat</button>
      <button id="butler-tab-work" type="button" className={styles.viewTab} aria-current={view === "work" ? "page" : undefined} aria-controls="butler-view-work" onClick={() => setView("work")}>Work{workAttentionCount > 0 && <span className={styles.workCount} aria-label={`${workAttentionCount} need attention`}>{workAttentionCount}</span>}</button>
      <button id="butler-tab-more" type="button" className={styles.viewTab} aria-current={view === "more" ? "page" : undefined} aria-controls="butler-view-more" onClick={() => setView("more")}>More</button>
    </nav>
    <div ref={body} className={styles.body} onScroll={(event) => handleConversationScroll(event.currentTarget)}>
      <section id="butler-view-chat" className={styles.viewPanel} aria-labelledby="butler-tab-chat" hidden={view !== "chat"}>
        {workAttentionCount > 0 && <div className={styles.attentionLine} role="status"><span>{workAttentionCount} {workAttentionCount === 1 ? "item needs" : "items need"} attention</span><button className="button quiet" type="button" onClick={() => setView("work")}>Review work</button></div>}
        {coreState === "offline" && <p className={styles.connectionLine} role="status">Butler is offline. Your conversation remains available.</p>}
        {messages.length === 0 && !loadingHistory && <div className={styles.emptyConversation}><strong>Ask Butler</strong><p>Check on work, start an assistant or mission, or change a setting.</p></div>}
      <ButlerTranscript messages={messages} api={api} projectId={engagement?.id} sessionId={sessionId} transcriptRef={transcript} onScroll={handleConversationScroll} onOpenSetting={onOpenSetting} onNavigate={onNavigate} onHide={setHidden} />
      {suggestions.length > 0 && <div className={styles.settingList} role="group" aria-label="Suggested settings">{suggestions.map((entry) => <button key={entry.id} className="button quiet" type="button" onClick={() => { openSelectedSetting(entry); setSuggestions([]); }}>{entry.label}</button>)}</div>}
      {error && <p role="alert">{error}</p>}
      {loadingHistory && <p role="status">Restoring your saved conversation…</p>}
      {!engagement && <p role="status">Choose a project to start a conversation. Open More to change device appearance.</p>}
      {engagement && !runtime && <p role="status">Connect a model provider or harness to ask Nebula.</p>}
      </section>
      <section id="butler-view-work" className={styles.viewPanel} aria-labelledby="butler-tab-work" hidden={view !== "work"}>
        <div className={styles.viewHeading}><strong>Work</strong><span>{workAttentionCount ? `${workAttentionCount} need attention` : "No items need attention"}</span></div>
      {!hidden && engagement && <div className={styles.butlerWorkspace}>
        <ButlerOversight key={engagement.id} api={api} projectId={engagement.id} coreState={coreState} compact openRevision={oversightOpenRevision} onIncidentChanged={() => void refreshAttention()} onNavigate={(path) => { onNavigate(path); setHidden(true); }} />
      </div>}
      {engagement && runtime && !loadingHistory && <details className={styles.catalog} open={messages.length === 0}>
        <summary>Work I can help with</summary>
        <div className={styles.quickActions} role="group" aria-label="Assistant work requests">
          <button className="button quiet" type="button" onClick={() => { setDraft("What are my assistants and missions doing in this project?"); setView("chat"); input.current?.focus(); }}>Check work</button>
          <button className="button quiet" type="button" onClick={() => { setDraft("Start an assistant to "); setView("chat"); input.current?.focus(); }}>Start assistant</button>
          <button className="button quiet" type="button" onClick={() => { setDraft("Start a mission to "); setView("chat"); input.current?.focus(); }}>Start mission</button>
        </div>
      </details>}
        {!engagement && <p role="status">Choose a project to review Butler’s work.</p>}
      </section>
      <section id="butler-view-more" className={styles.viewPanel} aria-labelledby="butler-tab-more" hidden={view !== "more"}>
      {settingsOpen && <section id="butler-settings" className={styles.ownSettings} aria-label="Butler settings">
        <div className={styles.settingsHeading}><strong>Butler settings</strong><span>{selectedSettingsRuntime?.model ?? ""}</span></div>
        <form onSubmit={(event) => { event.preventDefault(); void saveSettings(); }}>
          {selectedSettingsRuntime?.kind === "provider" && <label>Effort<select aria-label="Butler effort" value={effortDraft} disabled={settingsBusy || Boolean(settingsError)} onChange={(event) => setEffortDraft(event.target.value)}><option value="">Model default</option>{REASONING_EFFORTS.map((item) => <option key={item} value={item}>{item === "none" ? "None" : item}</option>)}</select></label>}
          {selectedSettingsRuntime?.kind === "harness" && <label>Effort<select aria-label="Butler effort" value={effortDraft} disabled={settingsBusy || Boolean(settingsError) || !selectedModelOptions?.reasoningEfforts.length} onChange={(event) => setEffortDraft(event.target.value)}><option value="">Harness default</option>{effortDraft && !selectedModelOptions?.reasoningEfforts.some((item) => item.id === effortDraft) && <option value={effortDraft}>{effortDraft} · saved</option>}{selectedModelOptions?.reasoningEfforts.map((item) => <option key={item.id} value={item.id} title={item.description || undefined}>{item.label}</option>)}</select></label>}
          {cyberChoices.length > 0 && <label>Cyber access<select aria-label="Butler cyber access" value={cyberDraft} disabled={settingsBusy || Boolean(settingsError)} onChange={(event) => setCyberDraft(event.target.value as ChatAssistantConfig["harnessCyberAccessProgram"])}><option value="automatic">Codex automatic</option>{cyberChoices.map((choice) => <option key={choice} value={choice}>{choice === "standard" ? "Standard" : choice === "daybreakBlue" ? "Daybreak Blue" : "Daybreak Red"}</option>)}</select><small>Available choices come from this Codex account and model.</small></label>}
          {selectedSettingsRuntime?.kind === "harness" && !selectedModelOptions?.reasoningEfforts.length && <small>This harness does not advertise effort choices.</small>}
          <div className={styles.settingsActions}><button className="button quiet" type="submit" disabled={settingsBusy || Boolean(settingsError) || busy}>Save</button>{sessionId && engagement && <button className="button quiet" type="button" onClick={() => { setHidden(true); onNavigate(`/projects/${encodeURIComponent(engagement.id)}/workbench?view=chat&session=${encodeURIComponent(sessionId)}&assistantSettings=open`); }}>Full Assistant settings</button>}</div>
          {settingsBusy && <p role="status">{!api ? "Connect to Core to load Butler settings." : !engagement ? "Choose a project to load Butler settings." : "Loading Butler settings…"}</p>}
          {settingsStatus && <p role="status">{settingsStatus}</p>}
          {settingsError && <p role="alert">{settingsError}</p>}
        </form>
        <div className={styles.voiceSettings}>
          <label htmlFor="butler-voice-timeout">Pause Voice after</label>
          <select id="butler-voice-timeout" aria-label="Pause Voice after" value={voiceSettings?.autoPauseSeconds ?? 20} disabled={!voiceSettings || voiceSettingsBusy} onChange={(event) => void saveVoiceSettings(Number(event.target.value) as VoicePauseSeconds)}>
            {[10, 20, 30, 60].map((seconds) => <option key={seconds} value={seconds}>{seconds} seconds</option>)}
          </select>
          <small>Shared by all projects. Applies to the next connection.</small>
          {voiceSettingsBusy && <p role="status">Loading Voice timeout…</p>}
          {voiceSettingsStatus && <p role="status">{voiceSettingsStatus}</p>}
          {voiceSettingsError && <p role="alert">{voiceSettingsError}</p>}
        </div>
      </section>}
        {engagement && <ButlerMemory api={api} projectId={engagement.id} coreState={coreState} refreshRevision={memoryRefreshRevision} />}
      <details ref={destinationCatalogRef} className={styles.catalog}><summary>Places I can open</summary><input aria-label="Find a place" placeholder="Find a place" value={destinationQuery} onChange={(event) => setDestinationQuery(event.target.value)} />
        <div className={styles.settingList}>{destinationMatches.length ? destinationMatches.map((entry) => <button key={entry.id} className="button quiet" type="button" onClick={() => { const confirmation = openDestination(entry); setMessages((current) => [...current, { role: "assistant", content: confirmation }]); }}><span>{entry.label}</span><small>{entry.path.includes("{project_id}") ? "Selected project" : "Open page"}</small></button>) : <p>No place found.</p>}</div>
      </details>
      <details ref={catalogRef} className={styles.catalog}><summary>Settings I can help with</summary><input aria-label="Find a setting" placeholder="Find a setting" value={query} onChange={(event) => setQuery(event.target.value)} />
        <div className={styles.settingList}>{matches.length ? matches.map((entry) => <button key={entry.id} className="button quiet" type="button" onClick={() => openSelectedSetting(entry)}><span>{entry.label}</span><small>{directSettingIds.has(entry.id) ? "Direct actions available · open controls" : "Open controls"}</small></button>) : <p>No setting found.</p>}</div>
        <div className={styles.themes}>{THEME_OPTIONS.map((theme) => <button key={theme.value} className="button quiet" type="button" aria-pressed={preference === theme.value} onClick={() => setPreference(theme.value)}>{theme.label}</button>)}</div>
      </details>
      </section>
    </div>
    {view === "chat" && messages.length > 0 && hasNewerMessages && <div className={styles.scrollJumpRail}><button className="chat-scroll-jump" type="button" aria-label="Scroll to latest message" title="Scroll to latest message" onClick={() => {
      followsLatest.current = true;
      const surface = scrollSurface();
      if (surface === body.current) {
        const lastMessage = transcript.current?.lastElementChild;
        if (lastMessage && typeof lastMessage.scrollIntoView === "function") lastMessage.scrollIntoView({ block: "end" });
        else if (surface) surface.scrollTop = surface.scrollHeight;
      } else if (surface) surface.scrollTop = surface.scrollHeight;
      setHasNewerMessages(false);
    }}><ChevronDown size={16} aria-hidden="true" /></button></div>}
    {butlerDrafts.length > 0 && <section className={styles.contextPack} aria-label="Butler context pack">
      <div className={styles.contextHeading}><span>{butlerDrafts.length} selected context {butlerDrafts.length === 1 ? "item" : "items"} for Butler</span><button type="button" className="icon-button subtle" aria-label="Clear Butler context" title="Clear Butler context" disabled={busy} onClick={() => context?.clearButlerDrafts()}><X size={16} /></button></div>
      <div className={styles.contextItems}>{butlerDrafts.map((item, index) => <div className={styles.contextItem} key={`${item.source.kind}:${item.source.id ?? ""}:${index}`}><details><summary>{item.source.label}{item.truncated ? " · shortened" : ""}</summary><p>{item.text}</p></details><button type="button" className="icon-button subtle" aria-label={`Remove ${item.source.label} from Butler context`} title={`Remove ${item.source.label}`} disabled={busy} onClick={() => context?.removeButlerDraft(index)}><X size={16} /></button></div>)}</div>
    </section>}
    {context?.butlerDraftNotice && <div className={styles.contextNotice} role="status"><span>{context.butlerDraftNotice}</span><button type="button" className="icon-button subtle" aria-label="Dismiss Butler context notice" title="Dismiss Butler context notice" onClick={() => context.clearButlerDraftNotice()}><X size={16} /></button></div>}
    {showTurnStatus && <section className={styles.turnStatus} data-state={pendingStatusError ? "error" : needsReview ? "attention" : "working"} aria-label="Butler activity">
      <span className={styles.turnStatusDot} aria-hidden="true" />
      <div className={styles.turnStatusText}>
        <strong role="status" aria-live="polite">{turnStatusLabel}</strong>
        <small>{Number.isFinite(turnStartedAt) && activeBusy ? `Working for ${formatButlerDuration(clock - turnStartedAt)}` : ""}
          {activeBusy && <>{Number.isFinite(turnStartedAt) ? " · " : ""}{lastSignalAt
            ? `Last signal ${formatButlerDuration(clock - lastSignalAt)} ago`
            : "Waiting for a Core update"}</>}
          {pendingStatusError && <span> Conversation remains available. Retry status to check it.</span>}
        </small>
        {turnFeedback && turnFeedback.recent.length > 1 && <details className={styles.turnSteps}><summary>Recent steps</summary><ol>{turnFeedback.recent.map((step, index) => <li key={`${step.at}-${index}`}>{step.label}</li>)}</ol></details>}
      </div>
      {needsReview && engagement && sessionId && <button className="button quiet" type="button" onClick={() => { setHidden(true); onNavigate(`/projects/${encodeURIComponent(engagement.id)}/workbench?view=chat&session=${encodeURIComponent(sessionId)}`); }}>Review</button>}
      {pendingStatusError && <button className="button quiet" type="button" onClick={() => pendingPoll.current()}>Retry status</button>}
    </section>}
    {pendingImages.length > 0 && <div className={styles.imageTray} role="list" aria-label="Butler image attachments">{pendingImages.map((block, index) => <div className={styles.imageItem} role="listitem" key={`${block.artifactId}-${index}`}>{api && <AuthenticatedChatImage api={api} block={block} className={styles.attachedImage} />}<span title={block.alt}>{block.alt}</span><button className="icon-button subtle" type="button" aria-label={`Remove ${block.alt ?? "image"}`} disabled={activeBusy} onClick={() => setPendingImages((current) => current.filter((_, imageIndex) => imageIndex !== index))}><X size={16} /></button></div>)}</div>}
    {uploadingImage && <p className={styles.imageStatus} role="status">Attaching image…</p>}
    {pendingLibraryAdd?.projectId === engagement?.id && <section className={styles.libraryHandoff} aria-label="Add to shared Library">
      <div><strong>Add to shared Library</strong><small>Choose a file from this device. Core will make it available to every project after confirmation.</small></div>
      <input ref={libraryInput} type="file" hidden accept={ACCEPTED_LIBRARY_FILES} aria-label="Choose shared Library file" onChange={(event) => void uploadLibraryFile(event)} />
      <div className={styles.libraryHandoffActions}>
        <button className="button quiet" type="button" disabled={uploadingLibrary || coreState !== "online"} onClick={() => libraryInput.current?.click()}><Upload size={16} aria-hidden="true" />{uploadingLibrary ? "Adding…" : "Choose file"}</button>
        <button className="button quiet" type="button" disabled={uploadingLibrary} onClick={() => { setPendingLibraryAdd(undefined); setMessages((current) => [...current, {role: "assistant", content: "No file was added to the shared Library."}]); }}>Cancel</button>
      </div>
    </section>}
    {pendingEvidenceUpload && pendingEvidenceUpload.projectId === engagement?.id && pendingEvidenceUpload.sourceMessageId && <ButlerEvidenceUpload
      key={pendingEvidenceUpload.sourceMessageId}
      projectId={pendingEvidenceUpload.projectId}
      currentProjectId={() => activeImageProject.current}
      onSaved={(observed, refreshFailed) => {
        if (!pendingEvidenceUpload) return;
        appendBrowserActionOutcome(pendingEvidenceUpload, `Stored ${observed.title} in Core's project Evidence at revision ${observed.revision} (SHA-256 ${observed.sha256}). [Open evidence](${resourcePath(observed.engagementId, "evidence", observed.id)}). Butler did not receive the file bytes.${refreshFailed ? " The Evidence list could not refresh; open Evidence to reload it." : ""}`);
        setPendingEvidenceUpload(undefined);
      }}
      onCancel={() => {
        if (!pendingEvidenceUpload) return;
        appendBrowserActionOutcome(pendingEvidenceUpload, "No evidence file was selected or stored.");
        setPendingEvidenceUpload(undefined);
      }}
      onUncertain={(message) => {
        if (!pendingEvidenceUpload) return;
        appendBrowserActionOutcome(pendingEvidenceUpload, message);
        setPendingEvidenceUpload(undefined);
      }}
    />}
    {pendingWorkspaceUpload && pendingWorkspaceUpload.projectId === engagement?.id && <section className={styles.libraryHandoff} aria-label="Upload to project workspace">
      <div><strong>Upload to project workspace</strong><small>Choose a file on this device for /workspace/{pendingWorkspaceUpload.directory}. Core will confirm the saved bytes.</small></div>
      <input ref={workspaceInput} type="file" hidden aria-label="Choose project workspace file" onChange={(event) => void uploadWorkspaceFile(event)} />
      <div className={styles.libraryHandoffActions}>
        <button className="button quiet" type="button" disabled={uploadingWorkspace || coreState !== "online"} onClick={() => workspaceInput.current?.click()}><Upload size={16} aria-hidden="true" />{uploadingWorkspace ? "Uploading…" : "Choose file"}</button>
        <button className="button quiet" type="button" onClick={() => {
          if (uploadingWorkspace) workspaceUploadAbort.current?.abort();
          else {setPendingWorkspaceUpload(undefined); setMessages((current) => [...current, {role: "assistant", content: "No workspace file was selected or uploaded."}]);}
        }}>{uploadingWorkspace ? "Cancel upload" : "Cancel"}</button>
      </div>
    </section>}
    {pendingNotebookIndex?.kind === "notebook_index_connect" && pendingNotebookIndex.projectId === engagement?.id && pendingNotebookIndex.sourceMessageId && <section className={styles.libraryHandoff} aria-label="Connect Notebook file index">
      <div><strong>Connect Notebook file index</strong><small>Choose a SQLite index on the Nebula host. Core checks it belongs to this project; Butler receives no host path.</small></div>
      <div className={styles.libraryHandoffActions}>
        {api && <HostFolderPicker api={api} purpose="index" onSelect={(path) => {void connectNotebookIndex(path);}} />}
        <button className="button quiet" type="button" disabled={connectingNotebookIndex} onClick={() => {appendNotebookIndexOutcome(pendingNotebookIndex, "No Notebook file index was selected."); setPendingNotebookIndex(undefined);}}>Cancel</button>
      </div>
      {connectingNotebookIndex && <span role="status">Checking selected file index in Core…</span>}
    </section>}
    {pendingLinkedProject && pendingLinkedProject.sourceProjectId === engagement?.id && pendingLinkedProject.sourceMessageId && <section className={styles.libraryHandoff} aria-label="Choose linked project folder">
      <div><strong>Choose project folder</strong><small>Browse a folder on the Nebula host. This becomes the new project's shared workspace. Host command access still needs consent.</small></div>
      <div className={styles.libraryHandoffActions}>
        <HostFolderPicker api={coreState === "online" && !creatingLinkedProject ? api : undefined} onSelect={path => {void createLinkedProject(path);}} />
        <button className="button quiet" type="button" disabled={creatingLinkedProject} onClick={() => {appendBrowserActionOutcome(pendingLinkedProject, "No project was created. The host folder was not linked."); setPendingLinkedProject(undefined);}}>Cancel</button>
      </div>
      {creatingLinkedProject && <span role="status">Creating linked project in Core…</span>}
    </section>}
    {pendingProjectRelink && pendingProjectRelink.sourceProjectId === engagement?.id && pendingProjectRelink.sourceMessageId && <section className={styles.libraryHandoff} aria-label="Relink project folder">
      <div><strong>Relink {pendingProjectRelink.name}</strong><small>Choose a folder on the Nebula host. Core will check that no existing work still uses the old folder.</small></div>
      <div className={styles.libraryHandoffActions}>
        <HostFolderPicker api={coreState === "online" && !relinkingProject ? api : undefined} onSelect={path => {void relinkProject(path);}} />
        <button className="button quiet" type="button" disabled={relinkingProject} onClick={() => {appendBrowserActionOutcome(pendingProjectRelink, `${pendingProjectRelink.name} was not relinked. No folder changed.`); setPendingProjectRelink(undefined);}}>Cancel</button>
      </div>
      {relinkingProject && <span role="status">Checking project folder in Core…</span>}
    </section>}
    {imageHelp && !imageInputSupported && <p id="butler-image-help" className={styles.imageStatus}>This Butler model cannot receive images. <button type="button" className="button quiet" onClick={() => void openImageRuntimeChoice()}>Open conversation to choose an image-capable model</button></p>}
    <form className={styles.composer} onSubmit={(event) => { event.preventDefault(); void send(input.current?.value ?? draft); }}>
      <input ref={imageInput} type="file" hidden multiple accept="image/png,image/jpeg,image/webp" aria-label="Choose images for Butler" onChange={(event) => { const files = [...(event.target.files ?? [])]; event.target.value = ""; void attachImageFiles(files); }} />
      <button className="icon-button subtle" type="button" aria-label="Attach image to Butler" title="Attach image to Butler" aria-describedby={imageHelp && !imageInputSupported ? "butler-image-help" : undefined} disabled={!api || !engagement || activeBusy || loadingHistory || uploadingImage || !pendingReady} onClick={() => imageInputSupported ? imageInput.current?.click() : setImageHelp(true)}><Image size={18} /></button>
      <textarea ref={input} aria-label="Ask Butler a question" value={draft} onChange={(event) => setDraft(event.target.value)} onPaste={pasteImages} onKeyDown={(event) => { if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) { event.preventDefault(); void send(event.currentTarget.value); } }} rows={2} maxLength={4000} placeholder="Message Butler…" disabled={activeBusy || loadingHistory || !pendingReady} />
      {activeBusy && canStopActive ? <button className="icon-button subtle" type="button" aria-label="Stop assistant response" title="Stop Butler’s response" onClick={() => void stop()}><Square size={18} /></button> : activeBusy ? null : <button className="icon-button subtle" type="submit" aria-label="Send to Butler" disabled={(!draft.trim() && !pendingImages.length) || (pendingImages.length > 0 && (!api || !engagement || !runtime || !imageInputSupported)) || (!pendingImages.length && !isSettingNavigationRequest(draft) && !projectModeChange(draft) && !themeChange(draft) && !destinationToOpen(draft) && (!engagement || !runtime)) || loadingHistory || uploadingImage || !pendingReady}><Send size={18} /></button>}
    </form>
    </div>
  </> : null;

  const attentionLabel = `${attentionCount} open incident${attentionCount === 1 ? "" : "s"} ${attentionCount === 1 ? "needs" : "need"} attention in the selected project`;
  return <NebulaAssistantLauncherContext.Provider value={{ opened: open, visible: open && !hidden, attentionCount, toggle: () => { if (open) setHidden((value) => !value); else { setOpen(true); setHidden(false); } } }}>
    {children}
    {!children && (!open || hidden) ? <button className={styles.launcher} type="button" aria-label={`${hidden ? "Show Butler" : "Open Butler"}${attentionCount ? `; ${attentionLabel}` : ""}`} title={attentionCount ? attentionLabel : "Butler"} onClick={() => { setOpen(true); setHidden(false); }}><img src="/nebula-logo.svg" width="22" height="36" alt="" aria-hidden="true" />{attentionCount > 0 && <span className={styles.launcherAttention} aria-hidden="true" />}</button> : null}
    {panelContent && createPortal(panelContent, document.body)}
    {voiceStart && api && engagement && createPortal(<NebulaVoice api={api} project={{ id: engagement.id, name: engagement.name }} initialMicrophone={voiceStart.microphone} onDelegate={delegateVoice} onExit={() => { setVoiceStart(undefined); setHidden(false); }} />, document.body)}
    {pendingReportTransform?.sourceMessageId && api && engagement?.id === pendingReportTransform.projectId && createPortal(<AIWritingDialog
      api={api}
      engagementId={pendingReportTransform.projectId}
      providers={providers}
      harnesses={harnesses}
      purpose="report_section"
      title={`Transform ${pendingReportTransform.note.title} for the report`}
      description="Choose a permitted runtime, review and edit its draft, then save this section to the selected report. The source note remains unchanged."
      sourceLabel={pendingReportTransform.note.title}
      sourceText={pendingReportTransform.note.body.slice(0, 100_000)}
      initialInstruction={pendingReportTransform.instruction}
      applyLabel="Save report section"
      reviewNotice="Core saves this section only after you review and select Save report section"
      onApply={saveReviewedReportTransform}
      onClose={() => {
        const action = pendingReportTransform;
        if (!action || activeImageProject.current !== action.projectId) return;
        appendBrowserActionOutcome(action, action.saveAttempted
          ? "The report section was not confirmed. Inspect the exact report in Core before asking Butler to retry."
          : "No reviewed report section was saved. The source note and report are unchanged.");
        setPendingReportTransform(undefined);
      }}
    />, document.body)}
  </NebulaAssistantLauncherContext.Provider>;
}
