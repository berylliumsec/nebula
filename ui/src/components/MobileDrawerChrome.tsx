import { ChevronDown, RefreshCw, Settings } from "lucide-react";
import { useNavigate } from "react-router-dom";
import { useOptionalChrome } from "../state/ChromeContext";
import { useWorkspace } from "../state/WorkspaceContext";
import { isNebulaShell } from "../hooks/useCompactLayout";
import { projectSurface } from "../resourceRoutes";

/** Current project at the top of the phone conversation drawer. */
export function MobileDrawerProject({ onNavigate }: { onNavigate: () => void }) {
  const chrome = useOptionalChrome();
  const navigate = useNavigate();
  const { engagement, engagements } = useWorkspace();
  if (!engagement) return null;
  const initial = (engagement.name.trim()[0] ?? "N").toUpperCase();
  if (isNebulaShell()) return <details className="mobile-drawer-project-picker">
    <summary className="mobile-drawer-project" aria-label={`Switch project, current ${engagement.name}`}>
      <span className="mobile-project-avatar" aria-hidden="true">{initial}</span>
      <span className="mobile-drawer-project-copy"><strong>{engagement.name}</strong><small>Choose a project</small></span>
      <ChevronDown size={18} aria-hidden="true" />
    </summary>
    <div className="mobile-drawer-project-options" role="group" aria-label="Projects">
      {engagements.map(project => <button type="button" key={project.id} aria-current={project.id === engagement.id ? "true" : undefined} onClick={() => {
        onNavigate();
        navigate(`${projectSurface(project.id, "workbench")}?view=chat`);
      }}>{project.name}</button>)}
    </div>
  </details>;
  return <button className="mobile-drawer-project" type="button" aria-label={`Switch project, current ${engagement.name}`} disabled={!chrome?.openProjectPicker} onClick={() => { onNavigate(); chrome?.openProjectPicker?.(); }}>
    <span className="mobile-project-avatar" aria-hidden="true">{initial}</span>
    <span className="mobile-drawer-project-copy"><strong>{engagement.name}</strong><small>{engagement.workspacePath ?? "Nebula workspace"}</small></span>
    <ChevronDown size={18} aria-hidden="true" />
  </button>;
}

const stateLabels = { starting: "Connecting", bootstrapping: "Connecting", ready: "Connected", degraded: "Limited connection", failed: "Disconnected" } as const;

/** One quiet connection line and Settings, replacing the header status icons. */
export function MobileDrawerFooter({ onNavigate }: { onNavigate: () => void }) {
  const navigate = useNavigate();
  const { reconnect, workspaceState } = useWorkspace();
  const canRetry = workspaceState === "failed" || workspaceState === "degraded";
  const tone = workspaceState === "ready" ? "healthy" : canRetry ? "unavailable" : "warning";
  return <footer className="mobile-drawer-footer">
    <span className={`status-dot ${tone}`} aria-hidden="true" />
    <span className="mobile-drawer-connection" role="status"><strong>{stateLabels[workspaceState]}</strong><small>{window.location.host}</small></span>
    {canRetry && <button className="icon-button subtle" type="button" aria-label="Retry connection" onClick={reconnect}><RefreshCw size={18} aria-hidden="true" /></button>}
    {isNebulaShell()
      ? <a className="icon-button subtle" href="nebula://settings" aria-label="App connection" title="App connection" onClick={onNavigate}><Settings size={19} aria-hidden="true" /></a>
      : <button className="icon-button subtle" type="button" aria-label="Settings" onClick={() => { onNavigate(); navigate("/settings"); }}><Settings size={19} aria-hidden="true" /></button>}
  </footer>;
}
