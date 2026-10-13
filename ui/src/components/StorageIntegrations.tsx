import { useEffect, useMemo, useRef, useState, type FormEvent } from "react";
import { Link } from "react-router-dom";
import { Copy, Database, RefreshCw } from "lucide-react";
import type { HarnessProfile, StorageSource } from "../api/types";
import { useWorkspace } from "../state/WorkspaceContext";
import "../storage.css";

export function StorageIntegrations() {
  const { api, engagements, providers, previewMode } = useWorkspace();
  const [sources, setSources] = useState<StorageSource[]>([]);
  const [harnesses, setHarnesses] = useState<HarnessProfile[]>([]);
  const nameRef = useRef<HTMLInputElement>(null);
  const [projectId, setProjectId] = useState("");
  const [runtime, setRuntime] = useState("");
  const [model, setModel] = useState("");
  const [interval, setInterval] = useState(900);
  const [callbackUrl, setCallbackUrl] = useState("");
  const [callbackRef, setCallbackRef] = useState("");
  const [issued, setIssued] = useState<{ token: string; webhook_url: string; source: StorageSource }>();
  const [editing, setEditing] = useState<StorageSource>();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");

  const refresh = async () => {
    if (!api) return;
    const [dashboard, choices] = await Promise.all([api.storageDashboard(), api.listHarnesses()]);
    setSources(dashboard.sources);
    setHarnesses(choices);
  };
  useEffect(() => { void refresh().catch((caught) => setError(String(caught))); }, [api]);
  useEffect(() => { if (!projectId && engagements.length) setProjectId(engagements[0].id); }, [engagements, projectId]);

  const runtimes = useMemo(() => [
    ...providers.filter((item) => item.enabled).map((item) => ({ key: `provider:${item.id}`, name: item.name, models: item.models, defaultModel: item.effectiveDefaultModel ?? item.defaultModel })),
    ...harnesses.filter((item) => item.enabled).map((item) => ({ key: `harness:${item.id}`, name: item.name, models: item.models, defaultModel: item.defaultModel })),
  ], [providers, harnesses]);
  useEffect(() => {
    if (!runtime && runtimes.length) {
      setRuntime(runtimes[0].key);
      setModel(runtimes[0].defaultModel ?? runtimes[0].models[0] ?? "");
    }
  }, [runtime, runtimes]);
  const chosenRuntime = runtimes.find((item) => item.key === runtime);
  const webhook = api ? new URL(`${api.baseUrl.replace(/\/$/, "")}/storage/events`, window.location.href).toString() : "";

  const save = async (event: FormEvent) => {
    event.preventDefault();
    if (!api || !chosenRuntime || !model || !projectId) return;
    const name = nameRef.current?.value ?? "";
    setBusy(true); setError(""); setMessage("");
    try {
      const [backend, profile_id] = runtime.split(":", 2);
      const result = await api.createStorageSource({ name, engagement_id: projectId, backend, profile_id, model,
        reporting_interval_seconds: interval, callback_url: callbackUrl.trim() || null,
        callback_credential_ref: callbackRef.trim() || null });
      setIssued(result); if (nameRef.current) nameRef.current.value = ""; await refresh();
      setMessage(`${result.source.name} was added disabled. Save the token, configure the sender, then enable it.`);
    } catch (caught) { setError(String(caught)); }
    finally { setBusy(false); }
  };
  const change = async (source: StorageSource, changes: Record<string, unknown>): Promise<boolean> => {
    if (!api) return false;
    setBusy(true); setError(""); setMessage("");
    try { await api.updateStorageSource(source, changes); await refresh(); return true; }
    catch (caught) { setError(String(caught)); return false; }
    finally { setBusy(false); }
  };
  const copy = async (value: string, inputId: string) => {
    try {
      await navigator.clipboard.writeText(value);
      setMessage("Copied to clipboard.");
    } catch {
      document.getElementById(inputId)?.focus();
      (document.getElementById(inputId) as HTMLInputElement | null)?.select();
      setMessage("Copy the selected value from the field.");
    }
  };
  const saveConfiguration = async (event: FormEvent, source: StorageSource) => {
    event.preventDefault();
    if (!editing) return;
    const saved = await change(source, {
      reporting_interval_seconds: editing.reporting_interval_seconds,
      callback_url: editing.callback_url?.trim() || null,
      callback_credential_ref: editing.callback_credential_ref?.trim() || null,
    });
    if (saved) { setEditing(undefined); setMessage(`Settings saved for ${source.name}.`); }
  };
  const rotate = async (source: StorageSource) => {
    if (!api) return;
    setBusy(true); setError("");
    try { const result = await api.rotateStorageSource(source.id); setIssued({ ...result, webhook_url: webhook }); await refresh();
      setMessage(`Token rotated for ${source.name}. Update the sender before its next report.`); }
    catch (caught) { setError(String(caught)); }
    finally { setBusy(false); }
  };
  const revoke = async (source: StorageSource) => {
    if (!api || !window.confirm(`Revoke ${source.name}'s token and disable its reports?`)) return;
    setBusy(true); setError("");
    try { await api.revokeStorageSource(source.id); await refresh(); setMessage(`${source.name} was revoked.`); }
    catch (caught) { setError(String(caught)); }
    finally { setBusy(false); }
  };

  return <section className="settings-section storage-settings" id="storage-integration-settings">
    <div className="section-heading"><div><h2>Storage</h2><p>Receive reports from any storage system</p></div><Link className="button quiet" to="/storage">Open dashboard</Link></div>
    <p className="storage-note">Each source sends the <a href="https://github.com/berylliumsec/nebula/blob/main/docs/storage-integrations.md" target="_blank" rel="noreferrer">versioned storage format</a>. Reports and assistant alerts stay off until you enable the source.</p>
    {error && <p role="alert" className="storage-error">{error}</p>}
    {message && <p role="status">{message}</p>}
    {issued && <div className="storage-issued panel" role="status"><strong>Save this token now · it is shown once</strong>
      <label>Webhook URL<input id="storage-webhook-url" readOnly value={issued.webhook_url} onFocus={(event) => event.target.select()} /></label>
      <button className="button quiet" type="button" onClick={() => void copy(issued.webhook_url, "storage-webhook-url")}><Copy size={15} /> Copy URL</button>
      <label>Source token<input id="storage-source-token" readOnly value={issued.token} onFocus={(event) => event.target.select()} /></label>
      <button className="button quiet" type="button" onClick={() => void copy(issued.token, "storage-source-token")}><Copy size={15} /> Copy token</button>
      <p>Send a test report to <code>/api/v1/storage/test</code> before enabling live delivery. This validates the token and JSON without creating a device.</p>
      <button className="button secondary" type="button" onClick={() => setIssued(undefined)}>Done</button>
    </div>}
    <form className="storage-source-form panel" onSubmit={(event) => void save(event)}>
      <h3>Add source</h3>
      <div className="storage-form-grid">
        <label>Source name<input required maxLength={200} ref={nameRef} placeholder="NAS, cloud account, backup server…" /></label>
        <label>Project<select required value={projectId} onChange={(event) => setProjectId(event.target.value)}><option value="">Choose project</option>{engagements.map((item) => <option value={item.id} key={item.id}>{item.name}</option>)}</select></label>
        <label>Assistant runtime<select required value={runtime} onChange={(event) => { const next = runtimes.find((item) => item.key === event.target.value); setRuntime(event.target.value); setModel(next?.defaultModel ?? next?.models[0] ?? ""); }}><option value="">Choose runtime</option>{runtimes.map((item) => <option value={item.key} key={item.key}>{item.name} · {item.key.startsWith("harness") ? "harness" : "provider"}</option>)}</select></label>
        <label>Model<select required aria-label="Storage model" value={model} onChange={(event) => setModel(event.target.value)}><option value="">Choose model</option>{chosenRuntime?.models.map((item) => <option value={item} key={item}>{item}</option>)}</select></label>
        <label>Reporting interval (seconds)<input type="number" min={60} max={604800} value={interval} onChange={(event) => setInterval(Number(event.target.value))} /></label>
      </div>
      <details><summary>Optional diagnostic and action callbacks</summary><div className="storage-form-grid">
        <label>Callback origin<input type="url" value={callbackUrl} onChange={(event) => setCallbackUrl(event.target.value)} placeholder="https://storage.example" /></label>
        <label>Credential reference<input value={callbackRef} onChange={(event) => setCallbackRef(event.target.value)} placeholder="env:STORAGE_CALLBACK_TOKEN" /></label>
      </div><p>Only this operator-configured destination receives callback requests. Every remote change still requires a preview and approval.</p></details>
      <button className="button primary" disabled={busy || previewMode || !chosenRuntime || !model || !projectId} type="submit">Add disabled source</button>
    </form>
    <div className="storage-source-list" aria-label="Storage sources">{sources.length ? sources.map((source) => <article className="panel" key={source.id}>
      <div className="storage-source-heading"><Database size={19} /><div><strong>{source.name}</strong><small>{source.enabled ? "Enabled" : "Disabled"} · {engagements.find((item) => item.id === source.engagement_id)?.name ?? "Project unavailable"} · {source.last_report_at ? `Last report ${new Date(source.last_report_at).toLocaleString()}` : "No reports yet"}</small></div></div>
      <label className="storage-source-toggle"><input type="checkbox" checked={source.enabled} disabled={busy || previewMode} onChange={(event) => void change(source, { enabled: event.target.checked })} /> Enable reports and alerts</label>
      <div className="storage-source-actions"><button className="button quiet" disabled={busy || previewMode} onClick={() => setEditing(editing?.id === source.id ? undefined : source)} type="button">{editing?.id === source.id ? "Cancel settings" : "Edit reporting and callbacks"}</button><button className="button quiet" disabled={busy || previewMode} onClick={() => void rotate(source)} type="button"><RefreshCw size={15} /> Rotate token</button><button className="button quiet" disabled={busy || previewMode} onClick={() => void revoke(source)} type="button">Revoke</button></div>
      {editing?.id === source.id && <form className="storage-source-edit" onSubmit={(event) => void saveConfiguration(event, source)}>
        <div className="storage-form-grid">
          <label>Reporting interval (seconds)<input type="number" min={60} max={604800} required value={editing.reporting_interval_seconds} onChange={(event) => setEditing({ ...editing, reporting_interval_seconds: Number(event.target.value) })} /></label>
          <label>Callback origin<input type="url" value={editing.callback_url ?? ""} onChange={(event) => setEditing({ ...editing, callback_url: event.target.value })} placeholder="https://storage.example" /></label>
          <label>Credential reference<input value={editing.callback_credential_ref ?? ""} onChange={(event) => setEditing({ ...editing, callback_credential_ref: event.target.value })} placeholder="env:STORAGE_CALLBACK_TOKEN" /></label>
        </div>
        <button className="button secondary" type="submit" disabled={busy || previewMode}>Save source settings</button>
      </form>}
    </article>) : <p>No storage sources yet. Add one above to get a webhook token.</p>}</div>
  </section>;
}
