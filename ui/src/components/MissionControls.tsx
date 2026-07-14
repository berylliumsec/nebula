import { useEffect, useMemo, useState, type FormEvent, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { Play, ShieldCheck, Square, Wrench, X } from "lucide-react";
import type { ToolSummary } from "../api/types";
import { useWorkspace } from "../state/WorkspaceContext";
import { useToolPackRevision } from "../state/toolPackChanges";
import { useConfirmation } from "./DialogSystem";

interface NewMissionButtonProps {
  className?: string;
  children?: ReactNode;
}

export function NewMissionButton({ className = "button primary", children }: NewMissionButtonProps) {
  const { api, coreState, engagement, previewMode, providers, startMission } = useWorkspace();
  const availableProviders = useMemo(() => providers.filter((provider) => provider.enabled), [providers]);
  const [open, setOpen] = useState(false);
  const [objective, setObjective] = useState("");
  const [providerId, setProviderId] = useState("");
  const provider = availableProviders.find((item) => item.id === providerId);
  const [model, setModel] = useState("");
  const [durationMinutes, setDurationMinutes] = useState(60);
  const [maxTokens, setMaxTokens] = useState(20_000);
  const [maxCost, setMaxCost] = useState(10);
  const [maxRetries, setMaxRetries] = useState(1);
  const [assignedTools, setAssignedTools] = useState<ToolSummary[]>([]);
  const [selectedTools, setSelectedTools] = useState<string[]>([]);
  const [toolConfigurationAvailable, setToolConfigurationAvailable] = useState(false);
  const [maxToolCalls, setMaxToolCalls] = useState(0);
  const [maxConcurrency, setMaxConcurrency] = useState(1);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string>();
  const toolPackRevision = useToolPackRevision();

  useEffect(() => {
    if (availableProviders.some((item) => item.id === providerId)) return;
    const next = availableProviders[0];
    setProviderId(next?.id ?? "");
    setModel(next?.defaultModel ?? next?.models[0] ?? "");
  }, [availableProviders, providerId]);

  useEffect(() => {
    let active = true;
    setSelectedTools([]);
    setMaxToolCalls(0);
    setMaxConcurrency(1);
    if (!api || coreState !== "online" || !engagement) {
      setAssignedTools([]);
      setToolConfigurationAvailable(false);
      return () => { active = false; };
    }
    void Promise.all([api.listEngagementToolAssignments(engagement.id), api.listTools(), api.listToolPacks()])
      .then(([assignments, tools, packs]) => {
        if (!active) return;
        const readyDigests = new Set(packs
          .filter((pack) => pack.status === "ready")
          .map((pack) => pack.manifestDigest));
        setToolConfigurationAvailable(true);
        setAssignedTools(tools.filter((tool) => assignments.some((assignment) => assignment.enabled
          && assignment.manifestDigest !== undefined
          && readyDigests.has(assignment.manifestDigest)
          && assignment.manifestDigest === tool.packManifestDigest
          && assignment.toolNames.includes(tool.name))));
      })
      .catch(() => {
        if (!active) return;
        setAssignedTools([]);
        setToolConfigurationAvailable(false);
      });
    return () => { active = false; };
  }, [api, coreState, engagement?.id, toolPackRevision]);

  const verification = provider?.capabilityVerifications?.[model];
  const providerSupportsTools = verification?.status === "verified";
  const toolSelectionMessage = !toolConfigurationAvailable
    ? coreState !== "online"
      ? "Nebula Core is offline; reconnect Core before configuring Toolbox capabilities."
      : "Toolbox configuration APIs are unavailable in this Core."
    : !providerSupportsTools
      ? verification?.status === "failed"
        ? `Tool verification failed for ${model}: ${verification.failureDetail ?? "the provider did not return a valid structured call"}. Reverify it in Settings.`
        : model
          ? `Tool calling has not been verified for ${model}. Verify it in Settings.`
          : "Select an exact model and verify tool calling in Settings."
      : assignedTools.length === 0
        ? "No ready Toolbox capabilities are assigned to this engagement."
        : undefined;

  useEffect(() => {
    if (providerSupportsTools) return;
    setSelectedTools([]);
    setMaxToolCalls(0);
    setMaxConcurrency(1);
  }, [providerSupportsTools]);

  const selectProvider = (id: string) => {
    const next = availableProviders.find((item) => item.id === id);
    setProviderId(id);
    setModel(next?.defaultModel ?? next?.models[0] ?? "");
  };

  const openMission = () => {
    setError(undefined);
    const defaults = providerSupportsTools
      ? assignedTools.filter((tool) => tool.available && tool.name.startsWith("environment.")).map((tool) => tool.name)
      : [];
    setSelectedTools(defaults);
    setMaxToolCalls(defaults.length ? 50 : 0);
    setMaxConcurrency(defaults.length ? 2 : 1);
    setOpen(true);
  };

  useEffect(() => {
    if (!open || !providerSupportsTools) return;
    const defaults = assignedTools
      .filter((tool) => tool.available && tool.name.startsWith("environment."))
      .map((tool) => tool.name);
    if (!defaults.length) return;
    setSelectedTools(defaults);
    setMaxToolCalls(50);
    setMaxConcurrency(2);
  }, [open, providerSupportsTools, assignedTools]);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    const cleanObjective = objective.trim();
    const cleanModel = model.trim();
    if (!engagement) {
      setError("Select an engagement before starting a mission.");
      return;
    }
    if (!provider) {
      setError("Select an enabled provider before starting a mission.");
      return;
    }
    if (!cleanObjective) {
      setError("Enter a mission objective.");
      return;
    }
    if (!cleanModel) {
      setError("Enter the exact model ID for this mission.");
      return;
    }
    if (!Number.isInteger(durationMinutes) || durationMinutes < 1 || durationMinutes > 60) {
      setError("Duration must be a whole number from 1 to 60 minutes.");
      return;
    }
    if (!Number.isInteger(maxTokens) || maxTokens < 1 || maxTokens > 200_000) {
      setError("Token limit must be a whole number from 1 to 200,000.");
      return;
    }
    if (!Number.isFinite(maxCost) || maxCost < 0 || maxCost > 100) {
      setError("Cost limit must be from $0 to $100.");
      return;
    }
    if (!Number.isInteger(maxRetries) || maxRetries < 0 || maxRetries > 2) {
      setError("Retries must be a whole number from 0 to 2.");
      return;
    }
    if (selectedTools.length && (!Number.isInteger(maxToolCalls) || maxToolCalls < 1 || maxToolCalls > 100)) {
      setError("Maximum tool calls must be a whole number from 1 to 100.");
      return;
    }
    if (selectedTools.length && (!Number.isInteger(maxConcurrency) || maxConcurrency < 1 || maxConcurrency > 2)) {
      setError("Maximum concurrency must be 1 or 2.");
      return;
    }
    setSaving(true);
    setError(undefined);
    try {
      await startMission({ engagementId: engagement.id, objective: cleanObjective, providerId: provider.id, model: cleanModel, maxDurationSeconds: durationMinutes * 60, maxTokens, maxCostUsd: maxCost, maxRetries, toolNames: selectedTools, maxToolCalls: selectedTools.length ? maxToolCalls : 0, maxConcurrency: selectedTools.length ? maxConcurrency : 1 });
      setOpen(false);
      setObjective("");
      setSelectedTools([]);
      setMaxToolCalls(0);
      setMaxConcurrency(1);
    } catch (startError) {
      setError(startError instanceof Error ? startError.message : "Could not start the mission.");
    } finally {
      setSaving(false);
    }
  };

  return <>
    <button className={className} type="button" disabled={previewMode || !engagement || availableProviders.length === 0} title={availableProviders.length ? undefined : "Add an enabled provider before starting a mission"} onClick={openMission}>{children ?? <><Play size={16} /> New mission</>}</button>
    {open && createPortal(<div className="dialog-backdrop"><form noValidate className="provider-dialog resource-dialog mission-dialog" role="dialog" aria-modal="true" aria-labelledby="mission-dialog-title" onSubmit={(event) => void submit(event)}><header><div><small>{selectedTools.length ? "Supervised Toolbox mission" : "Analysis-only mission"}</small><h2 id="mission-dialog-title">New mission</h2></div><button className="icon-button subtle" type="button" aria-label="Close mission dialog" onClick={() => setOpen(false)}><X size={17} /></button></header><label>Objective<textarea required rows={4} value={objective} placeholder="Review the bounded engagement data and identify evidence-backed risks…" onChange={(event) => { setObjective(event.target.value); setError(undefined); }} /></label><label>Provider<select value={providerId} onChange={(event) => { selectProvider(event.target.value); setError(undefined); }}>{availableProviders.map((item) => <option value={item.id} key={item.id}>{item.name}</option>)}</select></label><label>Model<input required value={model} list="mission-models" placeholder="Exact model ID" onChange={(event) => { setModel(event.target.value); setError(undefined); }} /><datalist id="mission-models">{provider?.models.map((item) => <option value={item} key={item} />)}</datalist></label><div className="resource-form-grid"><label>Duration (minutes)<input type="number" min={1} max={60} value={durationMinutes} onChange={(event) => setDurationMinutes(Number(event.target.value))} /></label><label>Token limit<input type="number" min={1} max={200000} value={maxTokens} onChange={(event) => setMaxTokens(Number(event.target.value))} /></label><label>Cost limit (USD)<input type="number" min={0} max={100} step="0.01" value={maxCost} onChange={(event) => setMaxCost(Number(event.target.value))} /></label><label>Retries<input type="number" min={0} max={2} value={maxRetries} onChange={(event) => setMaxRetries(Number(event.target.value))} /></label></div><section className="mission-tool-selection"><header><div><Wrench size={15} /><span><strong>Toolbox capabilities</strong><small>The installed image discovers the actual commands at runtime.</small></span></div><span>{selectedTools.length ? `${selectedTools.length} selected` : "Analysis only"}</span></header>{toolConfigurationAvailable && providerSupportsTools && assignedTools.length ? <fieldset className="resource-checklist"><legend>Assigned environment capabilities</legend>{assignedTools.map((tool) => <label key={tool.name}><input type="checkbox" checked={selectedTools.includes(tool.name)} disabled={!tool.available} onChange={(event) => { const next = event.target.checked ? [...selectedTools, tool.name] : selectedTools.filter((name) => name !== tool.name); setSelectedTools(next); setMaxToolCalls(next.length ? Math.max(maxToolCalls, 50) : 0); setMaxConcurrency(next.length ? Math.max(maxConcurrency, 2) : 1); }} /><span><strong>{tool.name}</strong><small>{tool.riskClass.replaceAll("_", " ")}{tool.requiresApproval ? " · approval required" : ""}{tool.unavailableReason ? ` · ${tool.unavailableReason}` : ""}</small></span></label>)}</fieldset> : <div className="mission-tool-empty" role="status"><ShieldCheck size={17} /><p>{toolSelectionMessage}</p></div>}{selectedTools.length > 0 && <div className="resource-form-grid"><label>Maximum tool calls<input type="number" min={1} max={100} value={maxToolCalls} onChange={(event) => setMaxToolCalls(Number(event.target.value))} /></label><label>Maximum concurrency<input type="number" min={1} max={2} value={maxConcurrency} onChange={(event) => setMaxConcurrency(Number(event.target.value))} /></label></div>}</section><p className="provider-dialog-note">{selectedTools.length ? "Ordinary in-scope commands run without per-command approval. Core still enforces the engagement scope, container boundary, budgets, evidence capture, and high-risk approval rules." : "This mission is analysis-only: the request carries an empty capability list and a zero tool-call budget."}</p>{error && <p className="form-error" role="alert">{error}</p>}<footer><button className="button secondary" type="button" onClick={() => setOpen(false)}>Cancel</button><button className="button primary" type="submit" disabled={saving}>{saving ? "Starting…" : "Start mission"}</button></footer></form></div>, document.body)}
  </>;
}

