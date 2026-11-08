import {useEffect, useRef, useState, type FormEvent} from "react";
import {RefreshCw} from "lucide-react";
import type {ApiClient} from "../api/client";
import {getHostPressure, saveHostPressure, HOST_PRESSURE_CHANGED, type HostPressure, type PressureMetric, type PressureThresholds} from "../api/hostPressure";
import {logCaughtDiagnostic} from "../diagnostics/logger";
import {announceSettingsSaved} from "./SettingsSaveFeedback";

const metrics: [PressureMetric, string][] = [["cpu", "CPU use"], ["memory", "Memory use"], ["load", "Runnable tasks / CPU capacity"], ["io", "Core I/O stalls (PSI some avg10)"]];
const draftFor = (value: HostPressure) => Object.fromEntries(metrics.map(([key]) => [key, String(value.saved_thresholds[key] * 100)])) as Record<PressureMetric, string>;
const percent = (value: number | null | undefined) => value == null ? "Unknown" : `${(value * 100).toFixed(1)}%`;

export function HostPressureSettings({api, previewMode = false}: {api?: ApiClient; previewMode?: boolean}) {
  const [saved, setSaved] = useState<HostPressure>();
  const [draft, setDraft] = useState<Record<PressureMetric, string>>({cpu: "80", memory: "80", load: "80", io: "80"});
  const [error, setError] = useState<string>();
  const [conflict, setConflict] = useState(false);
  const [saving, setSaving] = useState(false);
  const [reload, setReload] = useState(0);
  const version = useRef(0);
  const savingRef = useRef(false);
  const savedRef = useRef(saved);
  savedRef.current = saved;
  const dirty = !!saved && metrics.some(([key]) => Number(draft[key]) !== saved.saved_thresholds[key] * 100);
  const dirtyRef = useRef(dirty);
  dirtyRef.current = dirty;
  const valid = metrics.every(([key]) => draft[key].trim() && Number(draft[key]) > 0 && Number(draft[key]) <= 100);

  useEffect(() => {
    ++version.current;
    savedRef.current = undefined; dirtyRef.current = false; savingRef.current = false;
    setSaved(undefined); setError(undefined); setConflict(false); setSaving(false);
    if (!api || previewMode) return;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    const read = async () => {
      const requestVersion = version.current;
      try {
        const value = await getHostPressure(api, controller.signal);
        if (controller.signal.aborted || requestVersion !== version.current || savingRef.current) return;
        const prior = savedRef.current;
        if (dirtyRef.current && prior && (value.revision !== prior.revision || value.host_id !== prior.host_id)) {
          setConflict(true); setError("Host pressure settings changed elsewhere. Reload before saving.");
        } else {
          setSaved(value);
          if (!dirtyRef.current || !prior) setDraft(draftFor(value));
        }
      } catch (caught) {
        if (controller.signal.aborted || requestVersion !== version.current || savingRef.current) return;
        void logCaughtDiagnostic("interface.host_pressure.read_failed", "Host pressure settings could not be read.", caught, "settings");
        setError("Could not read host pressure settings. Reload to retry.");
      } finally {
        if (!controller.signal.aborted) timer = setTimeout(() => void read(), 2000);
      }
    };
    void read();
    return () => {controller.abort(); clearTimeout(timer); ++version.current;};
  }, [api, previewMode, reload]);
  useEffect(() => {
    const refresh = () => {
      if (dirtyRef.current) {setConflict(true); setError("Host pressure settings changed elsewhere. Reload before saving.");}
      else setReload(value => value + 1);
    };
    window.addEventListener(HOST_PRESSURE_CHANGED, refresh);
    return () => window.removeEventListener(HOST_PRESSURE_CHANGED, refresh);
  }, []);

  const save = async (event: FormEvent) => {
    event.preventDefault();
    if (!api || !saved || !valid || saving || conflict) return;
    const generation = ++version.current;
    const thresholds = Object.fromEntries(metrics.map(([key]) => [key, Number(draft[key]) / 100])) as PressureThresholds;
    savingRef.current = true;
    setSaving(true); setError(undefined);
    try {
      const updated = await saveHostPressure(api, saved, thresholds);
      const observed = await getHostPressure(api);
      if (generation !== version.current) return;
      if (observed.host_id !== saved.host_id || observed.revision !== updated.revision || metrics.some(([key]) => observed.effective_thresholds[key] !== thresholds[key])) throw new Error("Core did not confirm the saved thresholds.");
      setSaved(observed); setDraft(draftFor(observed)); setConflict(false);
      announceSettingsSaved("Host pressure thresholds saved and applied.");
    } catch (caught) {
      if (generation !== version.current) return;
      void logCaughtDiagnostic("interface.host_pressure.save_failed", "Host pressure thresholds could not be saved.", caught, "settings");
      setError(caught instanceof Error ? caught.message : "Could not save thresholds. Reload and retry.");
    } finally {if (generation === version.current) {savingRef.current = false; setSaving(false);}}
  };
  return <section className="panel setup-card" aria-labelledby="host-pressure-title">
    <header><h3 id="host-pressure-title">Host pressure</h3></header>
    <p>New starts wait when any reading reaches its threshold. Running work continues.</p>
    <p>Core I/O PSI measures time its tasks stall on I/O. Host load and host I/O are shown for context and do not hold new starts.</p>
    <form onSubmit={event => void save(event)} className="host-cpu-form">
      {metrics.map(([key, label]) => <label key={key}><span>{label} threshold (%)</span>
        <input type="number" inputMode="decimal" min="0.01" max="100" step="any" aria-label={`${label} threshold (%)`} value={draft[key]} disabled={!saved || saving || previewMode} onChange={event => setDraft(value => ({...value, [key]: event.target.value}))} />
        {saved && <small>Current: {percent(saved.readings[key])} · Saved: {percent(saved.saved_thresholds[key])} · Effective: {percent(saved.effective_thresholds[key])}</small>}
      </label>)}
      <div className="host-cpu-actions"><button type="submit" className="button primary" disabled={!saved || !valid || !dirty || saving || conflict || previewMode}>{saving ? "Saving…" : "Save pressure thresholds"}</button>
        <button type="button" className="button quiet" aria-label="Reload host pressure" title="Reload host pressure" disabled={saving} onClick={() => setReload(value => value + 1)}><RefreshCw size={16} /></button></div>
    </form>
    {saved && <><p role="status">{saved.reason ?? "Host pressure allows new starts."}</p><p>Host load average: {percent(saved.sample?.load_average_ratio)} · Host I/O stalls: {percent(saved.sample?.host_io_pressure_ratio)} (read only)</p><p>Provider limit: {saved.concurrency.provider_limit} · Background limit: {saved.concurrency.background_limit}</p></>}
    {!saved && <p role="status">{previewMode ? "Connect to Core to configure host pressure." : "Loading host pressure…"}</p>}
    {error && <p role="alert">{error}</p>}
  </section>;
}
