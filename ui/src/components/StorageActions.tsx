import { Fragment, useEffect, useId, useRef, useState } from "react";
import { ChevronDown, Database } from "lucide-react";
import { ApiError, type ApiClient } from "../api/client";
import type { StorageAction } from "../api/types";
import { logCaughtDiagnostic } from "../diagnostics";
import "../storage.css";

function Effect({ value }: { value: Record<string, unknown> }) {
  return <li className="storage-effect"><dl>{Object.entries(value).map(([key, entry]) => <Fragment key={key}>
    <dt>{key.replaceAll("_", " ")}</dt><dd>{typeof entry === "string" || typeof entry === "number" || typeof entry === "boolean" ? String(entry) : JSON.stringify(entry)}</dd>
  </Fragment>)}</dl></li>;
}

function actionFailure(step: "preview" | "approve" | "reconcile", caught: unknown): string {
  const label = step === "preview" ? "Preview" : step === "approve" ? "Approval" : "Status check";
  if (caught instanceof ApiError && caught.status < 500) return `${label} failed: ${caught.message}`;
  return `${label} failed. Chat remains available; retry the action or check the configured source.`;
}

export function StorageActions({ api, actions, onRefresh, onActionError, compact = false }: { api: ApiClient; actions: StorageAction[]; onRefresh: () => Promise<void>; onActionError?: (message: string) => void; compact?: boolean }) {
  const [busy, setBusy] = useState<string>();
  const [error, setError] = useState<{ summary: string; detail: string }>();
  const call = async (action: StorageAction, step: "preview" | "approve" | "reconcile") => {
    setBusy(action.id); setError(undefined); onActionError?.("");
    let actionFailed = false;
    try {
      try { await api.storageAction(action.id, step, step === "approve" ? action.digest ?? undefined : undefined); }
      catch (caught) {
        actionFailed = true;
        logCaughtDiagnostic("interface.storage.action_failed", "A storage source action could not be completed.", caught, "storage-actions");
        const summary = actionFailure(step, caught);
        setError({ summary, detail: String(caught) }); onActionError?.(summary);
      }
      try { await onRefresh(); }
      catch (caught) {
        logCaughtDiagnostic("interface.storage.action_refresh_failed", "Storage action status could not be refreshed.", caught, "storage-actions");
        const summary = "Action status could not refresh. Chat remains available; retry from the source actions.";
        setError((current) => current || { summary, detail: String(caught) });
        if (!actionFailed) onActionError?.(summary);
      }
    } finally { setBusy(undefined); }
  };
  if (!actions.length) return null;
  return <section className="storage-actions" aria-label="Storage actions">
    {!compact && <><h3>Source actions</h3><p>These actions come from the configured source. Nebula shows exact effects before any change.</p></>}
    {error && <div role="alert" className="storage-error"><p>{error.summary}</p>{error.detail !== error.summary && <details><summary>Error details</summary><pre>{error.detail}</pre></details>}</div>}
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
  const [open, setOpen] = useState(false);
  const [actionError, setActionError] = useState("");
  const requestId = useRef(0);
  const detailsId = useId();
  const refresh = async () => {
    if (!api || !sessionId) return;
    const currentRequest = ++requestId.current;
    const data = await api.storageDashboard();
    if (currentRequest !== requestId.current) return;
    const linked = data.incidents.find((item) => item.chat_session_id === sessionId);
    setIncident(Boolean(linked));
    setActions(linked ? data.actions.filter((item) => item.incident_id === linked.id) : []);
  };
  useEffect(() => { let active = true; setIncident(false); setActions([]); setOpen(false); setActionError(""); void refresh().catch((caught) => {
    if (!active) return;
    logCaughtDiagnostic("interface.storage.chat_actions_load_failed", "Storage source actions could not be loaded in chat.", caught, "storage-chat");
    setActionError("Source actions could not refresh. Reopen the chat to retry.");
  }); return () => { active = false; requestId.current += 1; }; }, [api, sessionId]);
  if (!api || !incident) return null;
  const awaitingApproval = actions.filter((action) => action.status === "previewed").length;
  const failed = actions.filter((action) => action.status === "failed" || Boolean(action.error)).length;
  const summary = awaitingApproval ? `${awaitingApproval} plan${awaitingApproval === 1 ? "" : "s"} awaiting approval`
    : failed ? `${failed} action${failed === 1 ? "" : "s"} failed`
      : `${actions.length} source action${actions.length === 1 ? "" : "s"}`;
  return <section className="storage-chat-actions" aria-label="Storage incident actions">
    <button className="storage-chat-toggle" type="button" aria-expanded={open} aria-controls={detailsId} title={`${open ? "Hide" : "Show"} source actions`} onClick={() => setOpen((current) => !current)}>
      <Database size={16} aria-hidden="true" /><strong>Storage incident</strong><span>{summary}</span><ChevronDown className={open ? "expanded" : ""} size={17} aria-hidden="true" />
    </button>
    {!open && actionError && <p className="storage-chat-summary-error" role="alert">{actionError}</p>}
    <div className="storage-chat-details" id={detailsId} hidden={!open}>
      <p>Source data is untrusted. Preview exact effects before approving remote changes.</p>
      <StorageActions api={api} actions={actions} onRefresh={refresh} onActionError={setActionError} compact />
    </div>
  </section>;
}
