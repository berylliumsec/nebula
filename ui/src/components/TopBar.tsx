import {
  Check,
  Command,
  Copy,
  PanelLeftClose,
  PanelLeftOpen,
  RefreshCw,
  ShieldAlert,
  Wifi,
  WifiOff,
  X,
} from "lucide-react";
import { useEffect, useState } from "react";
import { useLocation } from "react-router-dom";
import { logCaughtDiagnostic } from "../diagnostics";
import { navigationItems } from "../navigation";
import { useWorkspace } from "../state/WorkspaceContext";
import type { ContainerTerminalPublicIpStatus } from "../api/types";
import { copySelectionText } from "./selection";
import { createPortal } from "react-dom";
import { ModalSurface } from "./DialogSystem";

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
  const { api, coreError, engagement, reconnect, workspaceState } = useWorkspace();
  const [publicIp, setPublicIp] = useState<ContainerTerminalPublicIpStatus>();
  const [publicIpCopied, setPublicIpCopied] = useState(false);
  const [copyError, setCopyError] = useState("");
  const [addressOpen, setAddressOpen] = useState(false);
  const canRetry = workspaceState === "failed" || workspaceState === "degraded";

  useEffect(() => { setAddressOpen(false); setCopyError(""); setPublicIpCopied(false); }, [engagement?.id]);

  useEffect(() => {
    if (!api || typeof api.engagementContainerTerminalPublicIp !== "function" || !engagement || !["ready", "degraded"].includes(workspaceState)) {
      setPublicIp(undefined);
      return;
    }
    const controller = new AbortController();
    let timer: number | undefined;
    const refresh = async () => {
      try {
        const status = await api.engagementContainerTerminalPublicIp(engagement.id, controller.signal);
        if (!controller.signal.aborted) setPublicIp(status);
      } catch (caught) {
        if (!controller.signal.aborted) {
          setPublicIp(undefined);
          void logCaughtDiagnostic(
            "interface.container_terminal.public_ip_load_failed",
            "The terminal container public IP could not be refreshed.",
            caught,
            "container_terminal",
          );
        }
      } finally {
        if (!controller.signal.aborted) timer = globalThis.setTimeout(refresh, 15_000);
      }
    };
    void refresh();
    return () => {
      controller.abort();
      if (timer !== undefined) globalThis.clearTimeout(timer);
    };
  }, [api, engagement, workspaceState]);

  const copyPublicIp = async () => {
    if (!publicIp) return;
    try {
      await copySelectionText(publicIp.address);
      setCopyError("");
      setPublicIpCopied(true);
      globalThis.setTimeout(() => setPublicIpCopied(false), 1_500);
    } catch (error) {
      // diagnostic-expected: copy denial is surfaced with manual recovery guidance.
      setCopyError(error instanceof Error ? error.message : "Select the address and copy it manually.");
    }
  };

  return (
    <><header className={`top-bar${variant === "zero" ? " zero-status-band" : ""}`} data-shell="shared">
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
        <button className={`top-bar-public-ip${publicIp?.stale ? " stale" : ""}`} type="button" disabled={!publicIp} onClick={() => { setCopyError(""); setPublicIpCopied(false); setAddressOpen(true); }} aria-haspopup="dialog" title={publicIp ? `Terminal container public IP ${publicIp.address} · observed ${new Date(publicIp.observedAt).toLocaleString()}` : "Start a terminal to observe its container public IP"} aria-label={publicIp ? `Terminal container public IP ${publicIp.address}. Show details` : "Terminal container public IP unavailable"}>
          <span>IP</span><code>{publicIp?.address ?? "—"}</code>{publicIp && (publicIpCopied ? <Check size={13} aria-hidden="true" /> : <Copy size={13} aria-hidden="true" />)}
        </button>
        <button className="command-trigger" type="button" onClick={onOpenPalette} aria-label="Search pages, actions, and settings">
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
    {addressOpen && createPortal(<ModalSurface labelledBy="terminal-address-title" onClose={() => setAddressOpen(false)}>
      <div className="dialog-heading"><h2 id="terminal-address-title">Terminal network address</h2><button className="icon-button subtle" type="button" aria-label="Close network address" onClick={() => setAddressOpen(false)}><X size={18} aria-hidden="true" /></button></div>
      <p>The observed address belongs to the terminal container, not this browser device.</p>
      {publicIp ? <><label>Public IP address<input readOnly value={publicIp.address} onFocus={event => event.currentTarget.select()} /></label><p>{publicIp.stale ? "Last known address" : "Observed"} · {new Date(publicIp.observedAt).toLocaleString()}</p><button className="button secondary" type="button" onClick={() => void copyPublicIp()}><Copy size={16} aria-hidden="true" /> Copy address</button></> : <p role="status">The address is no longer available. Saved work is unchanged.</p>}
      {publicIpCopied && <p role="status">Address copied.</p>}
      {copyError && <p role="alert">{copyError}. Select the address above and copy it manually.</p>}
    </ModalSurface>, document.body)}
    </>
  );
}
