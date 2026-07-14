import { useCallback, useEffect, useMemo, useState } from "react";
import { Outlet, useNavigate } from "react-router-dom";
import { WorkbenchDraftProvider } from "../state/WorkbenchDraftContext";
import { ReleaseUpdateProvider } from "../state/ReleaseUpdateContext";
import { useWorkspace } from "../state/WorkspaceContext";
import { ChromeProvider } from "../state/ChromeContext";
import { ActivityCenter } from "./ActivityCenter";
import { CommandPalette } from "./CommandPalette";
import { SideNav } from "./SideNav";
import { TopBar } from "./TopBar";
import { UpdateBanner } from "./UpdateBanner";

export function AppShell() {
  const navigate = useNavigate();
  const { approvals, coreError, reconnect, workspaceState } = useWorkspace();
  const [activityOpen, setActivityOpen] = useState(false);
  const [paletteOpen, setPaletteOpen] = useState(false);
  const [sidebarCollapsed, setSidebarCollapsed] = useState(() => {
    const stored = localStorage.getItem("nebula.sidebar.collapsed");
    return stored === null ? window.matchMedia("(max-width: 760px)").matches : stored === "true";
  });
  const [toolbarHost, setToolbarHost] = useState<HTMLElement | null>(null);
  const toggleActivity = useCallback(() => setActivityOpen((value) => !value), []);
  const toggleSidebar = useCallback(() => setSidebarCollapsed((value) => {
    localStorage.setItem("nebula.sidebar.collapsed", String(!value));
    return !value;
  }), []);
  const openPalette = useCallback(() => setPaletteOpen(true), []);
  const closeMobileSidebar = useCallback(() => {
    if (!sidebarCollapsed && window.matchMedia("(max-width: 760px)").matches) toggleSidebar();
  }, [sidebarCollapsed, toggleSidebar]);
  const runContextualNew = useCallback(() => {
    document.querySelector<HTMLButtonElement>(".top-bar-page-actions .button.primary:not(:disabled)")?.click();
  }, []);

  const runCommand = useCallback((command: string) => {
    if (command === "command-center") openPalette();
    if (command === "toggle-sidebar") toggleSidebar();
    if (command === "toggle-inspector") toggleActivity();
    if (command === "settings") navigate("/settings");
    if (command === "home") navigate("/");
    if (command === "new-contextual") runContextualNew();
  }, [navigate, openPalette, runContextualNew, toggleActivity, toggleSidebar]);

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      const modifier = event.metaKey || event.ctrlKey;
      if (modifier && event.key.toLowerCase() === "k") {
        event.preventDefault();
        setPaletteOpen((value) => !value);
      }
      if (modifier && event.key === ",") {
        event.preventDefault();
        navigate("/settings");
      }
      if (modifier && !event.altKey && event.key === "1") {
        event.preventDefault();
        navigate("/");
      }
      if (modifier && !event.altKey && event.key.toLowerCase() === "n") {
        event.preventDefault();
        runContextualNew();
      }
      if (modifier && event.altKey && event.key.toLowerCase() === "s") {
        event.preventDefault();
        toggleSidebar();
      }
      if (modifier && event.altKey && event.key.toLowerCase() === "i") {
        event.preventDefault();
        toggleActivity();
      }
      if (event.key === "Escape" && paletteOpen) setPaletteOpen(false);
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [navigate, paletteOpen, runContextualNew, toggleActivity, toggleSidebar]);

  useEffect(() => {
    if (!("__TAURI_INTERNALS__" in window)) return;
    let disposed = false;
    let unlisten: (() => void) | undefined;
    void import("@tauri-apps/api/event").then(async ({ listen }) => {
      const stop = await listen<string>("nebula-menu-command", (event) => runCommand(event.payload));
      if (disposed) stop();
      else unlisten = stop;
    });
    return () => {
      disposed = true;
      unlisten?.();
    };
  }, [runCommand]);

  const chrome = useMemo(() => ({
    activityOpen,
    paletteOpen,
    sidebarCollapsed,
    toolbarHost,
    openPalette,
    setActivityOpen,
    setPaletteOpen,
    setToolbarHost,
    toggleActivity,
    toggleSidebar,
  }), [activityOpen, openPalette, paletteOpen, sidebarCollapsed, toggleActivity, toggleSidebar, toolbarHost]);

  return (
    <ReleaseUpdateProvider>
      <WorkbenchDraftProvider>
        <ChromeProvider value={chrome}>
          <div className={`app-shell${activityOpen ? " with-activity" : ""}${sidebarCollapsed ? " sidebar-collapsed" : ""}`}>
          <a className="skip-link" href="#main-content">Skip to main content</a>
          <SideNav collapsed={sidebarCollapsed} onNavigate={closeMobileSidebar} />
          <button className="sidebar-scrim" type="button" aria-label="Close sidebar" onClick={toggleSidebar} />
          <TopBar
            activityOpen={activityOpen}
            approvalsCount={approvals.length}
            onToggleActivity={toggleActivity}
            onToggleSidebar={toggleSidebar}
            onOpenPalette={openPalette}
            setToolbarHost={setToolbarHost}
            sidebarCollapsed={sidebarCollapsed}
          />
          <main id="main-content" className="main-content" tabIndex={-1}>
            {workspaceState === "starting" && <div className="workspace-state-banner starting" role="status">Starting Nebula…</div>}
            {workspaceState === "degraded" && <div className="workspace-state-banner degraded" role="status"><span><strong>Nebula is ready with limited features.</strong>{coreError && <small>{coreError}</small>}</span><button className="button quiet" type="button" onClick={reconnect}>Retry</button></div>}
            {workspaceState === "failed" && <div className="workspace-state-banner failed" role="alert"><span><strong>Nebula Core could not start.</strong><small>{coreError ?? "Check the local service and try again."}</small></span><button className="button primary" type="button" onClick={reconnect}>Try again</button></div>}
            <UpdateBanner />
            <Outlet />
          </main>
          <ActivityCenter open={activityOpen} onClose={() => setActivityOpen(false)} />
          <CommandPalette
            open={paletteOpen}
            onClose={() => setPaletteOpen(false)}
            onToggleActivity={toggleActivity}
            onToggleSidebar={toggleSidebar}
          />
          </div>
        </ChromeProvider>
      </WorkbenchDraftProvider>
    </ReleaseUpdateProvider>
  );
}
