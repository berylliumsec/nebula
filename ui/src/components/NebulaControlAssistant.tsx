import { useEffect, useRef, useState } from "react";
import { Bot, GripHorizontal, Minimize2, Send, Square, X } from "lucide-react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { createPortal } from "react-dom";
import { defaultModelRuntime } from "../api/runtimeDefaults";
import { sharesToolResultsAlways, type ToolSharingRuntime } from "../api/toolSharingConsent";
import { useConfirmation } from "./DialogSystem";
import { useWorkspace } from "../state/WorkspaceContext";
import { useTheme, type ThemePreference } from "../state/ThemeContext";
import { settingCatalog, type SettingCatalogEntry } from "../settingsCatalog";
import type { ChatMessage } from "../api/types";
import { assistantTurnOptions, projectModeChange, settingToOpen } from "./nebulaAssistantActions";
import styles from "./NebulaControlAssistant.module.css";

const THEMES: { value: ThemePreference; label: string }[] = [
  { value: "light", label: "Light" }, { value: "dark", label: "Dark" },
  { value: "studio-dark", label: "Studio Dark" },
  { value: "zero-light", label: "Zero Light" }, { value: "zero-dark", label: "Zero Dark" },
];

export function NebulaControlAssistant({ onOpenSetting, onCloseSetting, onSavedSetting }: {
  onOpenSetting: (entry: SettingCatalogEntry) => void;
  onCloseSetting?: () => void;
  onSavedSetting?: (entry: SettingCatalogEntry, status: string) => void;
}) {
  const { api, engagement, providers, harnesses } = useWorkspace();
  const confirm = useConfirmation();
  const { preference, setPreference } = useTheme();
  const [open, setOpen] = useState(false);
  const [hidden, setHidden] = useState(false);
  const [query, setQuery] = useState("");
  const [draft, setDraft] = useState("");
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [sessionId, setSessionId] = useState<string>();
  const [sessionRuntime, setSessionRuntime] = useState<{ kind: "provider" | "harness"; id: string; model: string }>();
  const [busy, setBusy] = useState(false);
  const [loadingHistory, setLoadingHistory] = useState(false);
  const [error, setError] = useState("");
  const [position, setPosition] = useState(() => ({ x: Math.max(12, window.innerWidth - 580), y: 72 }));
  const drag = useRef<{ x: number; y: number; left: number; top: number } | undefined>(undefined);
  const abort = useRef<AbortController | undefined>(undefined);
  const activeTurn = useRef<{ turnId?: string; harnessTurnId?: string }>({});
  const input = useRef<HTMLTextAreaElement>(null);
  const panel = useRef<HTMLDivElement>(null);
  const runtime = sessionRuntime ?? defaultModelRuntime(providers, harnesses);
  const toolRuntime: ToolSharingRuntime | undefined = runtime?.kind === "provider"
    ? (() => { const profile = providers.find((item) => item.id === runtime.id); return profile ? { kind: "provider", profile } : undefined; })()
    : runtime?.kind === "harness"
      ? (() => { const profile = harnesses.find((item) => item.id === runtime.id); return profile ? { kind: "harness", profile } : undefined; })()
      : undefined;
  const storageKey = engagement ? `nebula.control-assistant.session.${engagement.id}` : undefined;

  useEffect(() => () => { abort.current?.abort(); }, [engagement?.id]);

  useEffect(() => {
    setSessionId(undefined);
    setSessionRuntime(undefined);
    setMessages([]);
    setError("");
    setLoadingHistory(false);
    if (!storageKey || !api) return;
    setLoadingHistory(true);
    let current = true;
    void api.getControlAssistantSession(engagement!.id).then((session) => {
      if (!current || !session || session.engagementId !== engagement?.id) return;
      localStorage.setItem(storageKey, session.id);
      setSessionId(session.id);
      const runtimeId = session.backend === "provider" ? session.providerId : session.harnessProfileId;
      if (runtimeId && session.model) setSessionRuntime({ kind: session.backend, id: runtimeId, model: session.model });
      return api.listChatMessages(session.id);
    }).then((history) => { if (current && history) setMessages(history.filter((item) => item.role === "user" || item.role === "assistant")); })
      .catch(() => { if (current) setError("The saved conversation could not be loaded. Retry after Core reconnects."); })
      .finally(() => { if (current) setLoadingHistory(false); });
    return () => { current = false; };
  }, [api, engagement?.id, storageKey]);

  useEffect(() => {
    if (!open || hidden) return;
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
    if (!open) return;
    const clamp = () => { const rect = panel.current?.getBoundingClientRect(); if (rect) move(rect.left, rect.top); };
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
    } catch (caught) { setError(caught instanceof Error ? caught.message : "Could not stop the response. Try again."); }
  };

  const send = async (currentText = draft) => {
    const prompt = currentText.trim();
    if (!prompt || busy || loadingHistory) return;
    const modeChange = projectModeChange(prompt);
    if (modeChange) {
      if (!api || !engagement) { setError("Choose a project to change its execution mode."); return; }
      const entry = settingCatalog.find((item) => item.id === "settings.network-scope")!;
      const projectId = engagement.id;
      const projectName = engagement.name;
      const modeLabel = modeChange.mode === "host" ? "Host" : "Docker";
      setDraft("");
      setError("");
      setBusy(true);
      setHidden(true);
      setMessages((current) => [...current, { role: "user", content: prompt }]);
      onOpenSetting(entry);
      try {
        const policy = await api.getAutomationPolicy(projectId);
        const currentMode = policy.executionMode ?? "docker";
        if (modeChange.from && currentMode !== modeChange.from) {
          throw new Error(`${projectName} is already in ${currentMode === "host" ? "Host" : "Docker"} mode. Its policy was not changed. Check the open setting and try again.`);
        }
        if (currentMode === modeChange.mode) {
          onSavedSetting?.(entry, `${projectName} is already in ${modeLabel} mode.`);
          setMessages((current) => [...current, { role: "assistant", content: `${projectName} is already in ${modeLabel} mode. No change was needed.` }]);
        } else {
          const approved = await confirm({
            title: `Switch ${projectName} to ${modeLabel} mode?`,
            message: modeChange.mode === "host"
              ? "Commands will run as the Nebula server's OS user. Agents can access other host folders and networks allowed by that account. Docker isolation, scope-filtered networking, and container VPN routing will not apply. Existing sessions keep their current policy."
              : "New command sessions will run inside Docker. Existing sessions keep their current policy.",
            confirmLabel: `Switch to ${modeLabel} mode`,
          });
          if (!approved) {
            setMessages((current) => [...current, { role: "assistant", content: "The project policy was not changed." }]);
          } else {
            await api.updateAutomationPolicy(projectId, {
              executionMode: modeChange.mode,
              hostAccessAcknowledged: modeChange.mode === "host",
              approvalPolicy: policy.approvalPolicy,
              networkEnabled: policy.networkEnabled,
              runnerProfileId: policy.runnerProfileId,
              vpnProfileId: policy.vpnProfileId,
              maxTimeoutMs: policy.maxTimeoutMs,
              expectedRevision: policy.revision,
            });
            const observed = await api.getAutomationPolicy(projectId);
            if (observed.executionMode !== modeChange.mode) throw new Error("Core did not confirm the new execution mode. Reopen Project Policy before retrying.");
            onSavedSetting?.(entry, `${projectName} saved in ${modeLabel} mode for new sessions.`);
            setMessages((current) => [...current, { role: "assistant", content: `${projectName} is now in ${modeLabel} mode for new sessions. Existing sessions keep their previous policy.` }]);
          }
        }
        // Keep the updated Settings control visible long enough to inspect the result.
        await new Promise((resolve) => window.setTimeout(resolve, 1800));
      } catch (caught) {
        setError(caught instanceof Error ? caught.message : "The project policy could not be changed. Check the open setting and retry.");
        setDraft(prompt);
      } finally {
        onCloseSetting?.();
        setHidden(false);
        setBusy(false);
      }
      return;
    }
    const setting = settingToOpen(prompt);
    if (setting) {
      setDraft("");
      onOpenSetting(setting);
      setHidden(true);
      return;
    }
    if (!api || !engagement || !runtime) return;
    const turnOptions = assistantTurnOptions(runtime, harnesses);
    let allowCloudToolResults = false;
    if (toolRuntime && !(toolRuntime.kind === "provider" ? toolRuntime.profile.local : toolRuntime.profile.localOnly)) {
      if (!toolRuntime.profile.permitsSensitiveData) {
        setError(`${toolRuntime.profile.name} is configured for text only. Permit project data in Settings to use settings tools.`);
        return;
      }
      allowCloudToolResults = sharesToolResultsAlways(toolRuntime) || await confirm({
        title: "Share settings tool results?",
        message: `Allow this turn to send bounded settings tool inputs and results to ${toolRuntime.profile.name}? Core keeps the saved settings, and risky actions still require approval.`,
        confirmLabel: "Allow this turn",
      });
      if (!allowCloudToolResults) return;
    }
    const controller = new AbortController();
    abort.current = controller;
    activeTurn.current = {};
    setBusy(true);
    setError("");
    setMessages((current) => [...current, { role: "user", content: prompt }]);
    setDraft("");
    try {
      const result = await api.streamChat({
        controlAssistant: true,
        engagementId: engagement.id,
        sessionId,
        backend: runtime.kind,
        providerId: runtime.kind === "provider" ? runtime.id : undefined,
        harnessProfileId: runtime.kind === "harness" ? runtime.id : undefined,
        model: turnOptions.model,
        reasoningEffort: turnOptions.reasoningEffort,
        harnessReasoningEffort: turnOptions.harnessReasoningEffort,
        harnessServiceTier: turnOptions.harnessServiceTier,
        messages: [{ role: "user", content: prompt }],
        toolsEnabled: false,
        allowCloudToolResults,
      }, (event) => {
        if (event.type === "started" && event.turnId) activeTurn.current.turnId = event.turnId;
        if ("harnessTurnId" in event && event.harnessTurnId) activeTurn.current.harnessTurnId = event.harnessTurnId;
      }, controller.signal);
      if (result?.sessionId && storageKey) {
        localStorage.setItem(storageKey, result.sessionId);
        setSessionId(result.sessionId);
        const runtimeId = result.backend === "harness" ? result.harnessProfileId : result.providerId;
        if (runtimeId) setSessionRuntime({ kind: result.backend === "harness" ? "harness" : "provider", id: runtimeId, model: result.model });
      }
      if (result?.message) setMessages((current) => [...current, result.message]);
      else if (!controller.signal.aborted) setError("The response is still running. Reopen this assistant to check its saved conversation.");
    } catch (caught) {
      if (!controller.signal.aborted) { setDraft(prompt); setError(caught instanceof Error ? caught.message : "The assistant could not answer. Try again."); }
    } finally { setBusy(false); }
  };

  const matches = query.trim() ? settingCatalog.filter((entry) =>
    [entry.label, entry.description, ...entry.keywords].join(" ").toLowerCase().includes(query.toLowerCase())) : settingCatalog;
  const directSettingIds = new Set(["settings.appearance", "settings.providers", "settings.harnesses", "settings.follow-up"]);
  const panelContent = open ? <div ref={panel} className={styles.panel} role="dialog" aria-label="Nebula settings assistant" style={{ left: position.x, top: position.y }} hidden={hidden}>
    <header className={styles.header}>
      <button className="icon-button subtle" type="button" aria-label="Move Nebula assistant" title="Drag to move · arrow keys to reposition" onPointerDown={(event) => { if (event.button !== 0) return; event.currentTarget.setPointerCapture(event.pointerId); drag.current = { x: event.clientX, y: event.clientY, left: position.x, top: position.y }; }} onPointerMove={(event) => { if (drag.current) move(drag.current.left + event.clientX - drag.current.x, drag.current.top + event.clientY - drag.current.y); }} onPointerUp={() => { drag.current = undefined; }} onKeyDown={(event) => { const step: Record<string, [number, number]> = { ArrowLeft: [-24, 0], ArrowRight: [24, 0], ArrowUp: [0, -24], ArrowDown: [0, 24] }; const offset = step[event.key]; if (offset) { event.preventDefault(); move(position.x + offset[0], position.y + offset[1]); } }}><GripHorizontal size={18} /></button>
      <strong>Nebula assistant</strong><small>{engagement?.name ?? "Choose a project"}</small>
      <button className="icon-button subtle" type="button" aria-label="Hide Nebula assistant" title="Hide assistant" onClick={() => setHidden(true)}><Minimize2 size={18} /></button>
      <button className="icon-button subtle" type="button" aria-label="Close Nebula assistant" title="Close assistant; keep history" onClick={() => { setOpen(false); setHidden(false); }}><X size={18} /></button>
    </header>
    <div className={styles.body}>
      <div className={styles.transcript} role="log" aria-label="Nebula assistant conversation">{messages.map((message, index) => <div key={`${index}-${message.id ?? ""}`} className={message.role === "user" ? styles.question : styles.answer}><ReactMarkdown remarkPlugins={[remarkGfm]}>{message.content}</ReactMarkdown></div>)}</div>
      <details className={styles.catalog}><summary>Settings I can help with</summary><input aria-label="Find a setting" placeholder="Find a setting" value={query} onChange={(event) => setQuery(event.target.value)} />
        <div className={styles.settingList}>{matches.length ? matches.map((entry) => <button key={entry.id} className="button quiet" type="button" onClick={() => { onOpenSetting(entry); setHidden(true); }}><span>{entry.label}</span><small>{directSettingIds.has(entry.id) ? "Direct actions available · open controls" : "Open controls"}</small></button>) : <p>No setting found.</p>}</div>
        <div className={styles.themes}>{THEMES.map((theme) => <button key={theme.value} className="button quiet" type="button" aria-pressed={preference === theme.value} onClick={() => setPreference(theme.value)}>{theme.label}</button>)}</div>
      </details>
      {error && <p role="alert">{error}</p>}
      {loadingHistory && <p role="status">Restoring your saved conversation…</p>}
      {!engagement && <p role="status">Choose a project to start a conversation. Device appearance can still be changed above.</p>}
      {engagement && !runtime && <p role="status">Connect a model provider or harness to ask Nebula.</p>}
    </div>
    <form className={styles.composer} onSubmit={(event) => { event.preventDefault(); void send(input.current?.value ?? draft); }}><textarea ref={input} aria-label="Ask Nebula to change a setting" value={draft} onChange={(event) => setDraft(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) { event.preventDefault(); void send(event.currentTarget.value); } }} rows={2} maxLength={4000} placeholder="Ask about or open a setting…" disabled={busy || loadingHistory} />{busy ? <button className="icon-button subtle" type="button" aria-label="Stop assistant response" onClick={() => void stop()}><Square size={18} /></button> : <button className="icon-button subtle" type="submit" aria-label="Send to Nebula assistant" disabled={!draft.trim() || (!settingToOpen(draft) && !projectModeChange(draft) && (!engagement || !runtime)) || loadingHistory}><Send size={18} /></button>}</form>
  </div> : null;

  return <>{!open || hidden ? <button className={styles.launcher} type="button" aria-label={hidden ? "Show Nebula assistant" : "Open Nebula assistant"} title="Nebula assistant" onClick={() => { setOpen(true); setHidden(false); }}><Bot size={22} aria-hidden="true" /></button> : null}{panelContent && createPortal(panelContent, document.body)}</>;
}
