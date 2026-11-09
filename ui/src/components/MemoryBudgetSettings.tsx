import {useEffect, useRef, useState, type FormEvent} from "react";
import {RefreshCw} from "lucide-react";
import type {ApiClient} from "../api/client";
import {GIB, MEMORY_BUDGET_CHANGED, applyMemoryLimits, getMemoryJobs, getMemorySettings, memoryAmount, probeMemoryRunner, saveMemorySettings, type MemoryEnvironment, type MemoryJob, type MemoryLimits, type MemoryPolicy, type MemorySettings} from "../api/memoryBudgets";
import {logCaughtDiagnostic} from "../diagnostics/logger";
import {announceSettingsSaved} from "./SettingsSaveFeedback";
import styles from "./MemoryBudgetSettings.module.css";

const environments: MemoryEnvironment[] = ["local", "container", "ssh", "remote", "cpu-worker"];
const message = (error: unknown) => error instanceof Error ? error.message : "Memory budgets could not be updated. Reload to check Core state.";
const clone = <T,>(value: T): T => structuredClone(value);

function LimitsFields({name, value, onChange, maximum, swapMaximum, pressure}: {name: string; value: MemoryLimits; onChange: (limits: MemoryLimits) => void; maximum: number; swapMaximum: number; pressure: boolean}) {
  return <div className={styles.fields}>
    <label>{name} ceiling (GiB)<input type="number" min={1 / 64} max={maximum / GIB} step="any" required value={value.max_bytes / GIB} onChange={event => onChange({...value, max_bytes: Math.round(Number(event.target.value) * GIB)})} /><small>0.016–{memoryAmount(maximum)}</small></label>
    <label>{name} swap (GiB)<input type="number" min={0} max={swapMaximum / GIB} step="any" required value={value.swap_bytes / GIB} onChange={event => onChange({...value, swap_bytes: Math.round(Number(event.target.value) * GIB)})} /><small>0–{memoryAmount(swapMaximum)}; zero disables swap</small></label>
    <label>{name} pressure threshold (GiB)<input type="number" min={1 / 64} max={value.max_bytes / GIB} step="any" disabled={!pressure && value.high_bytes === null} value={value.high_bytes === null ? "" : value.high_bytes / GIB} onChange={event => onChange({...value, high_bytes: event.target.value === "" ? null : Math.round(Number(event.target.value) * GIB)})} /><small>{pressure ? "Optional; must be at or below the ceiling" : "Unavailable on this runner; leave empty"}</small></label>
  </div>;
}

function RunningLimits({api, job, settings, onApplied}: {api: ApiClient; job: MemoryJob; settings: MemorySettings; onApplied: () => void}) {
  const [limits, setLimits] = useState(job.evidence?.applied ?? job.snapshot.limits);
  const [ack, setAck] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string>();
  const capability = settings.capabilities.find(item => item.profile_id === job.snapshot.profile_id && item.environment === job.snapshot.environment);
  const submit = async (event: FormEvent) => {
    event.preventDefault(); if (busy || !capability?.live_update) return;
    setBusy(true); setError(undefined);
    try {await applyMemoryLimits(api, job, limits, ack); onApplied();}
    catch (caught) {void logCaughtDiagnostic("interface.memory.apply_failed", "Running memory limits could not be verified.", caught, "settings"); setError(message(caught));}
    finally {setBusy(false);}
  };
  return <details><summary>Change this running job’s limits</summary>
    <p>Lowering a limit can terminate this job. This action does not retry work or resume a goal.</p>
    {!capability?.live_update && <p>This runner cannot apply running limits. Save a budget for new jobs, stop this job, then retry explicitly.</p>}
    {error && <p role="alert">{error}</p>}
    <form onSubmit={event => void submit(event)}><fieldset className={styles.fields} disabled={busy || !capability?.live_update}><LimitsFields name="Running job" value={limits} onChange={setLimits} maximum={settings.administrator_limits.maximum_job_bytes} swapMaximum={settings.administrator_limits.maximum_swap_bytes} pressure={!!capability?.pressure_threshold} /></fieldset>
      <label className={styles.ack}><input type="checkbox" checked={ack} disabled={busy || !capability?.live_update} onChange={event => setAck(event.target.checked)} />I understand that lowering a limit can terminate this job.</label>
      <button className="button" type="submit" disabled={busy || !capability?.live_update}>{busy ? "Checking application…" : "Apply to this job"}</button>
    </form>
  </details>;
}

