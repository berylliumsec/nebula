import { useRef, useState, type FormEvent } from "react";
import { Archive, RotateCcw, Check, ChevronDown, LockKeyhole, Orbit, Plus, X } from "lucide-react";
import { NavLink, useLocation, useNavigate } from "react-router-dom";
import { navigationGroups, navigationItems } from "../navigation";
import { canonicalNavigationPath, projectSurface, replaceProjectInPath } from "../resourceRoutes";
import { useWorkspace } from "../state/WorkspaceContext";
import { DiagnosticErrorNotice, logCaughtDiagnostic } from "../diagnostics";
import { HostFolderPicker } from "./HostFolderPicker";
import { useConfirmation, useDialogPresence } from "./DialogSystem";

import "./ProjectSwitcher.css";

interface SideNavProps {
  collapsed: boolean;
  onNavigate: () => void;
  variant?: "standard" | "zero";
}

export function SideNav({ collapsed, onNavigate, variant = "standard" }: SideNavProps) {
  const location = useLocation();
  const navigate = useNavigate();
  const {
    api,
    coreState,
    createEngagement,
    activeOperator,
    engagement,
    engagements,
    archivedEngagements,
    setEngagementArchived,
  } = useWorkspace();
  const confirm = useConfirmation();
  const switcherButton = useRef<HTMLButtonElement>(null);
  const [showArchived, setShowArchived] = useState(false);
  const [updating, setUpdating] = useState(false);
  const [projectError, setProjectError] = useState<string>();
  const [notice, setNotice] = useState<string>();
  const [open, setOpen] = useState(false);
  const [creating, setCreating] = useState(false);
  const [name, setName] = useState("");
  const [clientName, setClientName] = useState("");
  const [workspacePath, setWorkspacePath] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string>();
  useDialogPresence(open);
  const engagementName = engagement?.name ?? "No project available";
  const initials = engagementName
    .split(/\s+/)
    .filter(Boolean)
    .slice(0, 2)
    .map((part) => part[0]?.toUpperCase())
    .join("") || "NE";
  const operatorName = activeOperator?.displayName ?? "No operator profile";
  const operatorInitials = operatorName.split(/\s+/).filter(Boolean).slice(0, 2).map((part) => part[0]?.toUpperCase()).join("") || "OP";

  const changeArchived = async (id: string, projectName: string, archived: boolean, trigger: HTMLButtonElement) => {
    if (updating) return;
    setUpdating(true);
    setProjectError(undefined);
    setNotice(undefined);
    try {
      if (archived && !await confirm({
        title: `Remove ${projectName}?`,
        message: "This archives the project and removes it from your active projects. Files and chat history are kept. Running work continues. You can restore it from Archived projects.",
        confirmLabel: "Remove project",
      })) return;
      const wasSelected = id === engagement?.id;
      const nextId = await setEngagementArchived(id, archived);
      if (archived && wasSelected) {
        navigate(nextId ? projectSurface(nextId, "workbench") : "/", { replace: true });
      }
      setNotice(archived ? "Project removed. You can restore it from Archived projects." : "Project restored. Select it from your active projects.");
      if (!archived) setShowArchived(false);
    } catch (failure) {
      setProjectError(`Could not ${archived ? "remove" : "restore"} the project. ${failure instanceof Error ? failure.message : "Core could not save the change."} Try again.`);
    } finally {
      setUpdating(false);
      requestAnimationFrame(() => {
        if (trigger.isConnected) trigger.focus();
        else switcherButton.current?.focus();
      });
    }
  };

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setSaving(true);
    setError(undefined);
    try {
      const created = await createEngagement({ name, clientName: clientName || undefined, workspacePath: workspacePath || undefined });
      navigate(`${projectSurface(created.id, "workbench")}?view=chat`);
      onNavigate();
      setName("");
      setClientName("");
      setWorkspacePath("");
      setCreating(false);
      setOpen(false);
    } catch (createError) {
      void logCaughtDiagnostic("interface.side_nav.caught_failure_01", "A handled interface operation failed.", createError, "side_nav");
      setError(createError instanceof Error ? createError.message : "Could not create the project.");
    } finally {
      setSaving(false);
    }
  };
  return (
    <aside className={`side-nav${variant === "zero" ? " zero-anchor-dock" : ""}`} data-shell="shared" aria-label="Primary navigation">
      <div className="brand-lockup">
        <span className="brand-mark" aria-hidden="true">
          <Orbit size={24} strokeWidth={1.8} />
        </span>
        <span>
          <strong>Nebula</strong>
          <small>Security workspace</small>
        </span>
        <span className="alpha-label">3.0</span>
      </div>

      <div className="engagement-picker">
        <button ref={switcherButton} className="engagement-switcher" type="button" title={engagementName} aria-label="Switch project" aria-expanded={open} onClick={() => setOpen((value) => !value)}>
          <span className="engagement-avatar">{initials}</span>
          <span className="engagement-copy"><small>Active project</small><strong>{engagementName}</strong></span>
          <ChevronDown size={16} aria-hidden="true" />
        </button>
        {open && <div className="engagement-menu" role="dialog" aria-label="Project switcher">
          <header><strong>Projects</strong><button className="icon-button subtle" type="button" aria-label="Close project switcher" onClick={() => setOpen(false)}><X size={14} /></button></header>
          {!creating && <div className="engagement-options">
            {(showArchived ? archivedEngagements : engagements).map((item) => <div className="project-switcher-row" key={item.id}>
              {showArchived ? <span className="project-switcher-name">{item.name}<small>Archived</small></span> : <button type="button" disabled={updating} aria-current={item.id === engagement?.id ? "true" : undefined} onClick={() => { navigate(replaceProjectInPath(location.pathname, item.id) + location.search); setOpen(false); }}><span>{item.name}<small>{item.clientName || item.status}</small></span>{item.id === engagement?.id && <Check size={14} />}</button>}
              <button className="project-switcher-action" type="button" disabled={updating || coreState !== "online"} aria-label={`${showArchived ? "Restore" : "Remove"} project ${item.name}`} title={showArchived ? "Restore project" : "Remove project"} onClick={(event) => void changeArchived(item.id, item.name, !showArchived, event.currentTarget)}>{showArchived ? <RotateCcw size={16} aria-hidden="true" /> : <Archive size={16} aria-hidden="true" />}</button>
            </div>)}
            {(showArchived ? archivedEngagements : engagements).length === 0 && <p>{showArchived ? "No archived projects." : "No active projects. Create a project or restore an archived one."}</p>}
          </div>}
          {!creating && <>
            {projectError && <p className="project-switcher-feedback" role="alert">{projectError}</p>}
            {notice && <p className="project-switcher-feedback" role="status">{notice}</p>}
            <button className="engagement-new" type="button" disabled={updating} onClick={() => setShowArchived(!showArchived)}>{showArchived ? "Active projects" : `Archived projects (${archivedEngagements.length})`}</button>
          </>}
          {creating ? <form className="engagement-create" onSubmit={(event) => void submit(event)}><label>Name<input required autoFocus value={name} onChange={(event) => setName(event.target.value)} /></label><label>Client name<input value={clientName} onChange={(event) => setClientName(event.target.value)} /></label><label>Project folder<input aria-label="Project folder" aria-describedby="project-folder-help" value={workspacePath} placeholder="Choose a folder" onChange={(event) => setWorkspacePath(event.target.value)} /><small id="project-folder-help">Optional. Grok, Codex, and Kali use this folder directly as their shared working directory.</small></label><HostFolderPicker api={api} value={workspacePath} onSelect={setWorkspacePath} />{error && <DiagnosticErrorNotice error={error} fallback="The operation could not be completed." compact />}<footer><button className="button quiet" type="button" onClick={() => setCreating(false)}>Cancel</button><button className="button primary" type="submit" disabled={saving}>{saving ? "Creating…" : "Create"}</button></footer></form> : <button className="engagement-new" type="button" disabled={updating || coreState !== "online"} onClick={() => setCreating(true)}><Plus size={14} /> New project</button>}
        </div>}
      </div>

      <nav className="nav-list">
        {navigationGroups.map((group) => (
          <section className="nav-group" aria-labelledby={`nav-group-${group.id}`} key={group.id}>
            <h2 id={`nav-group-${group.id}`}>{group.label}</h2>
            {navigationItems.filter((item) => item.group === group.id).map(({ path, label, icon: Icon }) => (
              <NavLink
                key={path}
                to={canonicalNavigationPath(path, engagement?.id)}
                end
                title={collapsed ? label : undefined}
                aria-label={label}
                onClick={onNavigate}
                className={({ isActive }) => `nav-item${isActive ? " active" : ""}`}
              >
                <Icon size={18} strokeWidth={1.8} aria-hidden="true" />
                <span>{label}</span>
              </NavLink>
            ))}
          </section>
        ))}
      </nav>

      <div className="side-nav-footer">
        {navigationItems.filter((item) => item.group === "settings").map(({ path, label, icon: Icon }) => (
          <NavLink
            key={path}
            to={path}
            title={collapsed ? label : undefined}
            aria-label={label}
            onClick={onNavigate}
            className={({ isActive }) => `nav-item settings-nav-item${isActive ? " active" : ""}`}
          >
            <Icon size={18} strokeWidth={1.8} aria-hidden="true" />
            <span>{label}</span>
          </NavLink>
        ))}
        <div className="operator-row">
          <span className="operator-avatar">{operatorInitials}</span>
          <span>
            <strong title={operatorName}>{operatorName}</strong>
            <small>{activeOperator?.role ?? activeOperator?.email ?? "Add when attribution is needed"}</small>
          </span>
          <LockKeyhole size={15} aria-label="Local attribution profile" />
        </div>
      </div>
    </aside>
  );
}
