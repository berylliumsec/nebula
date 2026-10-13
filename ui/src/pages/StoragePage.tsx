import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { Database, RefreshCw } from "lucide-react";
import type { StorageDashboard } from "../api/types";
import { PageHeader } from "../components/PageHeader";
import { StandardEmptyState } from "../components/SurfacePrimitives";
import { StorageActions } from "../components/StorageActions";
import { logCaughtDiagnostic } from "../diagnostics";
import { useWorkspace } from "../state/WorkspaceContext";
import "../storage.css";

function bytes(value: number | null): string {
  if (value == null) return "Unknown";
  if (value === 0) return "0 B";
  const order = Math.min(4, Math.floor(Math.log(value) / Math.log(1024)));
  return `${(value / 1024 ** order).toFixed(order ? 1 : 0)} ${["B", "KiB", "MiB", "GiB", "TiB"][order]}`;
}

export function StoragePage() {
  const { api, coreState, engagements } = useWorkspace();
  const [data, setData] = useState<StorageDashboard>();
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const refresh = async () => {
    if (!api) return;
    setLoading(true); setError("");
    try { setData(await api.storageDashboard()); }
    catch (caught) {
      logCaughtDiagnostic("interface.storage.dashboard_load_failed", "The Storage dashboard could not be loaded.", caught, "storage-dashboard");
      setError(String(caught));
    }
    finally { setLoading(false); }
  };
  const retryDispatch = async (dispatchId: string) => {
    if (!api) return;
    try { await api.retryStorageDispatch(dispatchId); await refresh(); }
    catch (caught) {
      logCaughtDiagnostic("interface.storage.dispatch_retry_failed", "The storage assistant chat could not be retried.", caught, "storage-dashboard");
      setError(String(caught));
    }
  };
  useEffect(() => { void refresh(); }, [api, coreState]);
  useEffect(() => {
    if (!api) return;
    const timer = window.setInterval(() => { if (document.visibilityState === "visible") void refresh(); }, 30000);
    return () => window.clearInterval(timer);
  }, [api]);
  const devices = useMemo(() => data?.devices ?? [], [data]);
  const open = data?.incidents.filter((item) => !item.resolved_at) ?? [];
  return <section className="page storage-page">
    <PageHeader title="Storage" description="Current capacity, report freshness, incidents, and approved source actions."
      actions={<><button className="button quiet" type="button" disabled={loading} onClick={() => void refresh()} aria-label="Refresh storage"><RefreshCw size={16} /> Refresh</button><Link className="button secondary" to="/settings#storage-integration-settings">Manage sources</Link></>} />
    {error && <p className="storage-error" role="alert">Storage could not refresh. Existing data may be old. <button className="button quiet" onClick={() => void refresh()}>Retry</button><span>{error}</span></p>}
    {!data && loading && <p role="status">Loading storage…</p>}
    {data && !data.sources.length && <StandardEmptyState icon={<Database size={24} />} title="No storage sources" explanation="Add a source in Settings → Integrations → Storage. It starts disabled until you connect your sender." />}
    {data && data.sources.length > 0 && <>
      <div className="storage-summary" aria-label="Storage summary"><article className="panel"><strong>{data.sources.filter((item) => item.enabled).length}</strong><span>Enabled sources</span></article><article className="panel"><strong>{devices.length}</strong><span>Devices</span></article><article className="panel"><strong>{open.length}</strong><span>Open incidents</span></article><article className="panel"><strong>{data.volumes.filter((item) => item.stale).length}</strong><span>Stale volumes</span></article></div>
      <section className="storage-section"><h2>Sources and devices</h2>{data.sources.map((source) => <article className="panel storage-source-card" key={source.id}>
        <header><div><h3>{source.name}</h3><small>{source.enabled ? "Enabled" : "Disabled"} · {engagements.find((item) => item.id === source.engagement_id)?.name ?? "Project unavailable"} · {source.backend} assistant</small></div><span className={`storage-state ${!source.enabled ? "disabled" : source.last_report_at && Date.now() - Date.parse(source.last_report_at) > source.reporting_interval_seconds * 1000 ? "stale" : "healthy"}`}>{!source.enabled ? "Disabled" : !source.last_report_at ? "Awaiting first report" : Date.now() - Date.parse(source.last_report_at) > source.reporting_interval_seconds * 1000 ? "Report overdue" : "Reporting"}</span></header>
        <p>{source.last_report_at ? `Last report ${new Date(source.last_report_at).toLocaleString()}` : "Awaiting first report"}</p>
        {devices.filter((item) => item.source_id === source.id).map((device) => <div className="storage-device" key={device.id}><strong>{device.label}</strong><small>{device.last_report_at ? `Reported ${new Date(device.last_report_at).toLocaleString()}` : "Inventory only · no report yet"}</small>
          {data.volumes.filter((item) => item.source_id === source.id && item.device_id === device.device_id).map((volume) => <div className="storage-volume" key={volume.id}>
            <span><strong>{volume.label}</strong><small>{volume.path}</small></span>
            <span>{volume.free_bytes != null && volume.total_bytes != null ? `${bytes(volume.free_bytes)} free of ${bytes(volume.total_bytes)}` : "Capacity unavailable"}</span>
            <span className={`storage-state ${volume.stale ? "stale" : volume.state}`}>{volume.stale ? `Stale · last ${new Date(volume.received_at).toLocaleString()}` : volume.state}</span>
          </div>)}
        </div>)}
      </article>)}</section>
      <section className="storage-section"><h2>Incidents</h2>{data.incidents.length ? [...data.incidents].reverse().map((incident) => {
        const volume = data.volumes.find((item) => item.id === incident.volume_key);
        const source = data.sources.find((item) => item.id === incident.source_id);
        const dispatches = data.dispatches.filter((item) => item.incident_id === incident.id);
        return <article className="panel storage-incident" key={incident.id}><header><div><h3>{volume?.label ?? "Storage volume"} · {source?.name ?? "Unknown source"}</h3><small>Opened {new Date(incident.opened_at).toLocaleString()}</small></div><span className={`storage-state ${incident.state}`}>{incident.state}</span></header>
          <div className="storage-incident-actions"><Link className="button secondary" to={`/projects/${encodeURIComponent(source?.engagement_id ?? "")}/workbench?view=chat&session=${encodeURIComponent(incident.chat_session_id)}`}>Open assistant chat</Link></div>
          {incident.callback_error && <p className="storage-error" role="alert">Source diagnostics unavailable: {incident.callback_error}</p>}
          {dispatches.filter((item) => item.status === "failed" || item.queue_status === "missing").map((item) => <p className="storage-error" key={item.id} role="alert">Chat dispatch failed: {item.error ?? item.queue_detail} <button className="button quiet" type="button" onClick={() => void retryDispatch(item.id)}>Retry</button></p>)}
          {dispatches.filter((item) => item.queue_status === "needs_review").map((item) => <p className="storage-error" key={item.id} role="alert">Assistant response needs review: {item.queue_detail ?? "The configured runtime could not finish."} Open the linked chat to inspect and retry.</p>)}
          {api && <StorageActions api={api} actions={data.actions.filter((item) => item.incident_id === incident.id)} onRefresh={refresh} />}
        </article>;
      }) : <p>No incidents yet. Warning, critical, and unavailable reports will appear here.</p>}</section>
    </>}
  </section>;
}
