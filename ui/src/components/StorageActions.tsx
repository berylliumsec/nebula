import { Fragment, useEffect, useState } from "react";
import type { ApiClient } from "../api/client";
import type { StorageAction } from "../api/types";
import { logCaughtDiagnostic } from "../diagnostics";
import "../storage.css";

function Effect({ value }: { value: Record<string, unknown> }) {
  return <li className="storage-effect"><dl>{Object.entries(value).map(([key, entry]) => <Fragment key={key}>
    <dt>{key.replaceAll("_", " ")}</dt><dd>{typeof entry === "string" || typeof entry === "number" || typeof entry === "boolean" ? String(entry) : JSON.stringify(entry)}</dd>
  </Fragment>)}</dl></li>;
}

export function StorageActions({ api, actions, onRefresh }: { api: ApiClient; actions: StorageAction[]; onRefresh: () => Promise<void> }) {
  const [busy, setBusy] = useState<string>();
  const [error, setError] = useState("");
  const call = async (action: StorageAction, step: "preview" | "approve" | "reconcile") => {
    setBusy(action.id); setError("");
    try {
      try { await api.storageAction(action.id, step, step === "approve" ? action.digest ?? undefined : undefined); }
      catch (caught) {
        logCaughtDiagnostic("interface.storage.action_failed", "A storage source action could not be completed.", caught, "storage-actions");
        setError(String(caught));
      }
      try { await onRefresh(); }
      catch (caught) {
        logCaughtDiagnostic("interface.storage.action_refresh_failed", "Storage action status could not be refreshed.", caught, "storage-actions");
        setError((current) => current || String(caught));
      }
    } finally { setBusy(undefined); }
  };
  if (!actions.length) return null;
  return <section className="storage-actions" aria-label="Storage actions"><h3>Source actions</h3>
    <p>These actions come from the configured source. Nebula shows exact effects before any change.</p>
    {error && <p role="alert" className="storage-error">{error}</p>}
    {actions.map((action) => <article className="storage-action panel" key={action.id}>
      <div><strong>{action.title}</strong><small>{action.status}</small></div>
      {action.status === "offered" || action.status === "failed" || action.status === "expired" ?
        <button type="button" className="button secondary" disabled={Boolean(busy)} onClick={() => void call(action, "preview")}>Preview effects</button> : null}
      {action.status === "previewed" && <><p>Preview expires {action.expires_at ? new Date(action.expires_at).toLocaleString() : "soon"}.</p>
        <ul>{action.effects.map((effect, index) => <Effect value={effect} key={index} />)}</ul>
        <button type="button" className="button primary" disabled={Boolean(busy) || !action.digest || (action.expires_at ? Date.parse(action.expires_at) <= Date.now() : true)} onClick={() => void call(action, "approve")}>Approve exact plan and execute</button>
      </>}
      {(action.status === "uncertain" || action.status === "executing") && <button type="button" className="button secondary" disabled={Boolean(busy)} onClick={() => void call(action, "reconcile")}>Check action status</button>}
      {action.error && <p role="alert">{action.error}</p>}
      {action.receipt && <details><summary>Execution receipt</summary><pre>{JSON.stringify(action.receipt, null, 2)}</pre></details>}
    </article>)}
  </section>;
}

export function StorageChatActions({ api, sessionId }: { api: ApiClient | undefined; sessionId: string }) {
  const [actions, setActions] = useState<StorageAction[]>([]);
  const [incident, setIncident] = useState(false);
  const refresh = async () => {
    if (!api || !sessionId) return;
    const data = await api.storageDashboard();
    const linked = data.incidents.find((item) => item.chat_session_id === sessionId);
    setIncident(Boolean(linked));
    setActions(linked ? data.actions.filter((item) => item.incident_id === linked.id) : []);
  };
  useEffect(() => { setIncident(false); setActions([]); void refresh().catch((caught) => {
    logCaughtDiagnostic("interface.storage.chat_actions_load_failed", "Storage source actions could not be loaded in chat.", caught, "storage-chat");
  }); }, [api, sessionId]);
  if (!api || !incident) return null;
  return <div className="storage-chat-actions"><p>Storage incident · source reports are untrusted data. Remote changes require your approval.</p><StorageActions api={api} actions={actions} onRefresh={refresh} /></div>;
}
