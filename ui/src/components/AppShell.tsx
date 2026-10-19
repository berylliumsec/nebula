import { useCallback, useEffect, useMemo, useRef, useState } from "react";
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
import { browserAuthorizationRecovery, isElectronRuntime } from "../api/runtime";
import { BrowserAutomationWorker } from "./BrowserAutomationWorker";
import { SettingsLens } from "./SettingsLens";
import { NebulaControlAssistant } from "./NebulaControlAssistant";
import { HandoffRecoveryNotice } from "./HandoffRecoveryNotice";
import type { SettingCatalogEntry } from "../settingsCatalog";
import { projectSurface } from "../resourceRoutes";
import { useNavigationChords } from "../state/useNavigationChords";
import { GuideProvider } from "../guides/GuideProvider";
import { useGuideAction } from "../guides/guideActions";
import { isNebulaShell } from "../hooks/useCompactLayout";

const resourceLabels: Record<string, string> = {
  projects: "Projects", providers: "Model providers", harnesses: "Harnesses", library: "Library", providerCatalog: "Provider setup",
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
  const zero = resolvedTheme.startsWith("zero-");
  const quietLens = resolvedTheme === "quiet-lens";
  // Phones hide the shell top bar on the Workbench, which draws its own header.
  // It stays for "Choose a project" and for a failed Core, where it carries recovery.
  const workbenchRoute = (location.pathname === "/" || /^\/projects\/[^/]+\/workbench\/?$/.test(location.pathname))
    && workspaceState !== "failed"
    && (Boolean(engagement) || workspaceState === "starting" || workspaceState === "bootstrapping");
  const [activityOpen, setActivityOpen] = useState(false);
  const [activityView, setActivityView] = useState<ActivityCenterView>("activity");
  const [paletteOpen, setPaletteOpen] = useState(false);
  const [projectPickerOpen, setProjectPickerOpen] = useState(false);
  const [settingLens, setSettingLens] = useState<{ entry: SettingCatalogEntry; returnFocus: HTMLElement | null; refreshKey: number; status?: string; statusMode?: "docker" | "host" }>();
  const [sidebarCollapsed, setSidebarCollapsed] = useState(() => {
    const stored = localStorage.getItem("nebula.sidebar.collapsed");
    return stored === null ? window.matchMedia("(max-width: 760px)").matches : stored === "true";
  });
  const [quietPointerActive, setQuietPointerActive] = useState(false);
  const [quietRailActive, setQuietRailActive] = useState(false);
  const [quietSideOpen, setQuietSideOpen] = useState(false);
  const [quietTopOpen, setQuietTopOpen] = useState(false);
  const quietPointerTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const quietRailTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const quietSideRevealSuppressed = useRef(false);
  const quietTopRevealSuppressed = useRef(false);
  const [toolbarHost, setToolbarHost] = useState<HTMLElement | null>(null);
  const [trailingToolbarHost, setTrailingToolbarHost] = useState<HTMLElement | null>(null);
  const [contextualCommands, setContextualCommands] = useState<ContextualCommand[]>([]);
  const authorizationRecovery = runtime?.reason === "browser_session_token_missing"
    ? browserAuthorizationRecovery(window.location.hostname)
    : undefined;
  const toggleActivity = useCallback(() => setActivityOpen((value) => !value), []);
  const toggleSidebar = useCallback(() => {
    if (quietLens) {
      setQuietSideOpen((value) => !value);
      return;
    }
    setSidebarCollapsed((value) => {
      localStorage.setItem("nebula.sidebar.collapsed", String(!value));
      return !value;
    });
  }, [quietLens]);
  const openPalette = useCallback(() => setPaletteOpen(true), []);
  const openSetting = useCallback((entry: SettingCatalogEntry, returnFocus?: HTMLElement | null) => {
    if (entry.target === "butler-settings") {
      window.dispatchEvent(new Event("nebula:open-butler-settings"));
      return;
    }
    if (entry.target === "butler-oversight") {
      window.dispatchEvent(new Event("nebula:open-butler-oversight"));
      return;
    }
    setSettingLens({ entry, returnFocus: returnFocus ?? (document.activeElement instanceof HTMLElement ? document.activeElement : null), refreshKey: 0 });
  }, []);
  const showSavedSetting = useCallback((entry: SettingCatalogEntry, status: string, mode?: "docker" | "host") => {
    setSettingLens((current) => current?.entry.id === entry.id
      ? { ...current, refreshKey: current.refreshKey + 1, status, statusMode: mode }
      : current);
  }, []);
  const openProjectPicker = useCallback(() => {
    if (quietLens) setQuietSideOpen(true);
    else setSidebarCollapsed(false);
    setProjectPickerOpen(true);
  }, [quietLens]);
  const closeMobileSidebar = useCallback(() => {
    if (quietLens) setQuietSideOpen(false);
    else if (!sidebarCollapsed && window.matchMedia("(max-width: 760px)").matches) toggleSidebar();
  }, [quietLens, sidebarCollapsed, toggleSidebar]);

  useEffect(() => {
    if (!quietLens) return;
    const refreshRail = () => {
      setQuietRailActive(true);
      if (quietRailTimer.current) clearTimeout(quietRailTimer.current);
      quietRailTimer.current = setTimeout(() => setQuietRailActive(false), 60_000);
    };
    setQuietRailActive(false);
    const onPointerMove = (event: PointerEvent) => {
      if (event.pointerType === "touch") return;
      refreshRail();
      setQuietPointerActive(true);
      if (quietPointerTimer.current) clearTimeout(quietPointerTimer.current);
      quietPointerTimer.current = setTimeout(() => setQuietPointerActive(false), 2600);
      if (event.target instanceof Element && !event.target.closest(".top-bar")) {
        const topBar = document.querySelector(".top-bar");
        if (!topBar?.contains(document.activeElement)) setQuietTopOpen(false);
      }
    };
    window.addEventListener("pointermove", onPointerMove, { passive: true });
    return () => {
      window.removeEventListener("pointermove", onPointerMove);
      if (quietPointerTimer.current) clearTimeout(quietPointerTimer.current);
      if (quietRailTimer.current) clearTimeout(quietRailTimer.current);
    };
  }, [quietLens]);

  useEffect(() => {
    setQuietSideOpen(false);
    setQuietTopOpen(false);
    quietSideRevealSuppressed.current = false;
    quietTopRevealSuppressed.current = false;
  }, [location.pathname]);

  useEffect(() => {
    if (!quietLens) return;
    const closeOutside = (event: PointerEvent) => {
      if (!(event.target instanceof Element)) return;
      if (!event.target.closest(".side-nav, .engagement-menu")) {
        quietSideRevealSuppressed.current = false;
        setQuietSideOpen(false);
      }
      if (!event.target.closest(".top-bar")) {
        quietTopRevealSuppressed.current = false;
        setQuietTopOpen(false);
      }
    };
    document.addEventListener("pointerdown", closeOutside);
    return () => document.removeEventListener("pointerdown", closeOutside);
  }, [quietLens]);
  const runContextualNew = useCallback(() => {
    (document.querySelector<HTMLButtonElement>(".mobile-conversation-header .mobile-new-chat:not(:disabled)")
      ?? document.querySelector<HTMLButtonElement>(".top-bar-trailing-actions .button.primary:not(:disabled)")
      ?? document.querySelector<HTMLButtonElement>(".top-bar-page-actions .button.primary:not(:disabled)"))?.click();
  }, []);

  const runCommand = useCallback((command: string) => {
    if (command === "command-center") openPalette();
    if (command === "toggle-sidebar") toggleSidebar();
    if (command === "toggle-inspector") toggleActivity();
    if (command === "settings") navigate("/settings");
    if (command === "home") navigate(engagement ? projectSurface(engagement.id, "workbench") : "/");
    if (command === "new-contextual") runContextualNew();
  }, [engagement, navigate, openPalette, runContextualNew, toggleActivity, toggleSidebar]);

  useGuideAction("open-palette", openPalette);

  useNavigationChords(navigate);

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
      if (event.key === "Escape") {
        if (paletteOpen) setPaletteOpen(false);
        if (quietLens) {
          const hovering = window.matchMedia("(hover: hover) and (pointer: fine)").matches;
          quietSideRevealSuppressed.current = hovering;
          quietTopRevealSuppressed.current = hovering;
          setQuietSideOpen(false);
          setQuietTopOpen(false);
        }
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [engagement, navigate, paletteOpen, quietLens, runContextualNew, toggleActivity, toggleSidebar]);

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
    trailingToolbarHost,
    openPalette,
    openSetting,
    openProjectPicker,
    setActivityOpen,
    setContextualCommands,
    setPaletteOpen,
    setToolbarHost,
    toggleActivity,
    toggleSidebar,
  }), [activityOpen, contextualCommands, openPalette, openProjectPicker, openSetting, paletteOpen, settingLens, sidebarCollapsed, toggleActivity, toggleSidebar, toolbarHost, trailingToolbarHost]);
  return (
    <ReleaseUpdateProvider>
      <WorkbenchEditorProvider>
        <WorkbenchDraftProvider>
          <ChromeProvider value={chrome}>
            <GuideProvider>
            {isNebulaShell() ? <NebulaControlAssistant onOpenSetting={openSetting} onNavigate={navigate} onCloseSetting={() => setSettingLens(undefined)} onSavedSetting={showSavedSetting}>
            <div className="app-shell assistant-only-shell workbench-route">
              <a className="skip-link" href="#main-content">Skip to chat</a>
              <main id="main-content" className="main-content" tabIndex={-1}>
                {workspaceState === "failed" && <div className="workspace-state-banner failed" role="alert"><DiagnosticErrorNotice error={coreError ?? "Check the Core connection and retry."} title={authorizationRecovery === "pair" ? "Pair this phone to continue." : "Core is unavailable."} fallback="Check the Core connection and retry." compact /><button className="button primary" type="button" onClick={reconnect}>Retry connection</button><a className="button quiet" href="nebula://settings">App connection</a></div>}
                {workspaceState === "degraded" && <div className="workspace-state-banner degraded" role="status"><strong>Some Core features are unavailable.</strong>{coreError && <small>{coreError}</small>}<button className="button quiet" type="button" onClick={reconnect}>Retry</button></div>}
                <Outlet />
              </main>
              {settingLens && <SettingsLens entry={settingLens.entry} key={settingLens.entry.id} returnFocus={settingLens.returnFocus} refreshKey={settingLens.refreshKey} status={settingLens.status} statusMode={settingLens.statusMode} onClose={() => setSettingLens(undefined)} />}
            </div>
            </NebulaControlAssistant> : <>
            {!isElectronRuntime() && <BrowserAutomationWorker />}
            <NebulaControlAssistant onOpenSetting={openSetting} onNavigate={navigate} onCloseSetting={() => setSettingLens(undefined)} onSavedSetting={showSavedSetting}>
            <div className={`app-shell${zero ? " zero-layer-shell" : ""}${quietLens ? " quiet-lens-shell" : ""}${workbenchRoute ? " workbench-route" : ""}${activityOpen ? " with-activity" : ""}${sidebarCollapsed && !quietLens ? " sidebar-collapsed" : ""}`} data-quiet-pointer={quietPointerActive ? "active" : "idle"} data-quiet-rail={quietRailActive ? "active" : "idle"}>
              <a className="skip-link" href="#main-content">Skip to main content</a>
              <SideNav collapsed={sidebarCollapsed && !quietLens} open={projectPickerOpen} setOpen={setProjectPickerOpen} onNavigate={closeMobileSidebar} variant={zero ? "zero" : "standard"} lensOpen={quietLens && (quietSideOpen || projectPickerOpen)} onToggleLens={quietLens ? () => setQuietSideOpen((value) => !value) : undefined} onRevealLens={quietLens ? () => { if (!quietSideRevealSuppressed.current) setQuietSideOpen(true); } : undefined} onLeaveLens={quietLens && !projectPickerOpen ? () => { quietSideRevealSuppressed.current = false; setQuietSideOpen(false); } : undefined} />
              <button className="sidebar-scrim" type="button" aria-label="Close sidebar" onClick={toggleSidebar} />
              <TopBar
                activityOpen={activityOpen}
                approvalsCount={approvals.length}
                onToggleActivity={toggleActivity}
                onToggleSidebar={toggleSidebar}
                onOpenPalette={openPalette}
                setToolbarHost={setToolbarHost}
                setTrailingToolbarHost={setTrailingToolbarHost}
                sidebarCollapsed={sidebarCollapsed && !quietLens}
                variant={zero ? "zero" : "standard"}
                lensOpen={quietLens && quietTopOpen}
                onToggleLens={quietLens ? () => { quietTopRevealSuppressed.current = false; setQuietTopOpen(true); } : undefined}
                onRevealLens={quietLens ? () => { if (!quietTopRevealSuppressed.current) setQuietTopOpen(true); } : undefined}
                onLeaveLens={quietLens ? () => { quietTopRevealSuppressed.current = false; setQuietTopOpen(false); } : undefined}
              />
              <main id="main-content" className="main-content" tabIndex={-1}>
                {zero && <span className="zero-route-flare" aria-hidden="true" key={`${location.pathname}${location.search}`} />}
                <div className="workspace-notices" role="region" aria-label="Workspace notices" tabIndex={0}>
                {workspaceState !== "failed" && <DiagnosticsAvailabilityBanner />}
                {workspaceState === "starting" && <div className="workspace-state-banner starting" role="status"><span><strong>Starting Nebula…</strong><small>{runtime?.mode === "desktop_remote" ? "Connecting this UI shell to the selected remote Core." : "Connecting to Nebula Core."}</small></span></div>}
                {workspaceState === "bootstrapping" && <div className="workspace-state-banner starting" role="status"><span><strong>Preparing your workspace…</strong><small>{setupStatus?.stageDetail ?? "Loading Projects and checking Terminal setup."}</small></span></div>}
                {workspaceState === "degraded" && <div className="workspace-state-banner degraded" role="status"><span><strong>Nebula is ready with limited features.</strong>{coreError && <small>{coreError}</small>}</span>{coreError && <button className="button quiet" type="button" onClick={reconnect}>Retry Core</button>}</div>}
                {workspaceState !== "failed" && Object.entries(resourceStatus).some(([, status]) => status.state === "failed") && <section className="workspace-resource-failures" aria-label="Workspace features needing attention">
                  {Object.entries(resourceStatus).filter(([, status]) => status.state === "failed").map(([resource, status]) => <div className="workspace-state-banner degraded" key={resource}>
                    <DiagnosticErrorNotice error={status.error} title={`${resourceLabels[resource] ?? resource} could not be loaded.`} fallback="This feature is temporarily unavailable; other loaded features remain usable." compact />
                    <button className="button quiet" type="button" disabled={status.state === "loading"} onClick={() => void retryResource(resource as Parameters<typeof retryResource>[0]).catch((error: unknown) => logDiagnostic({ level: "error", eventCode: "interface.workspace.resource_retry_failed", message: "A workspace resource retry failed.", outcome: "failure", stage: "resource-retry", retryable: true, exception: error }))}>Retry {resourceLabels[resource] ?? resource}</button>
                  </div>)}
                </section>}
                {workspaceState === "failed" && authorizationRecovery ? <div className="workspace-state-banner failed expired-session-state" role="alert"><span>{authorizationRecovery === "pair" ? <><strong>Pair this browser to continue</strong><small>On an authorized browser on the Nebula host, open Settings → Advanced → Identity &amp; Security → Paired devices, choose Pair phone, then scan the QR code. If this device was previously paired, pair it again because its access expired or was revoked.</small></> : <><strong>Browser session expired</strong><small>Nebula keeps the Core token in memory only, so reloading this page intentionally clears access. Close this tab and relaunch the interface with <code>nebula-core ui</code>.</small></>}</span></div> : workspaceState === "failed" ? <div className="workspace-state-banner failed"><DiagnosticErrorNotice error={coreError ?? "Check the Core service and reconnect."} title="Nebula Core is unavailable." fallback="Check the Core service and reconnect." compact /><button className="button primary" type="button" onClick={reconnect}>Reconnect</button></div> : null}
                <UpdateBanner />
                <HandoffRecoveryNotice />
                </div>
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
                onOpenSetting={openSetting}
              />
              {settingLens && <SettingsLens entry={settingLens.entry} key={settingLens.entry.id} returnFocus={settingLens.returnFocus} refreshKey={settingLens.refreshKey} status={settingLens.status} statusMode={settingLens.statusMode} onClose={() => setSettingLens(undefined)} />}
            </div>
            </NebulaControlAssistant>
            </>}
            </GuideProvider>
          </ChromeProvider>
        </WorkbenchDraftProvider>
      </WorkbenchEditorProvider>
    </ReleaseUpdateProvider>
  );
}