export function MemoryBudgetSettings({api, previewMode = false}: {api?: ApiClient; previewMode?: boolean}) {
  const [saved, setSaved] = useState<MemorySettings>();
  const [policy, setPolicy] = useState<MemoryPolicy>();
  const [jobs, setJobs] = useState<MemoryJob[]>([]);
  const [jobLimit, setJobLimit] = useState<number>();
  const [error, setError] = useState<string>();
  const [notice, setNotice] = useState<string>();
  const [busy, setBusy] = useState(false);
  const [reload, setReload] = useState(0);
  const [conflict, setConflict] = useState(false);
  const generation = useRef(0);
  const dirty = !!saved && !!policy && JSON.stringify(saved.policy) !== JSON.stringify(policy);

  useEffect(() => {
    const current = ++generation.current; const controller = new AbortController();
    setSaved(undefined); setPolicy(undefined); setError(undefined); setNotice(undefined); setConflict(false); setJobs([]); setBusy(false);
    if (!api || previewMode) return;
    void Promise.all([getMemorySettings(api, controller.signal), getMemoryJobs(api, controller.signal)]).then(([settings, list]) => {
      if (controller.signal.aborted || current !== generation.current) return;
      setSaved(settings); setPolicy(clone(settings.policy)); setJobs(list.items); setJobLimit(list.at_limit ? list.display_limit : undefined);
    }).catch(caught => {if (!controller.signal.aborted && current === generation.current) {void logCaughtDiagnostic("interface.memory.load_failed", "Memory budget settings could not be read.", caught, "settings"); setError(message(caught));}});
    return () => {controller.abort(); ++generation.current;};
  }, [api, previewMode, reload]);

  useEffect(() => {
    if (!api || !saved || previewMode || busy) return;
    const controller = new AbortController(); const current = generation.current;
    const refresh = async () => {
      try {
        const [settings, list] = await Promise.all([getMemorySettings(api, controller.signal), getMemoryJobs(api, controller.signal)]);
        if (controller.signal.aborted || current !== generation.current) return;
        setJobs(list.items);
        setJobLimit(list.at_limit ? list.display_limit : undefined);
        if (dirty && settings.revision !== saved.revision) {setConflict(true); setError("Memory budgets changed elsewhere. Reload before saving your draft.");}
        else {setSaved(settings); if (!dirty) setPolicy(clone(settings.policy));}
      } catch (caught) {if (!controller.signal.aborted && current === generation.current) {void logCaughtDiagnostic("interface.memory.refresh_failed", "Memory usage could not be refreshed.", caught, "settings"); setError("Could not refresh usage. Reload to check current limits.");}}
    };
    const tick = window.setInterval(() => {if (document.visibilityState === "visible") void refresh();}, 4000);
    window.addEventListener("focus", refresh); window.addEventListener(MEMORY_BUDGET_CHANGED, refresh);
    return () => {controller.abort(); clearInterval(tick); window.removeEventListener("focus", refresh); window.removeEventListener(MEMORY_BUDGET_CHANGED, refresh);};
  }, [api, previewMode, saved?.revision, dirty, busy]);

  const save = async (event: FormEvent) => {
    event.preventDefault(); if (!api || !saved || !policy || busy || conflict) return;
    const current = generation.current; setBusy(true); setError(undefined); setNotice(undefined);
    try {
      const result = await saveMemorySettings(api, saved, policy);
      const observed = await getMemorySettings(api);
      if (current !== generation.current) return;
      if (observed.revision !== result.revision || JSON.stringify(observed.policy) !== JSON.stringify(result.policy)) throw new Error("The save could not be verified. Reload before changing limits again.");
      setSaved(observed); setPolicy(clone(observed.policy)); setNotice("Saved and read back. New jobs use this policy; existing job limits are unchanged.");
      announceSettingsSaved("Memory budgets saved for new jobs"); window.dispatchEvent(new Event(MEMORY_BUDGET_CHANGED));
    } catch (caught) {if (current === generation.current) {void logCaughtDiagnostic("interface.memory.save_failed", "Memory budgets were not verified saved.", caught, "settings"); setError(message(caught)); if (/409|conflict|reload|changed/i.test(message(caught))) setConflict(true);}}
    finally {if (current === generation.current) setBusy(false);}
  };
  const probe = async (profileId: string) => {
    if (!api || busy) return; const current = generation.current; setBusy(true); setError(undefined);
    try {await probeMemoryRunner(api, profileId); const observed = await getMemorySettings(api); if (current === generation.current) {setSaved(observed); setNotice("Runner support checked. Each job still verifies its own kernel limits before starting.");}}
    catch (caught) {if (current === generation.current) {void logCaughtDiagnostic("interface.memory.probe_failed", "Runner memory enforcement could not be checked.", caught, "settings"); setError(message(caught));}}
    finally {if (current === generation.current) setBusy(false);}
  };
  const admin = saved?.administrator_limits;
  const minimumReserve = Math.max(admin?.minimum_reserve_bytes ?? 0, saved?.minimum_core_reserve_bytes ?? 0);
  const pressure = saved?.capabilities.some(item => item.pressure_threshold) ?? false;
  return <section id="memory-budget-settings" className={`settings-section ${styles.root}`} aria-labelledby="memory-budget-title">
    <div className="section-heading"><div><h2 id="memory-budget-title">Memory budgets</h2><p>Host-wide limits for managed compute across projects and runtime versions.</p></div><button className="icon-button subtle" type="button" title="Reload memory budgets and discard drafts" aria-label="Reload memory budgets" onClick={() => setReload(value => value + 1)} disabled={busy || !api || previewMode}><RefreshCw size={16} aria-hidden="true" /></button></div>
    {previewMode && <p>Connect to Core to configure and verify memory limits.</p>}
    {error && <p role="alert">{error}</p>}{notice && <p role="status">{notice}</p>}
    {!saved && !error && !previewMode && <p role="status">Loading memory budgets…</p>}
    {saved && policy && admin && <>
      <p>{saved.detail}</p><div className={styles.summary}><span>Reserved: {memoryAmount(saved.reserved_bytes)}</span><span>Available: {memoryAmount(saved.available_bytes)}</span><span>Effective total: {memoryAmount(saved.effective_aggregate_bytes)}</span><span>{saved.held_jobs} held · {saved.queued_jobs} queued</span></div>
      <form onSubmit={event => void save(event)}><fieldset className={styles.fields} disabled={busy || previewMode || conflict}>
        <LimitsFields name="Per job" value={policy.default_job} onChange={limits => setPolicy({...policy, default_job: limits})} maximum={admin.maximum_job_bytes} swapMaximum={admin.maximum_swap_bytes} pressure={pressure} />
        <label>Total job budget (GiB)<input type="number" required min={1 / 64} max={Math.min(admin.maximum_aggregate_bytes, admin.capacity_bytes - policy.reserve_bytes) / GIB} step="any" value={policy.aggregate_bytes / GIB} onChange={event => setPolicy({...policy, aggregate_bytes: Math.round(Number(event.target.value) * GIB)})} /><small>Ceilings plus swap are reserved before launch</small></label>
        <label>Core and services reserve (GiB)<input type="number" required min={minimumReserve / GIB} max={(admin.capacity_bytes - 16 * 1024 ** 2) / GIB} step="any" value={policy.reserve_bytes / GIB} onChange={event => setPolicy({...policy, reserve_bytes: Math.round(Number(event.target.value) * GIB)})} /><small>At least {memoryAmount(minimumReserve)} of {memoryAmount(admin.capacity_bytes)} host capacity{saved.runtime_reserved_bytes ? `. Resident runtimes reserve ${memoryAmount(saved.runtime_reserved_bytes)}.` : ""}</small></label>
      </fieldset>
      <fieldset className={styles.fields} disabled={busy || previewMode || conflict}><details className={styles.overrides}><summary>Environment and execution-profile defaults</summary><p>Environment defaults replace the host default; permitted profile overrides take precedence. A job may only lower its selected limits.</p>
        {environments.map(environment => <div className={styles.override} key={environment}><label className={styles.ack}><input type="checkbox" checked={!!policy.environment_defaults[environment]} onChange={event => {const values = {...policy.environment_defaults}; if (event.target.checked) values[environment] = clone(policy.default_job); else delete values[environment]; setPolicy({...policy, environment_defaults: values});}} />Override {environment} defaults</label>{policy.environment_defaults[environment] && <LimitsFields name={environment} value={policy.environment_defaults[environment]!} maximum={admin.maximum_job_bytes} swapMaximum={admin.maximum_swap_bytes} pressure={saved.capabilities.some(item => item.environment === environment && item.pressure_threshold)} onChange={limits => setPolicy({...policy, environment_defaults: {...policy.environment_defaults, [environment]: limits}})} />}</div>)}
        {saved.profiles.filter(profile => profile.enabled).map(profile => <div className={styles.override} key={profile.id}><label className={styles.ack}><input type="checkbox" checked={!!policy.profile_overrides[profile.id]} onChange={event => {const values = {...policy.profile_overrides}; if (event.target.checked) values[profile.id] = clone(policy.default_job); else delete values[profile.id]; setPolicy({...policy, profile_overrides: values});}} />Override {profile.name}</label>{policy.profile_overrides[profile.id] && <LimitsFields name={profile.name} value={policy.profile_overrides[profile.id]} maximum={admin.maximum_job_bytes} swapMaximum={admin.maximum_swap_bytes} pressure={!!profile.capabilities?.pressure_threshold} onChange={limits => setPolicy({...policy, profile_overrides: {...policy.profile_overrides, [profile.id]: limits}})} />}</div>)}
      </details></fieldset>
      <div className={styles.actions}><button className="button primary" type="submit" disabled={busy || !dirty || conflict}>{busy ? "Verifying…" : "Save memory budgets"}</button><small>Applies to new jobs. Running and queued jobs keep their snapshots.</small></div></form>
      <details><summary>Enforcement support</summary><ul className={styles.runners}>{saved.profiles.map(profile => <li className={styles.runner} key={profile.id}><header><strong>{profile.name}</strong><button className="button quiet" type="button" disabled={busy || !profile.enabled} onClick={() => void probe(profile.id)}>Check enforcement</button></header><p>{profile.capabilities?.detail ?? "Not checked. Computation stays held until kernel enforcement is verified."}</p></li>)}</ul>{saved.capabilities.filter(item => !item.profile_id).map(item => <p key={item.environment}><strong>{item.environment}</strong> · {item.detail}</p>)}</details>
      <h3>Compute jobs</h3>{jobLimit && <p>Showing up to {jobLimit} {jobLimit === 1 ? "job" : "jobs"}, charged jobs first. More jobs may exist.</p>}{!jobs.length && <p>No compute jobs have been submitted.</p>}<ul className={styles.jobs}>{jobs.map(job => {const limits = job.evidence?.applied ?? job.snapshot.limits; const current = job.evidence?.current_bytes; const ratio = current == null ? null : current / limits.max_bytes; return <li className={styles.job} key={job.id}><header><strong>{job.resource_kind} · {job.resource_id}</strong><span className={styles.state}>{job.outcome ?? job.state} · {job.application_state}</span></header>
        <p>{job.detail ?? job.evidence?.detail ?? "Waiting for verified enforcement."}</p>
        <p>Limit {memoryAmount(limits.max_bytes)} · Swap {memoryAmount(limits.swap_bytes)} · {job.evidence ? "Observed worker limits" : "Policy snapshot; not applied"} · Policy revision {job.snapshot.policy_revision}</p>
        {!!job.snapshot.runner_overhead_bytes && <p>Required helper budget: {memoryAmount(job.snapshot.runner_overhead_bytes)}; released after verified helper cleanup.</p>}
        {ratio !== null ? <><p>{memoryAmount(current!)} measured {ratio >= .8 ? "· Approaching limit" : ""}{job.evidence?.observed_at ? ` · ${new Date(job.evidence.observed_at).toLocaleTimeString()}` : ""}</p><meter className={styles.meter} min={0} max={1} high={.8} value={ratio} aria-label={`${job.resource_id} memory usage`} /></> : <p>Current usage unavailable.</p>}
        {job.evidence?.peak_bytes != null && <p>Peak: {memoryAmount(job.evidence.peak_bytes)}</p>}
        {job.pending_limits && <p>Application pending: requested {memoryAmount(job.pending_limits.max_bytes)}; current observed limits remain shown above.</p>}
        {job.outcome === "memory_limit_exceeded" && <p>Review completed artifacts and logs in the original job. Change the workload or save a new budget, then retry explicitly. Goals remain paused.</p>}
        {job.state === "running" && api && <RunningLimits key={`${job.id}:${job.revision}`} api={api} job={job} settings={saved} onApplied={() => setReload(value => value + 1)} />}
      </li>;})}</ul>
    </>}
  </section>;
}
