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
  const [enabled, setEnabled] = useState(false);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string>();
  const [notice, setNotice] = useState<string>();
  const [reload, setReload] = useState(0);
  const [conflict, setConflict] = useState(false);
  const generation = useRef(0);
  const dirty = !!saved && ((automatic ? saved.cpu_limit !== null : Number(draft) !== saved.cpu_limit)
    || (saved.executor_scope === "report-pdfs" && enabled !== !!saved.enabled));
  const managed = saved?.executor_scope === "report-pdfs";
  const minimum = managed ? 1 : 0.01;
  const maximum = saved?.maximum_worker_cpus ?? saved?.cpu_capacity;

  useEffect(() => {
    const current = ++generation.current;
    setSaved(undefined);
    setNotice(undefined);
    setError(undefined);
    setSaving(false);
    setLoading(false);
    setAutomatic(true);
    setEnabled(false);
    setDraft("");
    setConflict(false);
    if (!api || previewMode) return;
    const controller = new AbortController();
    setLoading(true);
    void getHostCpuBudget(api, controller.signal).then(value => {
      if (controller.signal.aborted || generation.current !== current) return;
      setSaved(value);
      setAutomatic(value.cpu_limit === null);
      setEnabled(!!value.enabled);
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
    if (!api || !saved || previewMode || saving) return;
    const current = generation.current;
    const refresh = () => void getHostCpuBudget(api).then(value => {
      if (current !== generation.current) return;
      if (dirty && (value.host_id !== saved.host_id || value.revision !== saved.revision)) {
        setConflict(true);
        setError("The host CPU budget changed elsewhere. Reload before saving your draft.");
      } else if (!dirty) {
        setSaved(value); setAutomatic(value.cpu_limit === null);
        setEnabled(!!value.enabled);
        setDraft(String(value.cpu_limit ?? value.automatic_cpu_limit));
      }
    }).catch(caught => {
      if (current !== generation.current) return;
      void logCaughtDiagnostic("interface.host_cpu.refresh_failed", "The host CPU budget could not be refreshed.", caught, "settings");
      setError("Could not refresh this host's CPU budget. Reload to retry.");
    });
    window.addEventListener("focus", refresh);
    return () => window.removeEventListener("focus", refresh);
  }, [api, saved, dirty, previewMode, saving]);

  useEffect(() => {
    if (!api || previewMode || !saved || saving || (saved.apply_state !== "pending" && !(managed && saved.enabled))) return;
    const controller = new AbortController();
    const current = generation.current;
    let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      try {
        const value = await getHostCpuBudget(api, controller.signal);
        if (controller.signal.aborted || current !== generation.current) return;
        if (value.host_id !== saved.host_id || value.revision !== saved.revision) {
          setConflict(true); setError("The host CPU budget changed elsewhere. Reload before saving your draft.");
          return;
        }
        setSaved(value);
        if (value.apply_state === "pending" || (managed && value.enabled)) {
          timer = setTimeout(() => void poll(), value.apply_state === "pending" ? 500 : 1500);
        }
      } catch (caught) {
        if (controller.signal.aborted || current !== generation.current) return;
        void logCaughtDiagnostic("interface.host_cpu.poll_failed", "Background worker status could not be refreshed.", caught, "settings");
        setError("Could not check background workers. Reload to retry.");
      }
    };
    timer = setTimeout(() => void poll(), 250);
    return () => {controller.abort(); clearTimeout(timer);};
  }, [api, previewMode, saving, managed, saved?.host_id, saved?.revision, saved?.apply_state, saved?.enabled]);

  const number = Number(draft);
  const valid = automatic || (!!draft.trim() && Number.isFinite(number) && number >= minimum && !!saved
    && number <= (maximum ?? saved.cpu_capacity) && (!managed || Number.isInteger(number)));
  const unavailable = !api || previewMode || loading || saving;
  const canSave = !unavailable && !conflict && !!saved && valid && (dirty || ["pending", "failed", "unavailable"].includes(saved.apply_state));

  const save = async (event: FormEvent) => {
    event.preventDefault();
    if (!api || !saved || !canSave) return;
    const current = generation.current;
    const target = automatic ? null : number;
    const targetEnabled = managed ? enabled : undefined;
    setSaving(true); setError(undefined); setNotice(undefined);
    try {
      const updated = await saveHostCpuBudget(api, saved, target, targetEnabled);
      const observed = await getHostCpuBudget(api);
      if (current !== generation.current) return;
      if (observed.host_id !== saved.host_id || observed.revision !== updated.revision || observed.cpu_limit !== target
          || (targetEnabled !== undefined && observed.enabled !== targetEnabled)) {
        throw new Error("Core did not confirm this host's CPU budget. Reload before saving again.");
      }
      setSaved(observed);
      setAutomatic(observed.cpu_limit === null);
      setEnabled(!!observed.enabled);
      setDraft(String(observed.cpu_limit ?? observed.automatic_cpu_limit));
      announceSettingsSaved("CPU budget saved on this Core host.");
      setNotice(managed ? undefined : observed.apply_state === "applied"
        ? `Saved and applied: ${observed.applied_cpu_limit} CPUs.` : observed.detail);
    } catch (caught) {
      if (current !== generation.current) return;
      void logCaughtDiagnostic("interface.host_cpu.save_failed", "The host CPU budget could not be confirmed.", caught, "settings");
      if (caught instanceof Error && /409|changed/.test(caught.message)) setConflict(true);
      setError(caught instanceof Error ? caught.message : "Could not save this host's CPU budget.");
    } finally {if (current === generation.current) setSaving(false);}
  };

  return <section className="panel setup-card host-cpu-settings" aria-labelledby="host-cpu-title">
    <header><div><small>Core host{saved ? ` · ${saved.host_name}` : ""}</small><h3 id="host-cpu-title">CPU budget</h3></div></header>
    <p>{managed ? "PDF generation across projects on this host. Chat runtimes, external tools and GPUs use their own execution paths." : "Shared background computation across projects on this host."}</p>
    {saved && <p>{saved.cpu_capacity} CPUs available. Automatic uses {saved.automatic_cpu_limit}.</p>}
    <form onSubmit={event => void save(event)} className="host-cpu-form">
      {managed && <label className="host-cpu-enable"><input type="checkbox" checked={enabled}
        onChange={event => {setEnabled(event.target.checked); setNotice(undefined);}}
        disabled={unavailable || (!saved?.activation_supported && !enabled)} /><span>Enable background PDF workers</span></label>}
      <label><span>CPU budget mode</span><select value={automatic ? "automatic" : "custom"} onChange={event => {setAutomatic(event.target.value === "automatic"); setNotice(undefined);}} disabled={unavailable || !saved}>
        <option value="automatic">Automatic</option><option value="custom">Custom</option>
      </select></label>
      {!automatic && <label><span>Maximum background CPUs</span><input type="number" min={minimum} max={maximum} step={managed ? 1 : "any"} inputMode={managed ? "numeric" : "decimal"} value={draft} onChange={event => {setDraft(event.target.value); setNotice(undefined);}} disabled={unavailable} /></label>}
      <div className="host-cpu-actions"><button className="button primary" type="submit" disabled={!canSave}>{saving ? "Saving…" : "Save CPU budget"}</button><button className="button quiet" type="button" aria-label="Reload CPU budget" title="Reload CPU budget" onClick={() => setReload(value => value + 1)} disabled={unavailable}><RefreshCw size={16} /></button></div>
    </form>
    {previewMode && <p role="status">Connect to Core to configure its CPU budget.</p>}
    {loading && <p role="status">Loading CPU budget…</p>}
    {saved && <p role="status">{!managed && saved.apply_state === "applied" ? `Applied: ${saved.applied_cpu_limit} CPUs.` : saved.detail}</p>}
    {managed && saved && <p>{saved.worker_count == null || saved.active_jobs == null || saved.queued_jobs == null
      ? "Worker counts unavailable."
      : `${saved.worker_count} workers · ${saved.active_jobs} active · ${saved.queued_jobs} queued`}</p>}
    {managed && !saved?.activation_supported && <p>{saved?.activation_unavailable_reason}</p>}
    {saved?.capacity_clamped && <p role="status">The effective limit is {saved.effective_cpu_limit} whole worker CPUs.</p>}
    {!valid && <p role="alert">Enter {managed ? "a whole CPU limit" : "a CPU limit"} from {minimum} up to {maximum ?? "this host's capacity"}.</p>}
    {error && <p role="alert">{error}</p>}
    {notice && <p role="status">{notice}</p>}
  </section>;
}
