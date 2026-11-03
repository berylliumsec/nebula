import {useEffect, useRef, useState, type FormEvent} from "react";
import {RefreshCw} from "lucide-react";
import type {ApiClient} from "../api/client";
import {getHostCpuBudget, saveHostCpuBudget, HOST_CPU_BUDGET_CHANGED, type HostCpuBudget} from "../api/hostCpuBudget";
import {logCaughtDiagnostic} from "../diagnostics/logger";
import {announceSettingsSaved} from "./SettingsSaveFeedback";

export function HostCpuBudgetSettings({api, previewMode = false}: {api?: ApiClient; previewMode?: boolean}) {
  const [saved, setSaved] = useState<HostCpuBudget>();
  const [automatic, setAutomatic] = useState(true);
  const [draft, setDraft] = useState("");
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string>();
  const [notice, setNotice] = useState<string>();
  const [reload, setReload] = useState(0);
  const [conflict, setConflict] = useState(false);
  const generation = useRef(0);
  const dirty = !!saved && (automatic ? saved.cpu_limit !== null : Number(draft) !== saved.cpu_limit);

  useEffect(() => {
    const current = ++generation.current;
    setSaved(undefined);
    setNotice(undefined);
    setError(undefined);
    setSaving(false);
    setLoading(false);
    setAutomatic(true);
    setDraft("");
    setConflict(false);
    if (!api || previewMode) return;
    const controller = new AbortController();
    setLoading(true);
    void getHostCpuBudget(api, controller.signal).then(value => {
      if (controller.signal.aborted || generation.current !== current) return;
      setSaved(value);
      setAutomatic(value.cpu_limit === null);
      setDraft(String(value.cpu_limit ?? value.automatic_cpu_limit));
    }).catch(caught => {
      if (controller.signal.aborted || generation.current !== current) return;
      void logCaughtDiagnostic("interface.host_cpu.load_failed", "The host CPU budget could not be read.", caught, "settings");
      setError(caught instanceof Error ? caught.message : "Could not load this host's CPU budget.");
    }).finally(() => {if (generation.current === current) setLoading(false);});
    return () => {controller.abort(); ++generation.current;};
  }, [api, previewMode, reload]);

  useEffect(() => {
    const refresh = () => {
      if (dirty) {
        setConflict(true);
        setError("The host CPU budget changed elsewhere. Reload before saving your draft.");
      } else setReload(value => value + 1);
    };
    window.addEventListener(HOST_CPU_BUDGET_CHANGED, refresh);
    return () => window.removeEventListener(HOST_CPU_BUDGET_CHANGED, refresh);
  }, [dirty]);

  useEffect(() => {
    if (!api || !saved || previewMode) return;
    const current = generation.current;
    const refresh = () => void getHostCpuBudget(api).then(value => {
      if (current !== generation.current) return;
      if (dirty && (value.host_id !== saved.host_id || value.revision !== saved.revision)) {
        setConflict(true);
        setError("The host CPU budget changed elsewhere. Reload before saving your draft.");
      } else if (!dirty) {
        setSaved(value); setAutomatic(value.cpu_limit === null);
        setDraft(String(value.cpu_limit ?? value.automatic_cpu_limit));
      }
    }).catch(caught => {
      if (current !== generation.current) return;
      void logCaughtDiagnostic("interface.host_cpu.refresh_failed", "The host CPU budget could not be refreshed.", caught, "settings");
      setError("Could not refresh this host's CPU budget. Reload to retry.");
    });
    window.addEventListener("focus", refresh);
    return () => window.removeEventListener("focus", refresh);
  }, [api, saved, dirty, previewMode]);

  const number = Number(draft);
  const valid = automatic || (!!draft.trim() && Number.isFinite(number) && number >= 0.01 && !!saved && number <= saved.cpu_capacity);
  const unavailable = !api || previewMode || loading || saving;
  const canSave = !unavailable && !conflict && !!saved && valid && (dirty || ["pending", "failed", "unavailable"].includes(saved.apply_state));

  const save = async (event: FormEvent) => {
    event.preventDefault();
    if (!api || !saved || !canSave) return;
    const current = generation.current;
    const target = automatic ? null : number;
    setSaving(true); setError(undefined); setNotice(undefined);
    try {
      const updated = await saveHostCpuBudget(api, saved, target);
      const observed = await getHostCpuBudget(api);
      if (current !== generation.current) return;
      if (observed.host_id !== saved.host_id || observed.revision !== updated.revision || observed.cpu_limit !== target) {
        throw new Error("Core did not confirm this host's CPU budget. Reload before saving again.");
      }
      setSaved(observed);
      setAutomatic(observed.cpu_limit === null);
      setDraft(String(observed.cpu_limit ?? observed.automatic_cpu_limit));
      announceSettingsSaved("CPU budget saved on this Core host.");
      setNotice(observed.apply_state === "applied"
        ? `Saved and applied: ${observed.applied_cpu_limit} CPUs.`
        : observed.detail);
    } catch (caught) {
      if (current !== generation.current) return;
      void logCaughtDiagnostic("interface.host_cpu.save_failed", "The host CPU budget could not be confirmed.", caught, "settings");
      if (caught instanceof Error && /409|changed/.test(caught.message)) setConflict(true);
      setError(caught instanceof Error ? caught.message : "Could not save this host's CPU budget.");
    } finally {if (current === generation.current) setSaving(false);}
  };

  return <section className="panel setup-card host-cpu-settings" aria-labelledby="host-cpu-title">
    <header><div><small>Core host{saved ? ` · ${saved.host_name}` : ""}</small><h3 id="host-cpu-title">CPU budget</h3></div></header>
    <p>Shared background computation across projects on this host.</p>
    {saved && <p>{saved.cpu_capacity} CPUs available. Automatic uses {saved.automatic_cpu_limit}.</p>}
    <form onSubmit={event => void save(event)} className="host-cpu-form">
      <label><span>CPU budget mode</span><select value={automatic ? "automatic" : "custom"} onChange={event => {setAutomatic(event.target.value === "automatic"); setNotice(undefined);}} disabled={unavailable || !saved}>
        <option value="automatic">Automatic</option><option value="custom">Custom</option>
      </select></label>
      {!automatic && <label><span>Maximum background CPUs</span><input type="number" min="0.01" max={saved?.cpu_capacity} step="any" inputMode="decimal" value={draft} onChange={event => {setDraft(event.target.value); setNotice(undefined);}} disabled={unavailable} /></label>}
      <div className="host-cpu-actions"><button className="button primary" type="submit" disabled={!canSave}>{saving ? "Saving…" : "Save CPU budget"}</button><button className="button quiet" type="button" aria-label="Reload CPU budget" title="Reload CPU budget" onClick={() => setReload(value => value + 1)} disabled={unavailable}><RefreshCw size={16} /></button></div>
    </form>
    {previewMode && <p role="status">Connect to Core to configure its CPU budget.</p>}
    {loading && <p role="status">Loading CPU budget…</p>}
    {saved && <p role="status">{saved.apply_state === "applied" ? `Applied: ${saved.applied_cpu_limit} CPUs.` : saved.detail}</p>}
    {saved?.capacity_clamped && <p role="status">The saved limit exceeds current capacity; the effective limit is {saved.effective_cpu_limit} CPUs.</p>}
    {!valid && <p role="alert">Enter a CPU limit from 0.01 up to {saved?.cpu_capacity ?? "this host's capacity"}.</p>}
    {error && <p role="alert">{error}</p>}
    {notice && <p role="status">{notice}</p>}
  </section>;
}
