import { useCallback, useEffect, useMemo, useState } from "react";
import { Outlet, useLocation, useNavigate } from "react-router-dom";
import { WorkbenchDraftProvider } from "../state/WorkbenchDraftContext";
import { WorkbenchEditorProvider } from "../state/WorkbenchEditorContext";
import { ReleaseUpdateProvider } from "../state/ReleaseUpdateContext";
import { useTheme } from "../state/ThemeContext";
import { useWorkspace } from "../state/WorkspaceContext";
import { ChromeProvider, type ContextualCommand } from "../state/ChromeContext";
import { ActivityCenter, type ActivityCenterView } from "./ActivityCenter";
import { CommandPalette } from "./CommandPalette";
import { SideNav } from "./SideNav";
import { TopBar } from "./TopBar";
import { UpdateBanner } from "./UpdateBanner";
import { DiagnosticErrorNotice, DiagnosticsAvailabilityBanner, logDiagnostic } from "../diagnostics";
import { browserAuthorizationRecovery } from "../api/runtime";
import { BrowserAutomationWorker } from "./BrowserAutomationWorker";
import { SettingsLens } from "./SettingsLens";
import { HandoffRecoveryNotice } from "./HandoffRecoveryNotice";
import type { SettingCatalogEntry } from "../settingsCatalog";
import { projectSurface } from "../resourceRoutes";

const resourceLabels: Record<string, string> = {
  projects: "Projects", providers: "Model providers", providerCatalog: "Provider setup",
  operators: "Operator profiles", setup: "Terminal setup", activity: "Activity",
  approvals: "Approvals", assets: "Assets", findings: "Findings", evidence: "Evidence",
  notes: "Notes", sources: "Knowledge sources", reports: "Reports",
};

