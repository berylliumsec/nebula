import {
  Command,
  PanelLeftClose,
  PanelLeftOpen,
  PanelRightClose,
  PanelRightOpen,
  RefreshCw,
  ShieldAlert,
  Wifi,
  WifiOff,
} from "lucide-react";
import { useLocation } from "react-router-dom";
import { navigationItems } from "../navigation";
import { useWorkspace } from "../state/WorkspaceContext";

interface TopBarProps {
  activityOpen: boolean;
  approvalsCount: number;
  onToggleActivity: () => void;
  onToggleSidebar: () => void;
  onOpenPalette: () => void;
  setToolbarHost: (element: HTMLDivElement | null) => void;
  sidebarCollapsed: boolean;
}

export function TopBar({
  activityOpen,
  approvalsCount,
  onToggleActivity,
  onToggleSidebar,
  onOpenPalette,
  setToolbarHost,
  sidebarCollapsed,
}: TopBarProps) {
  const location = useLocation();
  const page = navigationItems.find((item) => item.path === location.pathname) ?? navigationItems[0];
  const { coreError, coreState, previewMode, reconnect } = useWorkspace();

  return (
    <header className="top-bar">
      <div className="top-bar-leading">
        <button
          className="icon-button toolbar-button"
          type="button"
          onClick={onToggleSidebar}
          aria-label={sidebarCollapsed ? "Show sidebar" : "Hide sidebar"}
          aria-expanded={!sidebarCollapsed}
          title={`${sidebarCollapsed ? "Show" : "Hide"} sidebar (⌥⌘S)`}
        >
          {sidebarCollapsed ? <PanelLeftOpen size={18} /> : <PanelLeftClose size={18} />}
        </button>
        <div className="top-bar-title">
          <span>Engagement</span>
          <span aria-hidden="true">/</span>
          <strong>{page.label}</strong>
        </div>
      </div>
      <div className="top-bar-page-actions" ref={setToolbarHost} aria-label={`${page.label} actions`} />
      <div className="top-bar-actions">
        {previewMode && <span className="preview-pill">Interface preview</span>}
        <button
          className={`connection-chip ${coreState}`}
          type="button"
          onClick={coreState === "offline" ? reconnect : undefined}
          disabled={coreState !== "offline"}
          title={coreError}
          aria-label={coreState === "offline" ? "Nebula Core offline. Retry connection" : `Nebula Core ${coreState}`}
        >
          {coreState === "checking" ? (
            <RefreshCw className="spin" size={14} aria-hidden="true" />
          ) : coreState === "online" ? (
            <Wifi size={14} aria-hidden="true" />
          ) : (
            <WifiOff size={14} aria-hidden="true" />
          )}
          <span>Core {coreState}</span>
        </button>
        <button className="command-trigger" type="button" onClick={onOpenPalette} aria-label="Search commands">
          <Command size={15} aria-hidden="true" />
          <span>Search</span>
          <kbd>⌘K</kbd>
        </button>
        <button
          className={`icon-button approval-trigger${approvalsCount > 0 ? " has-approvals" : ""}`}
          type="button"
          onClick={onToggleActivity}
          aria-expanded={activityOpen}
          aria-controls="activity-center"
          aria-label={`${activityOpen ? "Hide" : "Show"} activity inspector${approvalsCount ? `, ${approvalsCount} pending approval${approvalsCount === 1 ? "" : "s"}` : ""}`}
          title={`${activityOpen ? "Hide" : "Show"} activity inspector (⌥⌘I)`}
        >
          {approvalsCount > 0 ? (
            <ShieldAlert size={18} aria-hidden="true" />
          ) : activityOpen ? (
            <PanelRightClose size={18} aria-hidden="true" />
          ) : (
            <PanelRightOpen size={18} aria-hidden="true" />
          )}
          {approvalsCount > 0 && <span className="notification-count" aria-hidden="true">{approvalsCount}</span>}
        </button>
      </div>
    </header>
  );
}
