import { useState, type FormEvent } from "react";
import { Check, ChevronDown, LockKeyhole, Orbit, Plus, ShieldCheck, X } from "lucide-react";
import { NavLink } from "react-router-dom";
import { navigationGroups, navigationItems } from "../navigation";
import { useWorkspace } from "../state/WorkspaceContext";

interface SideNavProps {
  collapsed: boolean;
  onNavigate: () => void;
}

export function SideNav({ collapsed, onNavigate }: SideNavProps) {
  const {
    coreState,
    createEngagement,
    activeOperator,
    engagement,
    engagements,
    selectEngagement,
  } = useWorkspace();
  const [open, setOpen] = useState(false);
  const [creating, setCreating] = useState(false);
  const [name, setName] = useState("");
  const [clientName, setClientName] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string>();
  const engagementName = engagement?.name ?? "No project available";
  const initials = engagementName
    .split(/\s+/)
    .filter(Boolean)
    .slice(0, 2)
    .map((part) => part[0]?.toUpperCase())
    .join("") || "NE";
  const operatorName = activeOperator?.displayName ?? "No operator profile";
  const operatorInitials = operatorName.split(/\s+/).filter(Boolean).slice(0, 2).map((part) => part[0]?.toUpperCase()).join("") || "OP";

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setSaving(true);
    setError(undefined);
    try {
      await createEngagement({ name, clientName: clientName || undefined });
      setName("");
      setClientName("");
      setCreating(false);
      setOpen(false);
    } catch (createError) {
      setError(createError instanceof Error ? createError.message : "Could not create the project.");
    } finally {
      setSaving(false);
    }
  };
  return (
    <aside className="side-nav" aria-label="Primary navigation">
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
        <button className="engagement-switcher" type="button" title={engagementName} aria-label="Switch project" aria-expanded={open} onClick={() => setOpen((value) => !value)}>
          <span className="engagement-avatar">{initials}</span>
          <span className="engagement-copy"><small>Active project</small><strong>{engagementName}</strong></span>
          <ChevronDown size={16} aria-hidden="true" />
        </button>
        {open && <div className="engagement-menu" role="dialog" aria-label="Project switcher">
          <header><strong>Projects</strong><button className="icon-button subtle" type="button" aria-label="Close project switcher" onClick={() => setOpen(false)}><X size={14} /></button></header>
          {!creating && <div className="engagement-options">
            {engagements.map((item) => <button type="button" key={item.id} aria-current={item.id === engagement?.id ? "true" : undefined} onClick={() => { selectEngagement(item.id); setOpen(false); }}><span>{item.name}<small>{item.clientName || item.status}</small></span>{item.id === engagement?.id && <Check size={14} />}</button>)}
            {engagements.length === 0 && <p>No projects yet.</p>}
          </div>}
          {creating ? <form className="engagement-create" onSubmit={(event) => void submit(event)}><label>Name<input required autoFocus value={name} onChange={(event) => setName(event.target.value)} /></label><label>Client name<input value={clientName} onChange={(event) => setClientName(event.target.value)} /></label>{error && <p className="form-error" role="alert">{error}</p>}<footer><button className="button quiet" type="button" onClick={() => setCreating(false)}>Cancel</button><button className="button primary" type="submit" disabled={saving}>{saving ? "Creating…" : "Create"}</button></footer></form> : <button className="engagement-new" type="button" disabled={coreState !== "online"} onClick={() => setCreating(true)}><Plus size={14} /> New project</button>}
        </div>}
      </div>

      <nav className="nav-list">
        {navigationGroups.map((group) => (
          <section className="nav-group" aria-labelledby={`nav-group-${group.id}`} key={group.id}>
            <h2 id={`nav-group-${group.id}`}>{group.label}</h2>
            {navigationItems.filter((item) => item.group === group.id).map(({ path, label, icon: Icon }) => (
              <NavLink
                key={path}
                to={path}
                end={path === "/"}
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
        <div className="local-first-note">
          <ShieldCheck size={17} aria-hidden="true" />
          <span>
            <strong>Local-first workspace</strong>
            <small>Cloud knowledge requires confirmation</small>
          </span>
        </div>
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
