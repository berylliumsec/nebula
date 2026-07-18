import { useCallback, useEffect, useState } from "react";
import { createPortal } from "react-dom";
import { ChevronDown, ChevronUp, Clipboard, LoaderCircle, Play, Sparkles, X } from "lucide-react";
import type { ApiClient } from "../api/client";
import type { GeneratedDraft, HarnessProfile, PostToolAssistantConfig, ProviderHealth } from "../api/types";
import type { FencedRunCandidate } from "./AssistantMarkdown";
import { DiagnosticErrorNotice, logCaughtDiagnostic } from "../diagnostics";

interface Props {
  api: ApiClient;
  engagementId: string;
  providers: ProviderHealth[];
  harnesses: HarnessProfile[];
  onRun: (candidate: FencedRunCandidate) => void;
}

const EMPTY: PostToolAssistantConfig = { suggestNextSteps: false, takeNotes: false, backendKind: "provider", cloudConfirmed: false };

function analysisRuntimeReady(config: PostToolAssistantConfig, providers: ProviderHealth[], harnesses: HarnessProfile[]): boolean {
  const harness = config.backendKind === "harness"
    ? harnesses.find((item) => item.id === config.harnessProfileId && item.enabled)
    : undefined;
  const provider = config.backendKind === "provider"
    ? providers.find((item) => item.id === config.providerId && item.enabled)
    : undefined;
  const backend = harness ?? provider;
  const remote = harness ? !harness.localOnly : provider ? !provider.local : false;
  const permitsProjectData = harness?.permitsSensitiveData ?? provider?.permitsSensitiveData ?? true;
  return Boolean(backend && config.model?.trim() && (!remote || (permitsProjectData && config.cloudConfirmed)));
}

