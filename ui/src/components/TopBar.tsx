import {
  Command,
  PanelLeftClose,
  PanelLeftOpen,
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
  variant?: "standard" | "zero";
}

export function TopBar({
  activityOpen,
  approvalsCount,
  onToggleActivity,
  onToggleSidebar,
  onOpenPalette,
  setToolbarHost,
  sidebarCollapsed,
  variant = "standard",
}: TopBarProps) {
  const location = useLocation();
  const page = navigationItems.find((item) => item.path === location.pathname) ?? navigationItems[0];
  const { coreError, reconnect, workspaceState } = useWorkspace();
  const canRetry = workspaceState === "failed" || workspaceState === "degraded";

  return (
    <header className={`top-bar${variant === "zero" ? " zero-status-band" : ""}`} data-variant={variant}>
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
          <span>Project</span>
          <span aria-hidden="true">/</span>
          <strong>{page.label}</strong>
        </div>
      </div>
      <div className="top-bar-page-actions" ref={setToolbarHost} role="group" aria-label={`${page.label} actions`} />
      <div className="top-bar-actions">
        <button
          className={`connection-chip ${workspaceState}`}
          type="button"
          onClick={canRetry ? reconnect : undefined}
          disabled={!canRetry}
          title={coreError}
          aria-label={canRetry ? `Nebula Core ${workspaceState}. Retry connection` : `Nebula Core ${workspaceState}`}
        >
          {workspaceState === "starting" ? (
            <RefreshCw className="spin" size={14} aria-hidden="true" />
          ) : workspaceState === "ready" ? (
            <Wifi size={14} aria-hidden="true" />
          ) : (
            <WifiOff size={14} aria-hidden="true" />
          )}
          <span>{workspaceState === "degraded" ? "Limited" : workspaceState}</span>
        </button>
        <button className="command-trigger" type="button" onClick={onOpenPalette} aria-label="Search commands">
          <Command size={15} aria-hidden="true" />
          <span>Search</span>
          <kbd>⌘K</kbd>
        </button>
        {approvalsCount > 0 && <button
          className={`icon-button approval-trigger${approvalsCount > 0 ? " has-approvals" : ""}`}
          type="button"
          onClick={onToggleActivity}
          aria-expanded={activityOpen}
          aria-controls="activity-center"
          aria-label={`${activityOpen ? "Hide" : "Show"} activity inspector${approvalsCount ? `, ${approvalsCount} pending approval${approvalsCount === 1 ? "" : "s"}` : ""}`}
          title={`${activityOpen ? "Hide" : "Show"} activity inspector (⌥⌘I)`}
        >
          <ShieldAlert size={18} aria-hidden="true" />
          {approvalsCount > 0 && <span className="notification-count" aria-hidden="true">{approvalsCount}</span>}
        </button>}
      </div>
    </header>
  );
}
