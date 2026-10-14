import { useEffect, useState } from "react";
import type { ApiClient } from "../api/client";
import type { NotebookFileIndexStatus } from "../api/types";
import { logCaughtDiagnostic } from "../diagnostics";
import { HostFolderPicker } from "./HostFolderPicker";

export function NotebookFileIndexSettings({api, projectId, listing, onChanged}: {
  api: ApiClient; projectId: string; listing?: boolean; onChanged: () => void;
}) {
  const [status, setStatus] = useState<NotebookFileIndexStatus>();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  useEffect(() => {
    let current = true;
    void api.notebookFileIndex(projectId).then(value => {if (current) setStatus(value);}).catch(caught => {
      logCaughtDiagnostic("interface.notebook.index_status_failed", "File index status unavailable.", caught);
      if (current) setError("File index status unavailable.");
    });
    return () => {current = false;};
  }, [api, projectId, listing]);
  const connect = async (path: string) => {
    setBusy(true); setError("");
    try {
      setStatus(await api.setNotebookFileIndex(projectId, path));
      onChanged();
    } catch (caught) {
      logCaughtDiagnostic("interface.notebook.index_connect_failed", "File index could not be connected.", caught);
      setError("File index could not be connected.");
    }
    finally { setBusy(false); }
  };
  const remove = async () => {
    setBusy(true); setError("");
    try {
      setStatus(await api.removeNotebookFileIndex(projectId));
      onChanged();
    } catch (caught) {
      logCaughtDiagnostic("interface.notebook.index_remove_failed", "File index could not be removed.", caught);
      setError("File index could not be removed.");
    }
    finally { setBusy(false); }
  };
  return <section className="notebook-index-settings" aria-label="Notebook file index">
    <p>Use a host SQLite repository file index to find notebooks faster. The project folder still owns the files.</p>
    {status?.path && <p role="status"><strong>{status.state === "fallback" ? "Scanning project folder" : "File index connected"}</strong> <code>{status.path}</code>{status.detail && <span> {status.detail}</span>}</p>}
    <HostFolderPicker api={api} purpose="index" value={status?.path ?? undefined} onSelect={path => {void connect(path);}} />
    {status?.path && <button className="button quiet" type="button" disabled={busy} onClick={() => void remove()}>Remove index</button>}
    {busy && <span role="status">Checking file index…</span>}
    {error && <p role="alert">{error}</p>}
  </section>;
}
