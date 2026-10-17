import { useEffect, useRef, useState, type ClipboardEvent as ReactClipboardEvent, type CSSProperties, type ReactNode } from "react";
import { GripHorizontal, Image, Minimize2, Send, Settings2, Square, X } from "lucide-react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { createPortal } from "react-dom";
import { defaultModelRuntime } from "../api/runtimeDefaults";
import { logDiagnostic } from "../diagnostics";
import type { ToolSharingRuntime } from "../api/toolSharingConsent";
import { useWorkspace } from "../state/WorkspaceContext";
import { useOptionalWorkbenchDrafts } from "../state/WorkbenchDraftContext";
import { createHashedSelectionAttachment } from "./selection/selectionActions";
import { THEME_OPTIONS, useTheme, type ThemePreference } from "../state/ThemeContext";
import { readStorage, writeStorage } from "../state/browserStorage";
import { settingCatalog, type SettingCatalogEntry } from "../settingsCatalog";
import { assistantDestinations, assistantDestinationPath, destinationToOpen, type AssistantDestination } from "../assistantDestinations";
import { REASONING_EFFORTS, type ButlerVoiceSettings, type ChatAssistantConfig, type ChatContentBlock, type ChatMessage, type VoicePauseSeconds } from "../api/types";
import { assistantTurnOptions, assistantWorkReceipt, headlessSessionChange, isSettingNavigationRequest, isThemeChangeCommand, projectModeChange, projectScopeChange, settingClientAction, settingNavigationCandidates, settingToOpen, themeChange, type ProjectMode, type SettingClientAction } from "./nebulaAssistantActions";
import { saveAssistantProjectScope } from "./assistantSettingsMutations";
import { useConfirmation } from "./DialogSystem";
import { NebulaAssistantLauncherContext } from "./NebulaAssistantLauncher";
import { MicrophoneIcon, NebulaVoice } from "./NebulaVoice";
import { ButlerMemory } from "./ButlerMemory";
import { ButlerOversight } from "./ButlerOversight";
import { AuthenticatedChatImage } from "./AuthenticatedChatImage";
import { useButlerOverview } from "./useButlerOverview";
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

