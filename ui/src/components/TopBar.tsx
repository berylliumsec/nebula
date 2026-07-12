import {
  Bell,
  Command,
  PanelRightClose,
  PanelRightOpen,
  RefreshCw,
  Wifi,
  WifiOff,
} from "lucide-react";
import { useLocation } from "react-router-dom";
import { navigationItems } from "../navigation";
import { useWorkspace } from "../state/WorkspaceContext";

interface TopBarProps {
  activityOpen: boolean;
  onToggleActivity: () => void;
  onOpenPalette: () => void;
}

export function TopBar({ activityOpen, onToggleActivity, onOpenPalette }: TopBarProps) {
  const location = useLocation();
  const page = navigationItems.find((item) => item.path === location.pathname) ?? navigationItems[0];
  const { coreState, previewMode, reconnect } = useWorkspace();

  return (
    <header className="top-bar">
      <div className="top-bar-title">
        <span>Engagement</span>
        <span aria-hidden="true">/</span>
        <strong>{page.label}</strong>
      </div>
      <div className="top-bar-actions">
        {previewMode && <span className="preview-pill">Interface preview</span>}
        <button
          className={`connection-chip ${coreState}`}
          type="button"
          onClick={coreState === "offline" ? reconnect : undefined}
          aria-label={coreState === "offline" ? "Nebula Core offline. Retry connection" : `Nebula Core ${coreState}`}
        >
          {coreState === "checking" ? (
            <RefreshCw className="spin" size={14} aria-hidden="true" />
          ) : coreState === "online" ? (
            <Wifi size={14} aria-hidden="true" />
          ) : (
            <WifiOff size={14} aria-hidden="true" />
          )}
          Core {coreState}
        </button>
        <button className="command-trigger" type="button" onClick={onOpenPalette}>
          <Command size={15} aria-hidden="true" />
          <span>Search</span>
          <kbd>⌘K</kbd>
        </button>
        <button className="icon-button" type="button" aria-label="Notifications">
          <Bell size={18} aria-hidden="true" />
          <span className="notification-dot" />
        </button>
        <button
          className="icon-button"
          type="button"
          onClick={onToggleActivity}
          aria-expanded={activityOpen}
          aria-controls="activity-center"
          aria-label={activityOpen ? "Hide activity center" : "Show activity center"}
        >
          {activityOpen ? (
            <PanelRightClose size={18} aria-hidden="true" />
          ) : (
            <PanelRightOpen size={18} aria-hidden="true" />
          )}
        </button>
      </div>
    </header>
  );
}
