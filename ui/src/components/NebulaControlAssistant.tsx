import { useEffect, useRef, useState, type CSSProperties, type ReactNode } from "react";
import { Bot, GripHorizontal, Minimize2, Send, Settings2, Square, X } from "lucide-react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { createPortal } from "react-dom";
import { defaultModelRuntime } from "../api/runtimeDefaults";
import { logDiagnostic } from "../diagnostics";
import type { ToolSharingRuntime } from "../api/toolSharingConsent";
import { useWorkspace } from "../state/WorkspaceContext";
import { THEME_OPTIONS, useTheme, type ThemePreference } from "../state/ThemeContext";
import { readStorage, writeStorage } from "../state/browserStorage";
import { settingCatalog, type SettingCatalogEntry } from "../settingsCatalog";
import { assistantDestinations, assistantDestinationPath, destinationToOpen, type AssistantDestination } from "../assistantDestinations";
import type { ChatAssistantConfig, ChatMessage } from "../api/types";
import { assistantTurnOptions, assistantWorkReceipt, headlessSessionChange, isSettingNavigationRequest, isThemeChangeCommand, projectModeChange, projectScopeChange, settingClientAction, settingNavigationCandidates, settingToOpen, themeChange, type ProjectMode, type SettingClientAction } from "./nebulaAssistantActions";
import { saveAssistantProjectScope } from "./assistantSettingsMutations";
import { useConfirmation } from "./DialogSystem";
import { NebulaAssistantLauncherContext } from "./NebulaAssistantLauncher";
import { MicrophoneIcon, NebulaVoice } from "./NebulaVoice";
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
  const { api, engagement, providers, harnesses } = useWorkspace();
  const { preference, setPreference } = useTheme();
  const confirm = useConfirmation();
  const [open, setOpen] = useState(false);
  const [hidden, setHidden] = useState(false);
  const [query, setQuery] = useState("");
  const [destinationQuery, setDestinationQuery] = useState("");
  const [draft, setDraft] = useState("");
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [suggestions, setSuggestions] = useState<SettingCatalogEntry[]>([]);
  const [sessionId, setSessionId] = useState<string>();
  const [sessionRuntime, setSessionRuntime] = useState<{ kind: "provider" | "harness"; id: string; model: string }>();
  const [sessionConfig, setSessionConfig] = useState<ChatAssistantConfig>();
  const [busy, setBusy] = useState(false);
  const [loadingHistory, setLoadingHistory] = useState(false);
  const [error, setError] = useState("");
  const [voiceStart, setVoiceStart] = useState<{ microphone?: Promise<MediaStream> }>();
  const [position, setPosition] = useState(() => ({ x: Math.max(12, window.innerWidth - 580), y: 72 }));
  const drag = useRef<{ x: number; y: number; left: number; top: number } | undefined>(undefined);
  const abort = useRef<AbortController | undefined>(undefined);
  const activeTurn = useRef<{ turnId?: string; harnessTurnId?: string }>({});
  const voiceSessionId = useRef<string | undefined>(undefined);
  const input = useRef<HTMLTextAreaElement>(null);
  const panel = useRef<HTMLDivElement>(null);
  const transcript = useRef<HTMLDivElement>(null);
  const catalogRef = useRef<HTMLDetailsElement>(null);
  const destinationCatalogRef = useRef<HTMLDetailsElement>(null);
  const runtime = sessionRuntime ?? defaultModelRuntime(providers, harnesses);
  const storageKey = engagement ? `nebula.control-assistant.session.${engagement.id}` : undefined;
  const actionOutcomesKey = storageKey ? `${storageKey}.action-outcomes` : undefined;

  useEffect(() => () => { abort.current?.abort(); }, [engagement?.id]);

  useEffect(() => {
    voiceSessionId.current = undefined;
    setSessionId(undefined);
    setSessionRuntime(undefined);
    setSessionConfig(undefined);
    setMessages([]);
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
      voiceSessionId.current = session.id;
      setSessionConfig(session.assistantConfig);
      if (typeof api.getChatAssistantConfig === "function") {
        void api.getChatAssistantConfig(session.id).then((saved) => {
          if (current) setSessionConfig(saved.settings);
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
    if (open && !hidden && transcript.current) transcript.current.scrollTop = transcript.current.scrollHeight;
  }, [messages, open, hidden]);

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

  const send = async (currentText = draft) => {
    const prompt = currentText.trim();
    if (!prompt || busy || loadingHistory) return;
    setSuggestions([]);
    const requestedTheme = themeChange(prompt);
    if (requestedTheme) {
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
    if (isThemeChangeCommand(prompt)) {
      setError("I couldn't identify that theme. Choose one in Settings → Appearance.");
      return;
    }
    const modeChange = projectModeChange(prompt);
    if (modeChange) {
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
    if (scopeChange || headlessChange) {
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
    if (destination) {
      setDraft("");
      const confirmation = openDestination(destination);
      setMessages((current) => [...current, { role: "user", content: prompt }, { role: "assistant", content: confirmation }]);
      return;
    }
    const setting = settingToOpen(prompt);
    if (setting) {
      setDraft("");
      onOpenSetting(setting);
      setHidden(true);
      return;
    }
    if (isSettingNavigationRequest(prompt) && (!api || !engagement || !runtime || settingNavigationCandidates(prompt).length > 0)) {
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
    let effectiveSessionId = sessionId;
    let effectiveConfig = sessionConfig;
    if (sessionId) {
      try {
        const latest = await api.getControlAssistantSession(engagement.id);
        if (latest?.id === sessionId) {
          effectiveSessionId = latest.id;
          effectiveConfig = latest.assistantConfig;
          if (typeof api.getChatAssistantConfig === "function") {
            effectiveConfig = (await api.getChatAssistantConfig(latest.id)).settings;
          }
          setSessionConfig(effectiveConfig);
        }
      } catch (caught) {
        void logDiagnostic({ level: "error", eventCode: "interface.nebula_assistant.settings_refresh_failed", message: "The assistant's next-turn settings could not be refreshed.", outcome: "failure", stage: "settings", retryable: true, exception: caught });
        setError(caught instanceof Error ? caught.message : "Could not load Nebula assistant settings. Retry after reconnecting.");
        return;
      }
    }
    const selectedRuntime = effectiveConfig
      ? { kind: effectiveConfig.backend, id: effectiveConfig.profileId, model: effectiveConfig.model }
      : runtime;
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
    const controller = new AbortController();
    abort.current = controller;
    activeTurn.current = {};
    setBusy(true);
    setError("");
    setMessages((current) => [...current, { role: "user", content: prompt }]);
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
        reasoningEffort: effectiveConfig?.reasoningEffort ?? turnOptions.reasoningEffort,
        harnessReasoningEffort: effectiveConfig?.harnessReasoningEffort ?? turnOptions.harnessReasoningEffort,
        harnessServiceTier: effectiveConfig?.harnessServiceTier ?? turnOptions.harnessServiceTier,
        harnessMode: effectiveConfig?.harnessMode,
        harnessCyberAccessProgram: effectiveConfig?.harnessCyberAccessProgram,
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
        allowCloudToolResults,
      }, (event) => {
        if (event.type === "started" && event.turnId) activeTurn.current.turnId = event.turnId;
        if ("harnessTurnId" in event && event.harnessTurnId) activeTurn.current.harnessTurnId = event.harnessTurnId;
        const action = settingClientAction(event);
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
        confirmations.push(...workReceipts.filter(({ url }) => !result.message.content.includes(url)).map(({ content }) => content));
        if (result.message.id && confirmations.length && actionOutcomesKey) {
          const outcomes = readActionOutcomes(actionOutcomesKey);
          outcomes[result.message.id] = confirmations.join("\n\n");
          writeStorage(actionOutcomesKey, JSON.stringify(Object.fromEntries(Object.entries(outcomes).slice(-50))));
        }
        setMessages((current) => [...current, result.message, ...confirmations.map((content) => ({ role: "assistant" as const, content }))]);
      }
      else if (!controller.signal.aborted) setError("The response is still running. Reopen this assistant to check its saved conversation.");
    } catch (caught) {
      if (!controller.signal.aborted) {
        void logDiagnostic({ level: "error", eventCode: "interface.nebula_assistant.turn_failed", message: "The assistant could not answer.", outcome: "failure", stage: "turn", retryable: true, exception: caught });
        setDraft(prompt);
        setError(caught instanceof Error ? caught.message : "The assistant could not answer. Try again.");
      }
    } finally { setBusy(false); }
  };

  const startVoice = () => {
    if (!api || !engagement || !runtime || busy || loadingHistory) return;
    // Begin permission from the actual button activation for mobile browsers.
    const microphone = window.isSecureContext && navigator.mediaDevices?.getUserMedia
      ? navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true } })
      : undefined;
    voiceSessionId.current = sessionId;
    setVoiceStart({ microphone });
    setOpen(true);
    setHidden(true);
  };

  const delegateVoice = async (prompt: string): Promise<string | undefined> => {
    if (!api || !engagement || !runtime) throw new Error("Choose a project and a healthy model runtime to direct work.");
    const effectiveSessionId = voiceSessionId.current;
    let effectiveConfig = sessionConfig;
    if (effectiveSessionId) {
      const latest = await api.getControlAssistantSession(engagement.id);
      if (latest?.id === effectiveSessionId) {
        effectiveConfig = latest.assistantConfig;
        if (typeof api.getChatAssistantConfig === "function") {
          effectiveConfig = (await api.getChatAssistantConfig(latest.id)).settings;
        }
        setSessionConfig(effectiveConfig);
      }
    }
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
      reasoningEffort: effectiveConfig?.reasoningEffort ?? options.reasoningEffort,
      harnessReasoningEffort: effectiveConfig?.harnessReasoningEffort ?? options.harnessReasoningEffort,
      harnessServiceTier: effectiveConfig?.harnessServiceTier ?? options.harnessServiceTier,
      harnessMode: effectiveConfig?.harnessMode,
      harnessCyberAccessProgram: effectiveConfig?.harnessCyberAccessProgram,
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
      const action = settingClientAction(event);
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
      voiceSessionId.current = result.sessionId;
      writeStorage(storageKey, result.sessionId);
      setSessionId(result.sessionId);
    }
    if (!result?.message) throw new Error("The assistant response is still running. Check the saved work before retrying.");
    confirmations.push(...workReceipts.filter(({ url }) => !result.message!.content.includes(url)).map(({ content }) => content));
    setMessages((current) => [...current, result.message!, ...confirmations.map((content) => ({ role: "assistant" as const, content }))]);
    return [result.message.content, ...confirmations].filter(Boolean).join("\n\n");
  };

  const matches = query.trim() ? settingCatalog.filter((entry) =>
    [entry.label, entry.description, ...entry.keywords].join(" ").toLowerCase().includes(query.toLowerCase())) : settingCatalog;
  const destinationMatches = destinationQuery.trim() ? assistantDestinations.filter((entry) =>
    [entry.label, ...entry.aliases].join(" ").toLowerCase().includes(destinationQuery.toLowerCase())) : assistantDestinations;
  const directSettingIds = new Set(["settings.appearance", "settings.providers", "settings.harnesses", "settings.follow-up", "settings.network-scope", "settings.automation-runtime"]);
  const panelContent = open ? <div ref={panel} id="nebula-assistant-panel" className={styles.panel} role="dialog" aria-label="Nebula assistant" style={{ "--panel-left": `${position.x}px`, "--panel-top": `${position.y}px` } as CSSProperties} hidden={hidden}>
    <div className={styles.header}>
      <button className={`icon-button subtle ${styles.dragButton}`} type="button" aria-label="Move Nebula assistant" title="Drag to move · arrow keys to reposition" onPointerDown={(event) => { if (event.button !== 0) return; event.currentTarget.setPointerCapture(event.pointerId); drag.current = { x: event.clientX, y: event.clientY, left: position.x, top: position.y }; }} onPointerMove={(event) => { if (drag.current) move(drag.current.left + event.clientX - drag.current.x, drag.current.top + event.clientY - drag.current.y); }} onPointerUp={() => { drag.current = undefined; }} onKeyDown={(event) => { const step: Record<string, [number, number]> = { ArrowLeft: [-24, 0], ArrowRight: [24, 0], ArrowUp: [0, -24], ArrowDown: [0, 24] }; const offset = step[event.key]; if (offset) { event.preventDefault(); move(position.x + offset[0], position.y + offset[1]); } }}><GripHorizontal size={18} /></button>
      <div className={styles.identity}><strong>Nebula assistant</strong><small>{engagement?.name ?? "Choose a project"}</small></div>
      <button className={`icon-button subtle ${styles.voiceButton}`} type="button" aria-label="Start Nebula Voice" title="Start Nebula Voice" disabled={!api || !engagement || !runtime || busy || loadingHistory} onClick={startVoice}><MicrophoneIcon size={18} /><span className={styles.voiceLabel}>Start Voice</span></button>
      <button className={`icon-button subtle ${styles.hideButton}`} type="button" aria-label="Hide Nebula assistant" title="Hide assistant" onClick={() => setHidden(true)}><Minimize2 size={18} /></button>
      <button className={`icon-button subtle ${styles.settingsButton}`} type="button" aria-label="Nebula assistant settings" title={sessionId ? "Open Nebula assistant settings" : "Send a message to create Nebula assistant settings"} disabled={!sessionId || !engagement} onClick={() => { if (!sessionId || !engagement) return; setHidden(true); onNavigate(`/projects/${encodeURIComponent(engagement.id)}/workbench?view=chat&session=${encodeURIComponent(sessionId)}`); }}><Settings2 size={18} /></button>
      <button className={`icon-button subtle ${styles.closeButton}`} type="button" aria-label="Close Nebula assistant" title="Close assistant; keep history" onClick={() => { setOpen(false); setHidden(false); }}><X size={18} /></button>
    </div>
    <div className={styles.body}>
      <div ref={transcript} className={styles.transcript} role="log" aria-label="Nebula assistant conversation">{messages.map((message, index) => <div key={`${index}-${message.id ?? ""}`} className={message.role === "user" ? styles.question : styles.answer}><ReactMarkdown remarkPlugins={[remarkGfm]} components={{ a: ({ href, children, ...props }) => {
        const setting = settingCatalog.find((entry) => href === `/settings#${entry.target}`);
        const destination = assistantDestinations.find((entry) => href === assistantDestinationPath(entry, engagement?.id));
        const workLink = engagement && href && /^\/projects\/[^/]+\/workbench\?view=(?:chat&session|missions&mission)=/.test(href)
          && href.startsWith(`/projects/${encodeURIComponent(engagement.id)}/workbench?`) ? href : undefined;
        return setting
          ? <a href={href} onClick={(event) => { event.preventDefault(); onOpenSetting(setting); setHidden(true); }}>{children}</a>
          : destination ? <a href={href} onClick={(event) => { event.preventDefault(); openDestination(destination); }}>{children}</a>
          : workLink ? <a href={workLink} onClick={(event) => { event.preventDefault(); onNavigate(workLink); setHidden(true); }}>{children}</a>
          : <a href={href} {...props}>{children}</a>;
      } }}>{message.content}</ReactMarkdown></div>)}</div>
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
    <form className={styles.composer} onSubmit={(event) => { event.preventDefault(); void send(input.current?.value ?? draft); }}><textarea ref={input} aria-label="Ask Nebula a question" value={draft} onChange={(event) => setDraft(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) { event.preventDefault(); void send(event.currentTarget.value); } }} rows={2} maxLength={4000} placeholder="Ask about work, start an assistant or mission, or change a setting…" disabled={busy || loadingHistory} />{busy ? <button className="icon-button subtle" type="button" aria-label="Stop assistant response" onClick={() => void stop()}><Square size={18} /></button> : <button className="icon-button subtle" type="submit" aria-label="Send to Nebula assistant" disabled={!draft.trim() || (!isSettingNavigationRequest(draft) && !projectModeChange(draft) && !themeChange(draft) && !destinationToOpen(draft) && (!engagement || !runtime)) || loadingHistory}><Send size={18} /></button>}</form>
  </div> : null;

  return <NebulaAssistantLauncherContext.Provider value={{ opened: open, visible: open && !hidden, toggle: () => { if (open) setHidden((value) => !value); else { setOpen(true); setHidden(false); } } }}>
    {children}
    {!children && (!open || hidden) ? <button className={styles.launcher} type="button" aria-label={hidden ? "Show Nebula assistant" : "Open Nebula assistant"} title="Nebula assistant" onClick={() => { setOpen(true); setHidden(false); }}><Bot size={22} aria-hidden="true" /></button> : null}
    {panelContent && createPortal(panelContent, document.body)}
    {voiceStart && api && engagement && createPortal(<NebulaVoice api={api} project={{ id: engagement.id, name: engagement.name }} initialMicrophone={voiceStart.microphone} onDelegate={delegateVoice} onExit={() => { setVoiceStart(undefined); setHidden(false); }} />, document.body)}
  </NebulaAssistantLauncherContext.Provider>;
}