export function NebulaControlAssistant({ children, onOpenSetting, onNavigate, onCloseSetting, onSavedSetting }: {
  children?: ReactNode;
  onOpenSetting: (entry: SettingCatalogEntry) => void;
  onNavigate: (path: string) => void;
  onCloseSetting?: () => void;
  onSavedSetting?: (entry: SettingCatalogEntry, status: string, mode?: "docker" | "host") => void;
}) {
  const { api, coreState, engagement, providers, harnesses } = useWorkspace();
  const context = useOptionalWorkbenchDrafts();
  const butlerDrafts = context?.butlerDrafts ?? [];
  const { preference, setPreference } = useTheme();
  const confirm = useConfirmation();
  const [open, setOpen] = useState(false);
  const [hidden, setHidden] = useState(false);
  const [query, setQuery] = useState("");
  const [destinationQuery, setDestinationQuery] = useState("");
  const [draft, setDraft] = useState("");
  const [pendingImages, setPendingImages] = useState<ChatContentBlock[]>([]);
  const [uploadingImage, setUploadingImage] = useState(false);
  const [imageHelp, setImageHelp] = useState(false);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [memoryRefreshRevision, setMemoryRefreshRevision] = useState(0);
  const [suggestions, setSuggestions] = useState<SettingCatalogEntry[]>([]);
  const [sessionId, setSessionId] = useState<string>();
  const [sessionRuntime, setSessionRuntime] = useState<{ kind: "provider" | "harness"; id: string; model: string }>();
  const [sessionConfig, setSessionConfig] = useState<ChatAssistantConfig>();
  const [settingsOpen, setSettingsOpen] = useState(false);
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
  const [error, setError] = useState("");
  const [voiceStart, setVoiceStart] = useState<{ microphone?: Promise<MediaStream> }>();
  const [position, setPosition] = useState(() => ({ x: Math.max(12, window.innerWidth - 580), y: 72 }));
  const drag = useRef<{ x: number; y: number; left: number; top: number } | undefined>(undefined);
  const abort = useRef<AbortController | undefined>(undefined);
  const activeTurn = useRef<{ turnId?: string; harnessTurnId?: string }>({});
  const input = useRef<HTMLTextAreaElement>(null);
  const imageInput = useRef<HTMLInputElement>(null);
  const activeImageProject = useRef(engagement?.id);
  activeImageProject.current = engagement?.id;
  const panel = useRef<HTMLDivElement>(null);
  const body = useRef<HTMLDivElement>(null);
  const transcript = useRef<HTMLDivElement>(null);
  const lastScroll = useRef({ visible: false, messages: [] as ChatMessage[], attentionCount: 0, loadingHistory: false });
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

  useEffect(() => () => { abort.current?.abort(); }, [engagement?.id]);

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
    setPendingImages([]);
    setUploadingImage(false);
    setImageHelp(false);
    setSuggestions([]);
    setError("");
    setLoadingHistory(false);
    if (!storageKey || !api) return;
    setLoadingHistory(true);
    let current = true;
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
      if (!current || !history) return;
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
    const visible = open && !hidden;
    const previous = lastScroll.current;
    if (visible) {
      const mobile = window.matchMedia("(max-width: 760px), (max-height: 500px) and (pointer: coarse)").matches;
      if (mobile && body.current) {
        if (attentionCount > 0 && (!previous.visible || attentionCount > previous.attentionCount)) body.current.scrollTop = 0;
        else if (!loadingHistory && (previous.messages !== messages || !previous.visible || previous.loadingHistory)) {
          const lastMessage = transcript.current?.lastElementChild;
          if (lastMessage && typeof lastMessage.scrollIntoView === "function") lastMessage.scrollIntoView({ block: "end" });
          else body.current.scrollTop = body.current.scrollHeight;
        }
      } else if (transcript.current) transcript.current.scrollTop = transcript.current.scrollHeight;
    }
    lastScroll.current = { visible, messages, attentionCount, loadingHistory };
  }, [messages, open, hidden, attentionCount, loadingHistory]);

  useEffect(() => {
    if (!open || hidden) return;
    if (window.matchMedia("(max-width: 760px), (max-height: 500px) and (pointer: coarse)").matches) return;
    input.current?.focus();
  }, [open, hidden]);

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

  const openSettings = async () => {
    if (settingsOpen) { setSettingsOpen(false); return; }
    setSettingsOpen(true);
    setVoiceSettingsStatus("");
    void loadVoiceSettings();
    setSettingsError("");
    setSettingsStatus("");
    let config = sessionConfig;
    if (api && engagement) {
      setSettingsBusy(true);
      try {
        // The panel can open before the background conversation restore finishes.
        // Read Core directly so saved settings never appear as first-turn defaults.
        let savedSessionId = sessionId;
        if (!savedSessionId) {
          const restored = await api.getControlAssistantSession(engagement.id);
          if (restored?.engagementId === engagement.id) {
            savedSessionId = restored.id;
            setSessionId(restored.id);
          }
        }
        if (savedSessionId) {
          const saved = await api.getChatAssistantConfig(savedSessionId);
          config = saved.settings;
          setSessionConfig(config);
          setSettingsRevision(saved.revision);
        }
      } catch (caught) {
        void logDiagnostic({ level: "warning", eventCode: "interface.nebula_assistant.settings_open_failed", message: "Butler settings could not be loaded.", outcome: "failure", stage: "settings", retryable: true, exception: caught });
        setSettingsError(caught instanceof Error ? caught.message : "Could not load Butler settings. Retry by reopening settings.");
      } finally { setSettingsBusy(false); }
    }
    setEffortDraft(config?.backend === "provider" ? config.reasoningEffort ?? "" : config?.harnessReasoningEffort ?? "");
    setCyberDraft(config?.harnessCyberAccessProgram ?? "automatic");
  };

  useEffect(() => {
    const openVoiceSettings = () => {
      setOpen(true);
      setHidden(false);
      if (!settingsOpen) void openSettings();
      else void loadVoiceSettings();
    };
    window.addEventListener("nebula:open-butler-settings", openVoiceSettings);
    return () => window.removeEventListener("nebula:open-butler-settings", openVoiceSettings);
  });

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
      setSettingsStatus(sessionId ? "Saved. Applies to Butler’s next message." : "Saved for Butler’s first message.");
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
      panel.current?.style.setProperty("--panel-visual-height", `${window.visualViewport?.height ?? window.innerHeight}px`);
      if (window.matchMedia("(max-width: 760px), (max-height: 500px) and (pointer: coarse)").matches) return;
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
  }, [open, hidden]);

  const stop = async () => {
    if (!api) return;
    try {
      const { turnId, harnessTurnId } = activeTurn.current;
      if (harnessTurnId) await api.stopHarnessTurn(harnessTurnId);
      else if (turnId) await api.cancelChatTurn(turnId);
      else if (sessionId) {
        const pending = await api.getPendingChatTurn(sessionId);
        if (pending?.harnessTurnId) await api.stopHarnessTurn(pending.harnessTurnId);
        else if (pending) await api.cancelChatTurn(pending.id);
      }
      abort.current?.abort();
      setBusy(false);
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

  const applyClientAction = async (action: SettingClientAction): Promise<string> => {
    if (action.kind === "device_theme") return applyDeviceTheme(action.theme);
    if (action.kind === "project_execution_mode") return applyProjectMode(action);
    if (action.kind === "project_scope") return applyProjectScope(action.changes);
    if (action.kind === "headless_session") return applyHeadlessSession(action.action);
    if (action.kind === "navigate") return openDestination(action.destination);
    onOpenSetting(action.setting);
    setHidden(true);
    return `Opened ${action.setting.label}.`;
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
      setError(caught instanceof Error ? caught.message : "Could not open Butler's model selector. Retry after reconnecting.");
    }
  };

  const send = async (currentText = draft) => {
    const prompt = currentText.trim() || (pendingImages.length ? "Attached image" : "");
    if (!prompt || busy || loadingHistory || uploadingImage) return;
    const hasImages = pendingImages.length > 0;
    if (hasImages && !imageInputSupported) { setImageHelp(true); return; }
    setSuggestions([]);
    const requestedTheme = themeChange(prompt);
    if (!hasImages && requestedTheme) {
      setDraft("");
      setError("");
      setBusy(true);
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
      onOpenSetting(setting);
      setHidden(true);
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
        catalogRef.current?.setAttribute("open", "");
        destinationCatalogRef.current?.setAttribute("open", "");
      }
      return;
    }
    if (!api || !engagement || !runtime) return;
    let effectiveSessionId: string;
    let effectiveConfig: ChatAssistantConfig | undefined;
    try {
      const saved = await getSavedSessionForTurn();
      effectiveSessionId = saved.session.id;
      effectiveConfig = saved.config;
    } catch (caught) {
      void logDiagnostic({ level: "error", eventCode: "interface.nebula_assistant.settings_refresh_failed", message: "The assistant's next-turn settings could not be refreshed.", outcome: "failure", stage: "settings", retryable: true, exception: caught });
      setError(caught instanceof Error ? caught.message : "Could not load Butler settings. Retry after reconnecting.");
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
      return;
    }
    const controller = new AbortController();
    abort.current = controller;
    activeTurn.current = {};
    setBusy(true);
    setError("");
    const outgoingUser: ChatMessage = { role: "user", content: prompt,
      ...(hasImages ? {contentBlocks: [{type: "text" as const, text: prompt}, ...pendingImages]} : {}),
    };
    setMessages((current) => [...current, outgoingUser]);
    setDraft("");
    const appliedCallIds = new Set<string>();
    const confirmations: string[] = [];
    const workReceipts: { url: string; content: string }[] = [];
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
        const action = settingClientAction(event, engagement.id);
        if (action && event.type === "tool_completed" && !appliedCallIds.has(event.toolCallId)) {
          appliedCallIds.add(event.toolCallId);
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
        setPendingImages([]);
        if (contextAttachments.length) context?.consumeButlerDrafts(butlerDrafts);
        confirmations.push(...workReceipts.filter(({ url }) => !result.message.content.includes(url)).map(({ content }) => content));
        if (result.message.id && confirmations.length && actionOutcomesKey) {
          const outcomes = readActionOutcomes(actionOutcomesKey);
          outcomes[result.message.id] = confirmations.join("\n\n");
          writeStorage(actionOutcomesKey, JSON.stringify(Object.fromEntries(Object.entries(outcomes).slice(-50))));
        }
        setMessages((current) => [...current, result.message, ...confirmations.map((content) => ({ role: "assistant" as const, content }))]);
        setMemoryRefreshRevision((current) => current + 1);
      }
      else if (!controller.signal.aborted) setError("The response is still running. Reopen this assistant to check its saved conversation.");
    } catch (caught) {
      if (!controller.signal.aborted) {
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
          } catch {
            setDraft(prompt);
            setError("Butler's send failed, and its saved state could not be checked. Reopen this conversation before retrying.");
          }
        } else {
          setDraft(prompt);
          setError(caught instanceof Error ? caught.message : "The assistant could not answer. Try again.");
        }
      }
    } finally { setBusy(false); }
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
    confirmations.push(...workReceipts.filter(({ url }) => !result.message!.content.includes(url)).map(({ content }) => content));
    setMessages((current) => [...current, result.message!, ...confirmations.map((content) => ({ role: "assistant" as const, content }))]);
    setMemoryRefreshRevision((current) => current + 1);
    return [result.message.content, ...confirmations].filter(Boolean).join("\n\n");
  };

  const matches = query.trim() ? settingCatalog.filter((entry) =>
    [entry.label, entry.description, ...entry.keywords].join(" ").toLowerCase().includes(query.toLowerCase())) : settingCatalog;
  const destinationMatches = destinationQuery.trim() ? assistantDestinations.filter((entry) =>
    [entry.label, ...entry.aliases].join(" ").toLowerCase().includes(destinationQuery.toLowerCase())) : assistantDestinations;
  const directSettingIds = new Set(["settings.appearance", "settings.providers", "settings.harnesses", "settings.follow-up", "settings.network-scope", "settings.automation-runtime"]);
  const panelContent = open ? <div ref={panel} id="nebula-assistant-panel" className={styles.panel} role="dialog" aria-label="Butler" data-selection-recipient="butler" style={{ "--panel-left": `${position.x}px`, "--panel-top": `${position.y}px` } as CSSProperties} hidden={hidden}>
    <div className={styles.header}>
      <button className={`icon-button subtle ${styles.dragButton}`} type="button" aria-label="Move Butler" title="Drag to move · arrow keys to reposition" onPointerDown={(event) => { if (event.button !== 0) return; event.currentTarget.setPointerCapture(event.pointerId); drag.current = { x: event.clientX, y: event.clientY, left: position.x, top: position.y }; }} onPointerMove={(event) => { if (drag.current) move(drag.current.left + event.clientX - drag.current.x, drag.current.top + event.clientY - drag.current.y); }} onPointerUp={() => { drag.current = undefined; }} onKeyDown={(event) => { const step: Record<string, [number, number]> = { ArrowLeft: [-24, 0], ArrowRight: [24, 0], ArrowUp: [0, -24], ArrowDown: [0, 24] }; const offset = step[event.key]; if (offset) { event.preventDefault(); move(position.x + offset[0], position.y + offset[1]); } }}><GripHorizontal size={18} /></button>
      <div className={styles.identity}><strong>Butler</strong><small>{engagement?.name ?? "Choose a project"}</small></div>
      <button className={`icon-button subtle ${styles.voiceButton}`} type="button" aria-label="Start Butler Voice" title="Start Butler Voice" disabled={!api || !engagement || !runtime || busy || loadingHistory} onClick={startVoice}><MicrophoneIcon size={18} /><span className={styles.voiceLabel}>Start Voice</span></button>
      <button className={`icon-button subtle ${styles.hideButton}`} type="button" aria-label="Hide Butler" title="Hide Butler" onClick={() => setHidden(true)}><Minimize2 size={18} /></button>
      <button className={`icon-button subtle ${styles.settingsButton}`} type="button" aria-label="Butler settings" aria-expanded={settingsOpen} aria-controls="butler-settings" title="Butler settings" disabled={!api || busy} onClick={() => void openSettings()}><Settings2 size={18} /></button>
      <button className={`icon-button subtle ${styles.closeButton}`} type="button" aria-label="Close Butler" title="Close Butler; keep history" onClick={() => { setOpen(false); setHidden(false); setSettingsOpen(false); }}><X size={18} /></button>
    </div>
    <div ref={body} className={styles.body}>
      {settingsOpen && <section id="butler-settings" className={styles.ownSettings} aria-label="Butler settings">
        <div className={styles.settingsHeading}><strong>Butler settings</strong><span>{selectedSettingsRuntime?.model ?? ""}</span></div>
        <form onSubmit={(event) => { event.preventDefault(); void saveSettings(); }}>
          {selectedSettingsRuntime?.kind === "provider" && <label>Effort<select aria-label="Butler effort" value={effortDraft} disabled={settingsBusy} onChange={(event) => setEffortDraft(event.target.value)}><option value="">Model default</option>{REASONING_EFFORTS.map((item) => <option key={item} value={item}>{item === "none" ? "None" : item}</option>)}</select></label>}
          {selectedSettingsRuntime?.kind === "harness" && <label>Effort<select aria-label="Butler effort" value={effortDraft} disabled={settingsBusy || !selectedModelOptions?.reasoningEfforts.length} onChange={(event) => setEffortDraft(event.target.value)}><option value="">Harness default</option>{effortDraft && !selectedModelOptions?.reasoningEfforts.some((item) => item.id === effortDraft) && <option value={effortDraft}>{effortDraft} · saved</option>}{selectedModelOptions?.reasoningEfforts.map((item) => <option key={item.id} value={item.id} title={item.description || undefined}>{item.label}</option>)}</select></label>}
          {cyberChoices.length > 0 && <label>Cyber access<select aria-label="Butler cyber access" value={cyberDraft} disabled={settingsBusy} onChange={(event) => setCyberDraft(event.target.value as ChatAssistantConfig["harnessCyberAccessProgram"])}><option value="automatic">Codex automatic</option>{cyberChoices.map((choice) => <option key={choice} value={choice}>{choice === "standard" ? "Standard" : choice === "daybreakBlue" ? "Daybreak Blue" : "Daybreak Red"}</option>)}</select><small>Available choices come from this Codex account and model.</small></label>}
          {selectedSettingsRuntime?.kind === "harness" && !selectedModelOptions?.reasoningEfforts.length && <small>This harness does not advertise effort choices.</small>}
          <div className={styles.settingsActions}><button className="button quiet" type="submit" disabled={settingsBusy || busy}>Save</button>{sessionId && engagement && <button className="button quiet" type="button" onClick={() => { setHidden(true); onNavigate(`/projects/${encodeURIComponent(engagement.id)}/workbench?view=chat&session=${encodeURIComponent(sessionId)}`); }}>Full Assistant settings</button>}</div>
          {settingsBusy && <p role="status">Loading Butler settings…</p>}
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
      {!hidden && engagement && <div className={styles.butlerWorkspace}>
        <ButlerOversight key={engagement.id} api={api} projectId={engagement.id} coreState={coreState} compact onIncidentChanged={() => void refreshAttention()} onNavigate={(path) => { onNavigate(path); setHidden(true); }} />
        <ButlerMemory api={api} projectId={engagement.id} coreState={coreState} refreshRevision={memoryRefreshRevision} />
      </div>}
      <div ref={transcript} className={styles.transcript} role="log" aria-label="Butler conversation" data-selection-source-kind="conversation" data-selection-source-id={sessionId} data-selection-source-label="Butler conversation">{messages.map((message, index) => <div key={`${index}-${message.id ?? ""}`} className={message.role === "user" ? styles.question : styles.answer}><ReactMarkdown remarkPlugins={[remarkGfm]} components={{ table: ({ children, ...props }) => <div className={styles.tableScroll} role="region" aria-label="Butler response table" tabIndex={0}><table {...props}>{children}</table></div>, a: ({ href, children, ...props }) => {
        const setting = settingCatalog.find((entry) => href === `/settings#${entry.target}`);
        const destination = assistantDestinations.find((entry) => href === assistantDestinationPath(entry, engagement?.id));
        const workLink = engagement && href && /^\/projects\/[^/]+\/workbench\?view=(?:chat&session|missions&mission)=/.test(href)
          && href.startsWith(`/projects/${encodeURIComponent(engagement.id)}/workbench?`) ? href : undefined;
        return setting
          ? <a href={href} onClick={(event) => { event.preventDefault(); onOpenSetting(setting); setHidden(true); }}>{children}</a>
          : destination ? <a href={href} onClick={(event) => { event.preventDefault(); openDestination(destination); }}>{children}</a>
          : workLink ? <a href={workLink} onClick={(event) => { event.preventDefault(); onNavigate(workLink); setHidden(true); }}>{children}</a>
          : <a href={href} {...props}>{children}</a>;
      } }}>{message.content}</ReactMarkdown>{api && message.contentBlocks?.filter((block) => block.type === "image").map((block, imageIndex) => <AuthenticatedChatImage key={`${block.artifactId ?? "image"}-${imageIndex}`} api={api} block={block} className={styles.transcriptImage} />)}</div>)}</div>
      {engagement && runtime && !loadingHistory && <details className={styles.catalog} open={messages.length === 0}>
        <summary>Work I can help with</summary>
        <div className={styles.quickActions} role="group" aria-label="Assistant work requests">
          <button className="button quiet" type="button" onClick={() => { setDraft("What are my assistants and missions doing in this project?"); input.current?.focus(); }}>Check work</button>
          <button className="button quiet" type="button" onClick={() => { setDraft("Start an assistant to "); input.current?.focus(); }}>Start assistant</button>
          <button className="button quiet" type="button" onClick={() => { setDraft("Start a mission to "); input.current?.focus(); }}>Start mission</button>
        </div>
      </details>}
      {suggestions.length > 0 && <div className={styles.settingList} role="group" aria-label="Suggested settings">{suggestions.map((entry) => <button key={entry.id} className="button quiet" type="button" onClick={() => { onOpenSetting(entry); setSuggestions([]); setHidden(true); }}>{entry.label}</button>)}</div>}
      <details ref={destinationCatalogRef} className={styles.catalog}><summary>Places I can open</summary><input aria-label="Find a place" placeholder="Find a place" value={destinationQuery} onChange={(event) => setDestinationQuery(event.target.value)} />
        <div className={styles.settingList}>{destinationMatches.length ? destinationMatches.map((entry) => <button key={entry.id} className="button quiet" type="button" onClick={() => { const confirmation = openDestination(entry); setMessages((current) => [...current, { role: "assistant", content: confirmation }]); }}><span>{entry.label}</span><small>{entry.path.includes("{project_id}") ? "Selected project" : "Open page"}</small></button>) : <p>No place found.</p>}</div>
      </details>
      <details ref={catalogRef} className={styles.catalog}><summary>Settings I can help with</summary><input aria-label="Find a setting" placeholder="Find a setting" value={query} onChange={(event) => setQuery(event.target.value)} />
        <div className={styles.settingList}>{matches.length ? matches.map((entry) => <button key={entry.id} className="button quiet" type="button" onClick={() => { onOpenSetting(entry); setHidden(true); }}><span>{entry.label}</span><small>{directSettingIds.has(entry.id) ? "Direct actions available · open controls" : "Open controls"}</small></button>) : <p>No setting found.</p>}</div>
        <div className={styles.themes}>{THEME_OPTIONS.map((theme) => <button key={theme.value} className="button quiet" type="button" aria-pressed={preference === theme.value} onClick={() => setPreference(theme.value)}>{theme.label}</button>)}</div>
      </details>
      {error && <p role="alert">{error}</p>}
      {loadingHistory && <p role="status">Restoring your saved conversation…</p>}
      {!engagement && <p role="status">Choose a project to start a conversation. Device appearance can still be changed above.</p>}
      {engagement && !runtime && <p role="status">Connect a model provider or harness to ask Nebula.</p>}
    </div>
    {butlerDrafts.length > 0 && <section className={styles.contextPack} aria-label="Butler context pack">
      <div className={styles.contextHeading}><span>{butlerDrafts.length} selected context {butlerDrafts.length === 1 ? "item" : "items"} for Butler</span><button type="button" className="icon-button subtle" aria-label="Clear Butler context" title="Clear Butler context" disabled={busy} onClick={() => context?.clearButlerDrafts()}><X size={16} /></button></div>
      <div className={styles.contextItems}>{butlerDrafts.map((item, index) => <div className={styles.contextItem} key={`${item.source.kind}:${item.source.id ?? ""}:${index}`}><details><summary>{item.source.label}{item.truncated ? " · shortened" : ""}</summary><p>{item.text}</p></details><button type="button" className="icon-button subtle" aria-label={`Remove ${item.source.label} from Butler context`} title={`Remove ${item.source.label}`} disabled={busy} onClick={() => context?.removeButlerDraft(index)}><X size={16} /></button></div>)}</div>
    </section>}
    {context?.butlerDraftNotice && <div className={styles.contextNotice} role="status"><span>{context.butlerDraftNotice}</span><button type="button" className="icon-button subtle" aria-label="Dismiss Butler context notice" title="Dismiss Butler context notice" onClick={() => context.clearButlerDraftNotice()}><X size={16} /></button></div>}
    {pendingImages.length > 0 && <div className={styles.imageTray} role="list" aria-label="Butler image attachments">{pendingImages.map((block, index) => <div className={styles.imageItem} role="listitem" key={`${block.artifactId}-${index}`}>{api && <AuthenticatedChatImage api={api} block={block} className={styles.attachedImage} />}<span title={block.alt}>{block.alt}</span><button className="icon-button subtle" type="button" aria-label={`Remove ${block.alt ?? "image"}`} disabled={busy} onClick={() => setPendingImages((current) => current.filter((_, imageIndex) => imageIndex !== index))}><X size={16} /></button></div>)}</div>}
    {uploadingImage && <p className={styles.imageStatus} role="status">Attaching image…</p>}
    {imageHelp && !imageInputSupported && <p id="butler-image-help" className={styles.imageStatus}>This Butler model cannot receive images. <button type="button" className="button quiet" onClick={() => void openImageRuntimeChoice()}>Open conversation to choose an image-capable model</button></p>}
    <form className={styles.composer} onSubmit={(event) => { event.preventDefault(); void send(input.current?.value ?? draft); }}><input ref={imageInput} type="file" hidden multiple accept="image/png,image/jpeg,image/webp" aria-label="Choose images for Butler" onChange={(event) => { const files = [...(event.target.files ?? [])]; event.target.value = ""; void attachImageFiles(files); }} /><button className="icon-button subtle" type="button" aria-label="Attach image to Butler" title="Attach image to Butler" aria-describedby={imageHelp && !imageInputSupported ? "butler-image-help" : undefined} disabled={!api || !engagement || busy || loadingHistory || uploadingImage} onClick={() => imageInputSupported ? imageInput.current?.click() : setImageHelp(true)}><Image size={18} /></button><textarea ref={input} aria-label="Ask Butler a question" value={draft} onChange={(event) => setDraft(event.target.value)} onPaste={pasteImages} onKeyDown={(event) => { if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) { event.preventDefault(); void send(event.currentTarget.value); } }} rows={2} maxLength={4000} placeholder="Ask about work, start an assistant or mission, or change a setting…" disabled={busy || loadingHistory} />{busy ? <button className="icon-button subtle" type="button" aria-label="Stop assistant response" onClick={() => void stop()}><Square size={18} /></button> : <button className="icon-button subtle" type="submit" aria-label="Send to Butler" disabled={(!draft.trim() && !pendingImages.length) || (pendingImages.length > 0 && (!api || !engagement || !runtime || !imageInputSupported)) || (!pendingImages.length && !isSettingNavigationRequest(draft) && !projectModeChange(draft) && !themeChange(draft) && !destinationToOpen(draft) && (!engagement || !runtime)) || loadingHistory || uploadingImage}><Send size={18} /></button>}</form>
  </div> : null;

  const attentionLabel = `${attentionCount} open incident${attentionCount === 1 ? "" : "s"} ${attentionCount === 1 ? "needs" : "need"} attention in the selected project`;
  return <NebulaAssistantLauncherContext.Provider value={{ opened: open, visible: open && !hidden, attentionCount, toggle: () => { if (open) setHidden((value) => !value); else { setOpen(true); setHidden(false); } } }}>
    {children}
    {!children && (!open || hidden) ? <button className={styles.launcher} type="button" aria-label={`${hidden ? "Show Butler" : "Open Butler"}${attentionCount ? `; ${attentionLabel}` : ""}`} title={attentionCount ? attentionLabel : "Butler"} onClick={() => { setOpen(true); setHidden(false); }}><img src="/nebula-logo.svg" width="22" height="36" alt="" aria-hidden="true" />{attentionCount > 0 && <span className={styles.launcherAttention} aria-hidden="true" />}</button> : null}
    {panelContent && createPortal(panelContent, document.body)}
    {voiceStart && api && engagement && createPortal(<NebulaVoice api={api} project={{ id: engagement.id, name: engagement.name }} initialMicrophone={voiceStart.microphone} onDelegate={delegateVoice} onExit={() => { setVoiceStart(undefined); setHidden(false); }} />, document.body)}
  </NebulaAssistantLauncherContext.Provider>;
}
