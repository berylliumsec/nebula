import { useCallback, useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { Check, ChevronDown, ChevronUp, CircleAlert, Clipboard, LoaderCircle, Play, Sparkles, X } from "lucide-react";
import type { ApiClient } from "../api/client";
import type { GeneratedDraft, HarnessProfile, PostToolAssistantConfig, ProviderHealth } from "../api/types";
import type { FencedRunCandidate } from "./AssistantMarkdown";
import { logCaughtDiagnostic } from "../diagnostics";

interface Props {
  api: ApiClient;
  engagementId: string;
  providers: ProviderHealth[];
  harnesses: HarnessProfile[];
  onRun: (candidate: FencedRunCandidate) => void;
}

const EMPTY: PostToolAssistantConfig = { suggestNextSteps: false, takeNotes: false, backendKind: "provider", cloudConfirmed: false };

interface AnalysisRuntimeStatus {
  ready: boolean;
  message?: string;
}

interface ToolFeedback {
  kind: "setup" | "success" | "error";
  title: string;
  message: string;
}

function analysisRuntimeStatus(config: PostToolAssistantConfig, providers: ProviderHealth[], harnesses: HarnessProfile[]): AnalysisRuntimeStatus {
  const harness = config.backendKind === "harness"
    ? harnesses.find((item) => item.id === config.harnessProfileId && item.enabled)
    : undefined;
  const provider = config.backendKind === "provider"
    ? providers.find((item) => item.id === config.providerId && item.enabled)
    : undefined;
  const backend = harness ?? provider;
  const remote = harness ? !harness.localOnly : provider ? !provider.local : false;
  const permitsProjectData = harness?.permitsSensitiveData ?? provider?.permitsSensitiveData ?? true;
  if (!backend) return { ready: false, message: "Choose an enabled model provider or agent harness in Settings." };
  if (!config.model?.trim()) return { ready: false, message: `Choose a model for ${backend.name} in Settings.` };
  if (remote && !permitsProjectData) return { ready: false, message: `${backend.name} is not permitted to receive project data.` };
  if (remote && !config.cloudConfirmed) return { ready: false, message: `Confirm project data use for ${backend.name} in Settings.` };
  return { ready: true };
}

export function PostToolAssistant({ api, engagementId, providers, harnesses, onRun }: Props) {
  const [config, setConfig] = useState<PostToolAssistantConfig>(EMPTY);
  const [result, setResult] = useState<GeneratedDraft>();
  const [expanded, setExpanded] = useState(false);
  const [command, setCommand] = useState("");
  const [busy, setBusy] = useState(false);
  const [savingToggle, setSavingToggle] = useState(false);
  const [feedback, setFeedback] = useState<ToolFeedback>();
  const [feedbackPosition, setFeedbackPosition] = useState({ top: 18, right: 18 });
  const togglesRef = useRef<HTMLDivElement>(null);
  const runtimeStatus = analysisRuntimeStatus(config, providers, harnesses);

  const positionFeedback = useCallback(() => {
    const anchor = togglesRef.current?.getBoundingClientRect();
    if (!anchor) return;
    const width = Math.min(380, globalThis.innerWidth - 24);
    const maximumRight = Math.max(12, globalThis.innerWidth - width - 12);
    setFeedbackPosition({
      top: anchor.bottom + 9,
      right: Math.min(Math.max(12, globalThis.innerWidth - anchor.right), maximumRight),
    });
  }, []);

  const refresh = useCallback(async () => {
    const [nextConfig, results] = await Promise.all([api.getPostToolAssistant(engagementId), api.listPostToolResults(engagementId)]);
    setConfig(nextConfig);
    const latest = results.find((item) => item.content?.nextStep && !item.metadata.dismissed);
    setResult(latest);
    setCommand(latest?.content?.nextStep?.command ?? "");
  }, [api, engagementId]);

  useEffect(() => { void refresh().catch((caught) => setFeedback({ kind: "error", title: "Tool assistance unavailable", message: caught instanceof Error ? caught.message : "Could not load tool assistance." })); }, [refresh]);

  useEffect(() => {
    if (!feedback) return;
    positionFeedback();
    globalThis.addEventListener("resize", positionFeedback);
    globalThis.addEventListener("scroll", positionFeedback, true);
    return () => {
      globalThis.removeEventListener("resize", positionFeedback);
      globalThis.removeEventListener("scroll", positionFeedback, true);
    };
  }, [feedback, positionFeedback]);

  useEffect(() => {
    const backendId = config.backendKind === "harness" ? config.harnessProfileId : config.providerId;
    if (savingToggle || (!config.suggestNextSteps && !config.takeNotes) || !runtimeStatus.ready || !backendId || !config.model) return;
    let active = true;
    const tick = async () => {
      try {
        const [executions, missions, results] = await Promise.all([
          api.listExecutions(engagementId, { limit: 30 }),
          api.listRuns(engagementId),
          api.listPostToolResults(engagementId),
        ]);
        const known = new Set(results.map((item) => item.executionId));
        const next = executions.items.filter((item) => ["completed", "failed", "timed_out"].includes(item.status) && !known.has(item.id)).sort((a, b) => a.queuedAt.localeCompare(b.queuedAt))[0];
        if (next) {
          setBusy(true);
          let generated = await api.generateExecutionDraft(next.id, config.providerId ?? "harness", config.model!, config.cloudConfirmed, config);
          for (let attempt = 0; attempt < 150 && generated.status === "generating"; attempt += 1) {
            await new Promise((resolve) => globalThis.setTimeout(resolve, 400));
            generated = await api.getGeneratedDraft(generated.id);
          }
        } else {
          const mission = missions.items
            .filter((item) => ["complete", "failed", "cancelled"].includes(item.status) && !known.has(item.id))
            .sort((a, b) => a.updatedAt.localeCompare(b.updatedAt))[0];
          if (mission) {
            setBusy(true);
            let generated = await api.generateMissionDraft(mission.id, config.providerId ?? "harness", config.model!, config.cloudConfirmed, config);
            for (let attempt = 0; attempt < 150 && generated.status === "generating"; attempt += 1) {
              await new Promise((resolve) => globalThis.setTimeout(resolve, 400));
              generated = await api.getGeneratedDraft(generated.id);
            }
          }
        }
        if (active) await refresh();
      } catch (caught) {
        void logCaughtDiagnostic("interface.post_tool_assistant.analysis_failed", "Post-tool analysis failed.", caught, "post_tool_assistant");
        if (active) setFeedback({ kind: "error", title: "Tool analysis stopped", message: caught instanceof Error ? caught.message : "Post-tool analysis failed." });
      } finally { if (active) setBusy(false); }
    };
    void tick();
    const timer = globalThis.setInterval(() => void tick(), 3000);
    return () => { active = false; globalThis.clearInterval(timer); };
  }, [api, config, engagementId, refresh, runtimeStatus.ready, savingToggle]);

  const toggle = async (key: "suggestNextSteps" | "takeNotes") => {
    const enabled = !config[key];
    setFeedback(undefined);
    if (enabled && !runtimeStatus.ready) {
      setFeedback({
        kind: "setup",
        title: "Analysis runtime required",
        message: runtimeStatus.message ?? "Configure tool follow-up in Settings before enabling this control.",
      });
      return;
    }
    const previous = config;
    const next = { ...config, [key]: enabled };
    setConfig(next);
    setSavingToggle(true);
    try {
      const saved = await api.setPostToolAssistant(engagementId, next);
      setConfig(saved);
      setFeedback({
        kind: "success",
        title: `${key === "suggestNextSteps" ? "Next-step suggestions" : "Notes"} ${enabled ? "enabled" : "disabled"}`,
        message: enabled ? "New completed tool results will be analyzed." : "No new tool results will be analyzed for this feature.",
      });
    }
    catch (caught) {
      setConfig(previous);
      setFeedback({ kind: "error", title: "Could not save this control", message: caught instanceof Error ? caught.message : "Could not update tool assistance." });
    }
    finally { setSavingToggle(false); }
  };

  const step = result?.content?.nextStep;
  const run = () => {
    if (!result || !step || !command.trim()) return;
    onRun({ source: command, language: step.language, declaredLanguage: step.language, origin: { kind: "rerun", executionId: result.executionId } });
    setResult(undefined);
  };
  const dismiss = async () => {
    if (!result) return;
    try { await api.dismissPostToolSuggestion(result.id); setResult(undefined); }
    catch (caught) { setFeedback({ kind: "error", title: "Could not dismiss suggestion", message: caught instanceof Error ? caught.message : "Could not dismiss the suggestion." }); }
  };

  return <>
    <div className="post-tool-toggles" aria-label="Post-tool assistance" ref={togglesRef}>
      <label title="Suggest next steps"><input aria-label="Suggest next steps" type="checkbox" checked={config.suggestNextSteps} disabled={savingToggle} onChange={() => void toggle("suggestNextSteps")} /><span>Suggest next steps</span></label>
      <label title="Take notes"><input aria-label="Take notes" type="checkbox" checked={config.takeNotes} disabled={savingToggle} onChange={() => void toggle("takeNotes")} /><span>Take notes</span></label>
      {busy && <LoaderCircle className="spin" size={13} aria-label="Analyzing tool result" />}
    </div>
    {feedback && createPortal(<div className={`post-tool-feedback ${feedback.kind}`} style={feedbackPosition} role={feedback.kind === "success" ? "status" : "alert"} aria-live={feedback.kind === "success" ? "polite" : "assertive"}>
      <span className="post-tool-feedback-icon" aria-hidden="true">{feedback.kind === "success" ? <Check size={15} /> : <CircleAlert size={15} />}</span>
      <span className="post-tool-feedback-copy"><strong>{feedback.title}</strong><small>{feedback.message}</small></span>
      {feedback.kind === "setup" && <a className="button quiet" href="/settings#post-tool-assistant-settings">Open Settings</a>}
      <button className="icon-button subtle post-tool-feedback-close" type="button" aria-label="Dismiss tool assistance message" onClick={() => setFeedback(undefined)}><X size={13} /></button>
    </div>, document.body)}
    {config.suggestNextSteps && step && result && <aside className={`post-tool-suggestion${expanded ? " expanded" : ""}`} aria-label="Suggested next step">
      <header><Sparkles size={15} /><span><small>Suggested next step</small><strong>{step.title}</strong></span><button className="icon-button subtle" type="button" aria-label="Dismiss suggestion" onClick={() => void dismiss()}><X size={14} /></button></header>
      {expanded && <div className="post-tool-suggestion-body"><p>{step.rationale}</p><label>Exact command<textarea aria-label="Suggested command" rows={4} value={command} onChange={(event) => setCommand(event.target.value)} /></label>{step.networkTarget && <small>Network: {step.networkTarget}{step.networkPorts.length ? ` · ${step.networkPorts.join(", ")}` : ""}</small>}</div>}
      <footer><button className="button quiet" type="button" onClick={() => setExpanded((value) => !value)}>{expanded ? <ChevronDown size={13} /> : <ChevronUp size={13} />} {expanded ? "Collapse" : "Expand"}</button>{expanded && <button className="button quiet" type="button" onClick={() => void navigator.clipboard.writeText(command)}><Clipboard size={13} /> Copy</button>}<button className="button primary" type="button" onClick={run}><Play size={13} /> Run</button></footer>
    </aside>}
  </>;
}
