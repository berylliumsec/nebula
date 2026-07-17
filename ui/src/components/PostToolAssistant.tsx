import { useCallback, useEffect, useMemo, useState } from "react";
import { ChevronDown, ChevronUp, Clipboard, LoaderCircle, Play, Sparkles, X } from "lucide-react";
import type { ApiClient } from "../api/client";
import type { GeneratedDraft, PostToolAssistantConfig, ProviderHealth } from "../api/types";
import type { FencedRunCandidate } from "./AssistantMarkdown";
import { DiagnosticErrorNotice, logCaughtDiagnostic } from "../diagnostics";

interface Props {
  api: ApiClient;
  engagementId: string;
  providers: ProviderHealth[];
  onRun: (candidate: FencedRunCandidate) => void;
}

const EMPTY: PostToolAssistantConfig = { suggestNextSteps: false, takeNotes: false, cloudConfirmed: false };

export function PostToolAssistant({ api, engagementId, providers, onRun }: Props) {
  const [config, setConfig] = useState<PostToolAssistantConfig>(EMPTY);
  const [result, setResult] = useState<GeneratedDraft>();
  const [expanded, setExpanded] = useState(false);
  const [command, setCommand] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string>();
  const structured = useMemo(() => providers.find((item) => item.enabled && item.capabilities.some((value) => value.toLowerCase().includes("strict structured"))), [providers]);

  const refresh = useCallback(async () => {
    const [nextConfig, results] = await Promise.all([api.getPostToolAssistant(engagementId), api.listPostToolResults(engagementId)]);
    setConfig(nextConfig);
    const latest = results.find((item) => item.content?.nextStep && !item.metadata.dismissed);
    setResult(latest);
    setCommand(latest?.content?.nextStep?.command ?? "");
  }, [api, engagementId]);

  useEffect(() => { void refresh().catch((caught) => setError(caught instanceof Error ? caught.message : "Could not load tool assistance.")); }, [refresh]);

  useEffect(() => {
    if (!config.suggestNextSteps && !config.takeNotes || !config.providerId || !config.model) return;
    let active = true;
    const tick = async () => {
      try {
        const [executions, results] = await Promise.all([api.listExecutions(engagementId, { limit: 30 }), api.listPostToolResults(engagementId)]);
        const known = new Set(results.map((item) => item.executionId));
        const next = executions.items.filter((item) => ["completed", "failed", "timed_out"].includes(item.status) && !known.has(item.id)).sort((a, b) => a.queuedAt.localeCompare(b.queuedAt))[0];
        if (next) {
          setBusy(true);
          let generated = await api.generateExecutionDraft(next.id, config.providerId!, config.model!, config.cloudConfirmed, config);
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
  }, [api, config, engagementId, refresh]);

  const toggle = async (key: "suggestNextSteps" | "takeNotes") => {
    const enabled = !config[key];
    const provider = config.providerId ? providers.find((item) => item.id === config.providerId) : structured;
    if (enabled && !provider) { setError("Configure an enabled provider with strict structured output first."); return; }
    const cloudConfirmed = enabled && provider && !provider.local && !config.cloudConfirmed
      ? globalThis.confirm(`Allow bounded, redacted tool results to be sent to ${provider.name} for this project?`)
      : config.cloudConfirmed;
    if (enabled && provider && !provider.local && !cloudConfirmed) return;
    const next = { ...config, [key]: enabled, providerId: provider?.id, model: config.model ?? provider?.defaultModel ?? provider?.models[0], cloudConfirmed: Boolean(cloudConfirmed) };
    try { setConfig(await api.setPostToolAssistant(engagementId, next)); setError(undefined); }
    catch (caught) { setError(caught instanceof Error ? caught.message : "Could not update tool assistance."); }
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
      <label title="Suggest next steps"><input type="checkbox" checked={config.suggestNextSteps} onChange={() => void toggle("suggestNextSteps")} /> Suggest next steps</label>
      <label title="Take notes"><input type="checkbox" checked={config.takeNotes} onChange={() => void toggle("takeNotes")} /> Take notes</label>
      {busy && <LoaderCircle className="spin" size={13} aria-label="Analyzing tool result" />}
    </div>
    {error && <div className="post-tool-error"><DiagnosticErrorNotice error={error} fallback="Post-tool assistance is unavailable." compact /></div>}
    {config.suggestNextSteps && step && result && <aside className={`post-tool-suggestion${expanded ? " expanded" : ""}`} aria-label="Suggested next step">
      <header><Sparkles size={15} /><span><small>Suggested next step</small><strong>{step.title}</strong></span><button className="icon-button subtle" type="button" aria-label="Dismiss suggestion" onClick={() => void dismiss()}><X size={14} /></button></header>
      {expanded && <div className="post-tool-suggestion-body"><p>{step.rationale}</p><label>Exact command<textarea aria-label="Suggested command" rows={4} value={command} onChange={(event) => setCommand(event.target.value)} /></label>{step.networkTarget && <small>Network: {step.networkTarget}{step.networkPorts.length ? ` · ${step.networkPorts.join(", ")}` : ""}</small>}</div>}
      <footer><button className="button quiet" type="button" onClick={() => setExpanded((value) => !value)}>{expanded ? <ChevronDown size={13} /> : <ChevronUp size={13} />} {expanded ? "Collapse" : "Expand"}</button>{expanded && <button className="button quiet" type="button" onClick={() => void navigator.clipboard.writeText(command)}><Clipboard size={13} /> Copy</button>}<button className="button primary" type="button" onClick={run}><Play size={13} /> Run</button></footer>
    </aside>}
  </>;
}
