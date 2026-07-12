import { useEffect, useMemo, useState, type FormEvent, type ReactNode } from "react";
import { Play, Square, X } from "lucide-react";
import { useWorkspace } from "../state/WorkspaceContext";

interface NewMissionButtonProps {
  className?: string;
  children?: ReactNode;
}

export function NewMissionButton({ className = "button primary", children }: NewMissionButtonProps) {
  const { engagement, previewMode, providers, startMission } = useWorkspace();
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
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string>();

  useEffect(() => {
    if (availableProviders.some((item) => item.id === providerId)) return;
    const next = availableProviders[0];
    setProviderId(next?.id ?? "");
    setModel(next?.defaultModel ?? next?.models[0] ?? "");
  }, [availableProviders, providerId]);

  const selectProvider = (id: string) => {
    const next = availableProviders.find((item) => item.id === id);
    setProviderId(id);
    setModel(next?.defaultModel ?? next?.models[0] ?? "");
  };

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (!engagement || !provider || !model.trim()) return;
    setSaving(true);
    setError(undefined);
    try {
      await startMission({ engagementId: engagement.id, objective, providerId: provider.id, model: model.trim(), maxDurationSeconds: durationMinutes * 60, maxTokens, maxCostUsd: maxCost, maxRetries });
      setOpen(false);
      setObjective("");
    } catch (startError) {
      setError(startError instanceof Error ? startError.message : "Could not start the mission.");
    } finally {
      setSaving(false);
    }
  };

  return <>
    <button className={className} type="button" disabled={previewMode || !engagement || availableProviders.length === 0} title={availableProviders.length ? undefined : "Add an enabled provider before starting a mission"} onClick={() => { setError(undefined); setOpen(true); }}>{children ?? <><Play size={16} /> New mission</>}</button>
    {open && <div className="dialog-backdrop"><form className="provider-dialog resource-dialog" role="dialog" aria-modal="true" aria-labelledby="mission-dialog-title" onSubmit={(event) => void submit(event)}><header><div><small>Analysis-only mission</small><h2 id="mission-dialog-title">New mission</h2></div><button className="icon-button subtle" type="button" aria-label="Close mission dialog" onClick={() => setOpen(false)}><X size={17} /></button></header><label>Objective<textarea required rows={4} value={objective} placeholder="Review the bounded engagement data and identify evidence-backed risks…" onChange={(event) => setObjective(event.target.value)} /></label><label>Provider<select value={providerId} onChange={(event) => selectProvider(event.target.value)}>{availableProviders.map((item) => <option value={item.id} key={item.id}>{item.name}</option>)}</select></label><label>Model<input required value={model} list="mission-models" placeholder="Exact model ID" onChange={(event) => setModel(event.target.value)} /><datalist id="mission-models">{provider?.models.map((item) => <option value={item} key={item} />)}</datalist></label><div className="resource-form-grid"><label>Duration (minutes)<input type="number" min={1} max={60} value={durationMinutes} onChange={(event) => setDurationMinutes(Number(event.target.value))} /></label><label>Token limit<input type="number" min={1} max={200000} value={maxTokens} onChange={(event) => setMaxTokens(Number(event.target.value))} /></label><label>Cost limit (USD)<input type="number" min={0} max={100} step="0.01" value={maxCost} onChange={(event) => setMaxCost(Number(event.target.value))} /></label><label>Retries<input type="number" min={0} max={2} value={maxRetries} onChange={(event) => setMaxRetries(Number(event.target.value))} /></label></div><p className="provider-dialog-note">This mission is analysis-only: executable tools are disabled and the Core enforces zero tool calls.</p>{error && <p className="form-error" role="alert">{error}</p>}<footer><button className="button secondary" type="button" onClick={() => setOpen(false)}>Cancel</button><button className="button primary" type="submit" disabled={saving}>{saving ? "Starting…" : "Start mission"}</button></footer></form></div>}
  </>;
}

const terminalStatuses = new Set(["failed", "complete", "cancelled"]);

export function StopMissionButton({ className = "button secondary" }: { className?: string }) {
  const { previewMode, run, stopMission } = useWorkspace();
  const [stopping, setStopping] = useState(false);
  const [error, setError] = useState<string>();
  const disabled = previewMode || !run || terminalStatuses.has(run.status) || run.status === "cancelling";
  const stop = async () => {
    if (!run || !window.confirm(`Stop “${run.title}”? Active analysis will be cancelled after the current safe boundary.`)) return;
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