export function AppShell() {
  const navigate = useNavigate();
  const location = useLocation();
  const { resolvedTheme } = useTheme();
  const {
    approvals,
    api,
    coreError,
    engagement,
    reconnect,
    resourceStatus,
    runtime,
    retryResource,
    setupStatus,
    workspaceState,
  } = useWorkspace();
  const zero = resolvedTheme === "zero-dark" || resolvedTheme === "zero-light";
  const [activityOpen, setActivityOpen] = useState(false);
  const [activityView, setActivityView] = useState<ActivityCenterView>("activity");
  const [paletteOpen, setPaletteOpen] = useState(false);
  const [settingLens, setSettingLens] = useState<{ entry: SettingCatalogEntry; returnFocus: HTMLElement | null }>();
  const [sidebarCollapsed, setSidebarCollapsed] = useState(() => {
    const stored = localStorage.getItem("nebula.sidebar.collapsed");
    return stored === null ? window.matchMedia("(max-width: 760px)").matches : stored === "true";
  });
  const [toolbarHost, setToolbarHost] = useState<HTMLElement | null>(null);
  const [contextualCommands, setContextualCommands] = useState<ContextualCommand[]>([]);
  const authorizationRecovery = runtime?.reason === "browser_session_token_missing"
    ? browserAuthorizationRecovery(window.location.hostname)
    : undefined;
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
    if (command === "home") navigate(engagement ? projectSurface(engagement.id, "workbench") : "/");
    if (command === "new-contextual") runContextualNew();
  }, [engagement, navigate, openPalette, runContextualNew, toggleActivity, toggleSidebar]);

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
        navigate(engagement ? projectSurface(engagement.id, "workbench") : "/");
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
  }, [engagement, navigate, paletteOpen, runContextualNew, toggleActivity, toggleSidebar]);

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
    contextualCommands,
    paletteOpen,
    settingLensOpen: Boolean(settingLens),
    sidebarCollapsed,
    toolbarHost,
    openPalette,
    setActivityOpen,
    setContextualCommands,
    setPaletteOpen,
    setToolbarHost,
    toggleActivity,
    toggleSidebar,
  }), [activityOpen, contextualCommands, openPalette, paletteOpen, settingLens, sidebarCollapsed, toggleActivity, toggleSidebar, toolbarHost]);
  return (
    <ReleaseUpdateProvider>
      <WorkbenchEditorProvider>
        <WorkbenchDraftProvider>
          <ChromeProvider value={chrome}>
            <BrowserAutomationWorker />
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
                {workspaceState === "starting" && <div className="workspace-state-banner starting" role="status"><span><strong>Starting Nebula…</strong><small>{runtime?.mode === "desktop_remote" ? "Connecting this UI shell to the selected remote Core." : "Connecting to the local Core service."}</small></span></div>}
                {workspaceState === "bootstrapping" && <div className="workspace-state-banner starting" role="status"><span><strong>Preparing your workspace…</strong><small>{setupStatus?.stageDetail ?? "Loading Projects and checking Terminal setup."}</small></span></div>}
                {workspaceState === "degraded" && <div className="workspace-state-banner degraded" role="status"><span><strong>Nebula is ready with limited features.</strong>{coreError && <small>{coreError}</small>}</span>{coreError && <button className="button quiet" type="button" onClick={reconnect}>Retry Core</button>}</div>}
                {workspaceState !== "failed" && Object.entries(resourceStatus).some(([, status]) => status.state === "failed") && <section className="workspace-resource-failures" aria-label="Workspace features needing attention">
                  {Object.entries(resourceStatus).filter(([, status]) => status.state === "failed").map(([resource, status]) => <div className="workspace-state-banner degraded" key={resource}>
                    <DiagnosticErrorNotice error={status.error} title={`${resourceLabels[resource] ?? resource} could not be loaded.`} fallback="This feature is temporarily unavailable; other loaded features remain usable." compact />
                    <button className="button quiet" type="button" disabled={status.state === "loading"} onClick={() => void retryResource(resource as Parameters<typeof retryResource>[0]).catch((error: unknown) => logDiagnostic({ level: "error", eventCode: "interface.workspace.resource_retry_failed", message: "A workspace resource retry failed.", outcome: "failure", stage: "resource-retry", retryable: true, exception: error }))}>Retry {resourceLabels[resource] ?? resource}</button>
                  </div>)}
                </section>}
                {workspaceState === "failed" && authorizationRecovery ? <div className="workspace-state-banner failed expired-session-state" role="alert"><span>{authorizationRecovery === "pair" ? <><strong>Pair this browser to continue</strong><small>On an authorized browser on the Nebula host, open Settings → Advanced → Identity &amp; Security → Paired devices, choose Pair phone, then scan the QR code. If this device was previously paired, pair it again because its access expired or was revoked.</small></> : <><strong>Browser session expired</strong><small>Nebula keeps the Core token in memory only, so reloading this page intentionally clears access. Close this tab and relaunch the interface with <code>nebula-core ui</code>.</small></>}</span></div> : workspaceState === "failed" ? <div className="workspace-state-banner failed"><DiagnosticErrorNotice error={coreError ?? "Check the local service and try again."} title="Nebula Core could not start." fallback="Check the local service and try again." compact /><button className="button primary" type="button" onClick={reconnect}>Try again</button></div> : null}
                <UpdateBanner />
                <HandoffRecoveryNotice />
                <Outlet />
              </main>
              <ActivityCenter open={activityOpen} onClose={() => setActivityOpen(false)} view={activityView} onViewChange={setActivityView} />
              <CommandPalette
                api={api}
                activeProjectId={engagement?.id}
                contextualCommands={contextualCommands}
                open={paletteOpen}
                onClose={() => setPaletteOpen(false)}
                onToggleActivity={toggleActivity}
                onToggleSidebar={toggleSidebar}
                onOpenSetting={(entry, returnFocus) => setSettingLens({ entry, returnFocus })}
              />
              {settingLens && <SettingsLens entry={settingLens.entry} key={settingLens.entry.id} returnFocus={settingLens.returnFocus} onClose={() => setSettingLens(undefined)} />}
            </div>
          </ChromeProvider>
        </WorkbenchDraftProvider>
      </WorkbenchEditorProvider>
    </ReleaseUpdateProvider>
  );
}
