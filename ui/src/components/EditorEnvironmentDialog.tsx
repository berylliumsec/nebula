import { useCallback, useEffect, useState } from "react";
import { Box, CheckCircle2, ExternalLink, FolderSync, LoaderCircle, RefreshCw, ShieldCheck, X } from "lucide-react";
import type { ApiClient } from "../api/client";
import type { ContainerTerminalCapabilities } from "../api/types";
import { DiagnosticErrorNotice, logCaughtDiagnostic } from "../diagnostics";
import { ModalSurface } from "./DialogSystem";

interface EditorEnvironmentDialogProps {
  api: ApiClient;
  engagementId: string;
  workspacePath?: string;
  onClose(): void;
  onOpenTerminal?: () => void;
}

export function EditorEnvironmentDialog({ api, engagementId, workspacePath, onClose, onOpenTerminal }: EditorEnvironmentDialogProps) {
  const [capabilities, setCapabilities] = useState<ContainerTerminalCapabilities>();
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string>();
  const [revision, setRevision] = useState(0);
  const refresh = useCallback(() => setRevision((current) => current + 1), []);

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setError(undefined);
    void api.containerTerminalCapabilities(engagementId, controller.signal).then((result) => {
      if (!controller.signal.aborted) setCapabilities(result);
    }).catch((caughtError) => {
      if (controller.signal.aborted) return;
      void logCaughtDiagnostic("interface.code_editor.environment", "The editor could not inspect the existing project runtime.", caughtError, "code_editor");
      setError(caughtError instanceof Error ? caughtError.message : "Could not inspect the project runtime.");
    }).finally(() => {
      if (!controller.signal.aborted) setLoading(false);
    });
    return () => controller.abort();
  }, [api, engagementId, revision]);

  return <ModalSurface className="editor-environment-dialog" labelledBy="editor-environment-title" onClose={onClose}>
    <header><div><small>Shared project context</small><h2 id="editor-environment-title">Workspace environment</h2></div><button className="icon-button subtle" type="button" aria-label="Close workspace environment" onClick={onClose}><X size={17} /></button></header>
    <div className="editor-environment-grid">
      <section aria-labelledby="editor-workspace-authority"><FolderSync size={19} /><div><small>Persistent file authority</small><h3 id="editor-workspace-authority">{workspacePath ? "Linked host folder" : "Nebula-managed project workspace"}</h3><code>{workspacePath ?? "Managed by Nebula Core"}</code><p>Code, agents, project operations, and Terminal resolve these same files. Returning to Code checks open tabs for external changes.</p></div></section>
      <section aria-labelledby="editor-container-workspace"><Box size={19} /><div><small>Container mount</small><h3 id="editor-container-workspace"><code>/workspace</code></h3><p>The existing Kali Terminal mounts the persistent project folder here. The container is disposable; project files are not.</p></div></section>
      <section aria-labelledby="editor-runtime-status"><ShieldCheck size={19} /><div><small>Security workstation</small><h3 id="editor-runtime-status">{loading ? "Checking runtime…" : capabilities?.ready ? "Kali runtime ready" : "Runtime needs attention"}</h3>{loading ? <p><LoaderCircle className="spin" size={14} /> Reading the authoritative runtime capability.</p> : capabilities ? <><code>{capabilities.sourceImage}</code><p>{capabilities.ready ? <><CheckCircle2 size={14} /> Ready for an isolated Terminal using shared <code>{capabilities.workspace}</code>.</> : capabilities.detail ?? "Open Terminal to prepare or diagnose the existing workstation runtime."}</p></> : null}</div></section>
    </div>
    {error && <><DiagnosticErrorNotice error={error} fallback="The runtime status check failed." compact /><button className="button quiet editor-environment-retry" type="button" onClick={refresh}><RefreshCw size={13} /> Retry status check</button></>}
    <footer><span>Runtime creation, command history, and shell controls stay in Terminal.</span><div><button className="button quiet" type="button" onClick={onClose}>Close</button>{onOpenTerminal && <button className="button primary" type="button" onClick={() => { onClose(); onOpenTerminal(); }}><ExternalLink size={14} /> Open Terminal</button>}</div></footer>
  </ModalSurface>;
}
