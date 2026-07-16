import { useCallback, useEffect, useMemo, useState } from "react";
import { Outlet, useLocation, useNavigate } from "react-router-dom";
import { WorkbenchDraftProvider } from "../state/WorkbenchDraftContext";
import { WorkbenchEditorProvider } from "../state/WorkbenchEditorContext";
import { ReleaseUpdateProvider } from "../state/ReleaseUpdateContext";
import { useTheme } from "../state/ThemeContext";
import { useWorkspace } from "../state/WorkspaceContext";
import { ChromeProvider } from "../state/ChromeContext";
import { ActivityCenter, type ActivityCenterView } from "./ActivityCenter";
import { CommandPalette } from "./CommandPalette";
import { SideNav } from "./SideNav";
import { TopBar } from "./TopBar";
import { UpdateBanner } from "./UpdateBanner";
import { DiagnosticErrorNotice, DiagnosticsAvailabilityBanner, logDiagnostic } from "../diagnostics";

export function AppShell() {
  const navigate = useNavigate();
  const location = useLocation();
  const { resolvedTheme } = useTheme();
  const {
    approvals,
    coreError,
    reconnect,
    workspaceState,
  } = useWorkspace();
  const zero = resolvedTheme === "zero";
  const [activityOpen, setActivityOpen] = useState(false);
  const [activityView, setActivityView] = useState<ActivityCenterView>("activity");
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
    void import("@tauri-apps/api/event")
      .then(async ({ listen }) => {
        const stop = await listen<string>("nebula-menu-command", (event) => runCommand(event.payload));
        if (disposed) stop();
        else unlisten = stop;
      })
      .catch((error: unknown) => logDiagnostic({
        level: "error",
        eventCode: "interface.menu.listener_failed",
        message: "The interface could not listen for native menu actions.",
        outcome: "failure",
        stage: "menu-listener",
        retryable: true,
        exception: error,
      }));
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
      <WorkbenchEditorProvider>
        <WorkbenchDraftProvider>
          <ChromeProvider value={chrome}>
            <div className={`app-shell${zero ? " zero-layer-shell" : ""}${activityOpen ? " with-activity" : ""}${sidebarCollapsed ? " sidebar-collapsed" : ""}`}>
              <a className="skip-link" href="#main-content">Skip to main content</a>
              <SideNav collapsed={sidebarCollapsed} onNavigate={closeMobileSidebar} variant={zero ? "zero" : "standard"} />
              <button className="sidebar-scrim" type="button" aria-label="Close sidebar" onClick={toggleSidebar} />
              <TopBar
                activityOpen={activityOpen}
                approvalsCount={approvals.length}
                onToggleActivity={toggleActivity}
                onToggleSidebar={toggleSidebar}
                onOpenPalette={openPalette}
                setToolbarHost={setToolbarHost}
                sidebarCollapsed={sidebarCollapsed}
                variant={zero ? "zero" : "standard"}
              />
              <main id="main-content" className="main-content" tabIndex={-1}>
                {workspaceState !== "failed" && <DiagnosticsAvailabilityBanner />}
                {zero && <span className="zero-route-flare" aria-hidden="true" key={`${location.pathname}${location.search}`} />}
                {workspaceState === "starting" && <div className="workspace-state-banner starting" role="status">Starting Nebula…</div>}
                {workspaceState === "degraded" && <div className="workspace-state-banner degraded" role="status"><span><strong>Nebula is ready with limited features.</strong>{coreError && <small>{coreError}</small>}</span><button className="button quiet" type="button" onClick={reconnect}>Retry</button></div>}
                {workspaceState === "failed" && <div className="workspace-state-banner failed"><DiagnosticErrorNotice error={coreError ?? "Check the local service and try again."} title="Nebula Core could not start." fallback="Check the local service and try again." compact /><button className="button primary" type="button" onClick={reconnect}>Try again</button></div>}
                <UpdateBanner />
                <Outlet />
              </main>
              <ActivityCenter open={activityOpen} onClose={() => setActivityOpen(false)} view={activityView} onViewChange={setActivityView} />
              <CommandPalette
                open={paletteOpen}
                onClose={() => setPaletteOpen(false)}
                onToggleActivity={toggleActivity}
                onToggleSidebar={toggleSidebar}
              />
            </div>
          </ChromeProvider>
        </WorkbenchDraftProvider>
      </WorkbenchEditorProvider>
    </ReleaseUpdateProvider>
  );
}