const terminalStatuses = new Set(["failed", "complete", "cancelled"]);

export function StopMissionButton({ className = "button secondary" }: { className?: string }) {
  const confirm = useConfirmation();
  const { previewMode, run, stopMission } = useWorkspace();
  const [stopping, setStopping] = useState(false);
  const [error, setError] = useState<string>();
  const disabled = previewMode || !run || terminalStatuses.has(run.status) || run.status === "cancelling";
  const stop = async () => {
    if (!run || !await confirm({
      title: "Stop this mission?",
      message: `“${run.title}” will be cancelled after the current safe boundary. Persisted events and evidence will be retained.`,
      confirmLabel: "Stop mission",
      tone: "danger",
    })) return;
    setStopping(true);
    setError(undefined);
    try {
      await stopMission(run.id, { reason: "Stopped by the operator from the workspace" });
    } catch (stopError) {
      setError(stopError instanceof Error ? stopError.message : "Could not stop the mission.");
    } finally {
      setStopping(false);
    }
  };
  return <span className="mission-stop-control"><button className={className} type="button" disabled={disabled || stopping} onClick={() => void stop()}><Square size={14} /> {stopping ? "Stopping…" : run?.status === "cancelling" ? "Cancelling…" : "Stop mission"}</button>{error && <small role="alert">{error}</small>}</span>;
}
