import { ChevronDown, LockKeyhole, Orbit, ShieldCheck } from "lucide-react";
import { NavLink } from "react-router-dom";
import { navigationItems } from "../navigation";
import { useWorkspace } from "../state/WorkspaceContext";

export function SideNav() {
  const { engagement, previewMode } = useWorkspace();
  const engagementName = engagement?.name ?? (previewMode ? "Acme external assessment" : "No engagement selected");
  const initials = engagementName
    .split(/\s+/)
    .filter(Boolean)
    .slice(0, 2)
    .map((part) => part[0]?.toUpperCase())
    .join("") || "NE";
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
        <span className="alpha-label">3 alpha</span>
      </div>

      <button className="engagement-switcher" type="button" aria-label="Switch engagement">
        <span className="engagement-avatar">{initials}</span>
        <span className="engagement-copy">
          <small>Active engagement</small>
          <strong>{engagementName}</strong>
        </span>
        <ChevronDown size={16} aria-hidden="true" />
      </button>

      <nav className="nav-list">
        {navigationItems.map(({ path, label, icon: Icon }) => (
          <NavLink
            key={path}
            to={path}
            end={path === "/"}
            className={({ isActive }) => `nav-item${isActive ? " active" : ""}`}
          >
            <Icon size={18} strokeWidth={1.8} aria-hidden="true" />
            <span>{label}</span>
          </NavLink>
        ))}
      </nav>

      <div className="side-nav-footer">
        <div className="local-first-note">
          <ShieldCheck size={17} aria-hidden="true" />
          <span>
            <strong>Local-first workspace</strong>
            <small>Cloud transfer requires policy approval</small>
          </span>
        </div>
        <div className="operator-row">
          <span className="operator-avatar">JD</span>
          <span>
            <strong>Jordan Diaz</strong>
            <small>Engagement lead</small>
          </span>
          <LockKeyhole size={15} aria-label="Authenticated session" />
        </div>
      </div>
    </aside>
  );
}
