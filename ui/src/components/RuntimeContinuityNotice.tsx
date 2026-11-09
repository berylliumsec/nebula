import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { RefreshCw } from "lucide-react";
import type { ApiClient } from "../api/client";
import type { PendingRequest } from "../api/durableRequests";
import { logCaughtDiagnostic } from "../diagnostics";

/** Recovery here performs reads only. Drafts, decisions and resumes require an explicit action. */
export function RuntimeContinuityNotice({api}: {api?: ApiClient}) {
  const [records, setRecords] = useState<PendingRequest[]>([]);
  const [error, setError] = useState<string>();
  const [busy, setBusy] = useState(false);
  const scope = api?.getContinuityScope();
  useEffect(() => {
    if (!api) { setRecords([]); return; }
    let active = true;
    const controller = new AbortController();
    const load = async (reconcile = false) => {
      try {
        let items = await api.getPendingRequests();
        if (reconcile && items.length && scope?.persistent) items = await api.reconcilePendingRequests(controller.signal);
        if (active) setRecords(items);
      } catch (caught) {
        if (active) {
          logCaughtDiagnostic("interface.continuity.saved_requests_load_failed", "Saved requests could not be loaded or checked.", caught, "saved-requests-load");
          setError(caught instanceof Error ? caught.message : "Saved requests could not be checked. Retry when Core is reachable.");
        }
      }
    };
    void load(true);
    const unsubscribe = api.subscribeContinuity(() => { void load(); });
    return () => { active = false; controller.abort(); unsubscribe(); };
  }, [api, scope?.key]);

  const check = async () => {
    if (!api || busy) return;
    setBusy(true); setError(undefined);
    try { setRecords(await api.reconcilePendingRequests()); }
    catch (caught) {
      logCaughtDiagnostic("interface.continuity.saved_requests_check_failed", "Saved requests could not be checked.", caught, "saved-requests-check");
      setError(caught instanceof Error ? caught.message : "Saved requests could not be checked.");
    }
    finally { setBusy(false); }
  };
  if (!api || (!records.length && !scope?.limitation && !error)) return null;
  return <aside className="chat-recovery-notice" aria-label="Saved requests">
    {scope?.limitation && <p role="status">{scope.limitation}</p>}
    {error && <p role="alert">{error}</p>}
    {!!records.length && <details open={records.some(item => item.state === "uncertain" || item.state === "failed")}>
      <summary>{records.length} {records.length === 1 ? "request awaiting confirmation" : "requests awaiting confirmation"}</summary>
      <p>Check saved work before sending it again. Closing this window does not stop accepted work.</p>
      <button className="button quiet" type="button" onClick={() => void check()} disabled={busy}><RefreshCw size={16} aria-hidden="true" />{busy ? "Checking saved requests…" : "Check saved requests"}</button>
      <ul>{records.map(record => {
        const status = record.status;
        const project = status?.project_id ?? record.projectId;
        const session = status?.session_id ?? record.sessionId;
        return <li key={record.key}>
          <p>{record.summary || (record.path.startsWith("approvals/") ? "Approval decision" : "Saved request")} · {status?.state ?? (record.state === "accepted" ? "Saved; waiting for Core" : "Acceptance unconfirmed")}</p>
          {!record.replayable && <p>Only the request identity was saved. Check its outcome before retrying from the original screen.</p>}
          {project && session && <Link className="button quiet" to={`/projects/${encodeURIComponent(project)}/workbench?view=chat&session=${encodeURIComponent(session)}`}>Open saved conversation</Link>}
          {status?.state === "succeeded" && <button className="button quiet" type="button" onClick={() => void api.dismissCompletedRequest(record.key).then(() => setRecords(items => items.filter(item => item.key !== record.key))).catch(caught => {
            logCaughtDiagnostic("interface.continuity.saved_request_dismiss_failed", "A completed request could not be dismissed.", caught, "saved-request-dismiss");
            setError(String(caught));
          })}>Dismiss completed request</button>}
        </li>;
      })}</ul>
    </details>}
  </aside>;
}