export function PostToolAssistant({ api, engagementId, providers, harnesses, onRun }: Props) {
  const [config, setConfig] = useState<PostToolAssistantConfig>(EMPTY);
  const [result, setResult] = useState<GeneratedDraft>();
  const [expanded, setExpanded] = useState(false);
  const [command, setCommand] = useState("");
  const [busy, setBusy] = useState(false);
  const [savingToggle, setSavingToggle] = useState(false);
  const [error, setError] = useState<string>();
  const [savedMessage, setSavedMessage] = useState<string>();
  const [savedNeedsSettings, setSavedNeedsSettings] = useState(false);
  const analysisReady = analysisRuntimeReady(config, providers, harnesses);

  const refresh = useCallback(async () => {
    const [nextConfig, results] = await Promise.all([api.getPostToolAssistant(engagementId), api.listPostToolResults(engagementId)]);
    setConfig(nextConfig);
    const latest = results.find((item) => item.content?.nextStep && !item.metadata.dismissed);
    setResult(latest);
    setCommand(latest?.content?.nextStep?.command ?? "");
  }, [api, engagementId]);

  useEffect(() => { void refresh().catch((caught) => setError(caught instanceof Error ? caught.message : "Could not load tool assistance.")); }, [refresh]);

  useEffect(() => {
    const backendId = config.backendKind === "harness" ? config.harnessProfileId : config.providerId;
    if (savingToggle || (!config.suggestNextSteps && !config.takeNotes) || !analysisReady || !backendId || !config.model) return;
    let active = true;
    const tick = async () => {
      try {
        const [executions, results] = await Promise.all([api.listExecutions(engagementId, { limit: 30 }), api.listPostToolResults(engagementId)]);
        const known = new Set(results.map((item) => item.executionId));
        const next = executions.items.filter((item) => ["completed", "failed", "timed_out"].includes(item.status) && !known.has(item.id)).sort((a, b) => a.queuedAt.localeCompare(b.queuedAt))[0];
        if (next) {
          setBusy(true);
          let generated = await api.generateExecutionDraft(next.id, config.providerId ?? "harness", config.model!, config.cloudConfirmed, config);
          for (let attempt = 0; attempt < 150 && generated.status === "generating"; attempt += 1) {
            await new Promise((resolve) => globalThis.setTimeout(resolve, 400));
            generated = await api.getGeneratedDraft(generated.id);
          }
        }
        if (active) await refresh();
      } catch (caught) {
        void logCaughtDiagnostic("interface.post_tool_assistant.analysis_failed", "Post-tool analysis failed.", caught, "post_tool_assistant");
        if (active) setError(caught instanceof Error ? caught.message : "Post-tool analysis failed.");
      } finally { if (active) setBusy(false); }
    };
    void tick();
    const timer = globalThis.setInterval(() => void tick(), 3000);
    return () => { active = false; globalThis.clearInterval(timer); };
  }, [analysisReady, api, config, engagementId, refresh, savingToggle]);

  const toggle = async (key: "suggestNextSteps" | "takeNotes") => {
    const enabled = !config[key];
    setSavedMessage(undefined);
    setSavedNeedsSettings(false);
    const previous = config;
    const next = { ...config, [key]: enabled };
    setConfig(next);
    setSavingToggle(true);
    try {
      const saved = await api.setPostToolAssistant(engagementId, next);
      setConfig(saved);
      setError(undefined);
      const needsSettings = enabled && !analysisRuntimeReady(saved, providers, harnesses);
      setSavedNeedsSettings(needsSettings);
      setSavedMessage(
        `${key === "suggestNextSteps" ? "Next-step suggestions" : "Notes"} ${enabled ? "enabled" : "disabled"}.`
        + (needsSettings ? " Complete the analysis runtime setup before tool output is analyzed." : ""),
      );
    }
    catch (caught) {
      setConfig(previous);
      setError(caught instanceof Error ? caught.message : "Could not update tool assistance.");
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
    catch (caught) { setError(caught instanceof Error ? caught.message : "Could not dismiss the suggestion."); }
  };

  return <>
    <div className="post-tool-toggles" aria-label="Post-tool assistance">
      <label title="Suggest next steps"><input aria-label="Suggest next steps" type="checkbox" checked={config.suggestNextSteps} disabled={savingToggle} onChange={() => void toggle("suggestNextSteps")} /><span>Suggest next steps</span></label>
      <label title="Take notes"><input aria-label="Take notes" type="checkbox" checked={config.takeNotes} disabled={savingToggle} onChange={() => void toggle("takeNotes")} /><span>Take notes</span></label>
      {busy && <LoaderCircle className="spin" size={13} aria-label="Analyzing tool result" />}
    </div>
    {(error || savedMessage) && createPortal(<div className={`post-tool-feedback${error ? " error" : " success"}`} role={error ? undefined : "status"} aria-live={error ? undefined : "polite"}>
      {error ? <><DiagnosticErrorNotice error={error} fallback="Post-tool assistance is unavailable." compact /><a className="button quiet" href="/settings#post-tool-assistant-settings">Open tool follow-up settings</a></> : <><span>{savedMessage}</span>{savedNeedsSettings && <a className="button quiet" href="/settings#post-tool-assistant-settings">Open tool follow-up settings</a>}</>}
    </div>, document.body)}
    {config.suggestNextSteps && step && result && <aside className={`post-tool-suggestion${expanded ? " expanded" : ""}`} aria-label="Suggested next step">
      <header><Sparkles size={15} /><span><small>Suggested next step</small><strong>{step.title}</strong></span><button className="icon-button subtle" type="button" aria-label="Dismiss suggestion" onClick={() => void dismiss()}><X size={14} /></button></header>
      {expanded && <div className="post-tool-suggestion-body"><p>{step.rationale}</p><label>Exact command<textarea aria-label="Suggested command" rows={4} value={command} onChange={(event) => setCommand(event.target.value)} /></label>{step.networkTarget && <small>Network: {step.networkTarget}{step.networkPorts.length ? ` · ${step.networkPorts.join(", ")}` : ""}</small>}</div>}
      <footer><button className="button quiet" type="button" onClick={() => setExpanded((value) => !value)}>{expanded ? <ChevronDown size={13} /> : <ChevronUp size={13} />} {expanded ? "Collapse" : "Expand"}</button>{expanded && <button className="button quiet" type="button" onClick={() => void navigator.clipboard.writeText(command)}><Clipboard size={13} /> Copy</button>}<button className="button primary" type="button" onClick={run}><Play size={13} /> Run</button></footer>
    </aside>}
  </>;
}
