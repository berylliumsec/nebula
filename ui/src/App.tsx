import { lazy, Suspense, useEffect, type ReactNode } from "react";
import { Link, Navigate, Outlet, Route, Routes, useLocation, useParams } from "react-router-dom";
import { AppShell } from "./components/AppShell";
import { projectRoot, projectSurface, type ProjectSurface } from "./resourceRoutes";
import { rememberedProjectId, useWorkspace } from "./state/WorkspaceContext";
import { useChrome } from "./state/ChromeContext";
import { isNebulaShell } from "./hooks/useCompactLayout";
import { ShellButlerRoute, ShellProjectsPage, shellButlerPath } from "./components/ShellProjects";
import { MemoryBudgetSettings } from "./components/MemoryBudgetSettings";
import { RuntimeContinuityNotice } from "./components/RuntimeContinuityNotice";
import { NavigationContinuity } from "./state/NavigationContinuityBoundary";

const SessionsPage = lazy(() => import("./pages/SessionsPage").then((module) => ({ default: module.SessionsPage })));
const FindingsPage = lazy(() => import("./pages/FindingsPage").then((module) => ({ default: module.FindingsPage })));
const ProjectPage = lazy(() => import("./pages/ProjectPage").then((module) => ({ default: module.ProjectPage })));
const WorkPage = lazy(() => import("./pages/WorkPage").then((module) => ({ default: module.WorkPage })));
const LibraryPage = lazy(() => import("./pages/LibraryPage").then((module) => ({ default: module.LibraryPage })));
const ReportsPage = lazy(() => import("./pages/ReportsPage").then((module) => ({ default: module.ReportsPage })));
const SettingsPage = lazy(() => import("./pages/SettingsPage").then((module) => ({ default: module.SettingsPage })));
const StoragePage = lazy(() => import("./pages/StoragePage").then((module) => ({ default: module.StoragePage })));
const ButlerModePage = lazy(() => import("./pages/ButlerModePage").then((module) => ({ default: module.ButlerModePage })));

function route(element: ReactNode) {
  return <Suspense fallback={<div className="route-loading" role="status">Loading workspace…</div>}>{element}</Suspense>;
}

function CanonicalProjectBoundary() {
  const { projectId = "" } = useParams();
  const { continuityScope, engagement, engagements, selectEngagement, workspaceState } = useWorkspace();
  const requested = engagements.find((item) => item.id === projectId);

  useEffect(() => {
    if (requested && engagement?.id !== requested.id) selectEngagement(requested.id);
  }, [engagement?.id, requested, selectEngagement]);

  if ((workspaceState === "starting" || workspaceState === "bootstrapping")
    && (!requested || engagement?.id !== requested.id)) {
    return <div className="route-loading" role="status">Opening project…</div>;
  }
  if (!requested) {
    // Nothing is substituted, but the operator still needs a way back: the
    // project they were using, or any active one.
    const rememberedId = rememberedProjectId(continuityScope);
    const escape = engagement ?? engagements.find((item) => item.id === rememberedId) ?? engagements[0];
    return <section className="page"><div className="standard-empty-state" role="alert"><h1>Project unavailable</h1><p>This link points to a deleted, inaccessible, or unknown project. Nebula did not substitute another project.</p>{escape && <Link className="button primary" to={isNebulaShell() ? shellButlerPath(escape.id) : projectRoot(escape.id)}>Open {escape.name}</Link>}</div></section>;
  }
  if (engagement?.id !== requested.id) return <div className="route-loading" role="status">Switching project…</div>;
  // Reconnecting the current project refreshes authoritative data in place.
  // Keep its route mounted so current view state and unsent input survive.
  return <Outlet />;
}

function LegacyProjectRedirect({ surface, legacyView }: { surface?: ProjectSurface | "project"; legacyView?: string }) {
  const { coreState, engagement, workspaceState } = useWorkspace();
  const { openProjectPicker } = useChrome();
  const location = useLocation();
  if (legacyView && !new URLSearchParams(location.search).has("view")) {
    const legacyParams = new URLSearchParams(location.search);
    legacyParams.set("view", legacyView);
    return <Navigate to={`${location.pathname}?${legacyParams.toString()}`} replace />;
  }
  if (!engagement && (workspaceState === "starting" || workspaceState === "bootstrapping")) return <div className="route-loading" role="status">Opening project…</div>;
  if (!engagement && (surface === "project" || surface === "work")) return <Navigate to="/projects" replace />;
  if (!engagement && coreState === "online") return <section className="page"><div className="standard-empty-state"><h1>Choose a project</h1><p>Create a project or restore an archived one to start working.</p><button type="button" className="button primary" onClick={openProjectPicker}>Choose project</button></div></section>;
  if (!engagement) {
    if (surface === "findings") return route(<FindingsPage />);
    if (surface === "reports") return route(<ReportsPage />);
    if (surface === "project" || surface === "assets" || surface === "evidence" || surface === "sources" || surface === "results") {
      const view = surface === "project" ? undefined : surface;
      return route(<ProjectPage canonicalView={view ?? "overview"} />);
    }
    if (surface === "work") return <Navigate to="/projects" replace />;
    return route(<SessionsPage />);
  }
  const params = new URLSearchParams(location.search);
  let targetSurface = surface;
  if (surface === "project") {
    const requested = params.get("view");
    if (requested === "application-model" || requested === "model") {
      return <Navigate to={`${projectSurface(engagement.id, "workbench")}?view=chat`} replace />;
    }
    targetSurface = requested === "assets" || requested === "evidence" || requested === "sources" || requested === "results" ? requested : undefined;
  }
  if (surface === "project") params.delete("view");
  const objectId = targetSurface === "sources" ? params.get("source") : params.get("id");
  params.delete("source");
  params.delete("id");
  const base = targetSurface && targetSurface !== "project" ? projectSurface(engagement.id, targetSurface, objectId ?? undefined) : projectRoot(engagement.id);
  const query = params.toString();
  return <Navigate to={`${base}${query ? `?${query}` : ""}`} replace />;
}

function LegacyWorkbenchRedirect({ view }: { view: string }) {
  const location = useLocation();
  const params = new URLSearchParams(location.search);
  params.set("view", view);
  return <Navigate to={`/?${params.toString()}`} replace />;
}

function RetiredModelRedirect() {
  const { projectId = "" } = useParams();
  // Object/edge filters belong to the retired inspector, not a conversation.
  return <Navigate to={`${projectSurface(projectId, "workbench")}?view=chat`} replace />;
}

function WorkbenchRoute() {
  const location = useLocation();
  const params = new URLSearchParams(location.search);
  if (isNebulaShell() && (params.get("view") !== "chat" || params.has("side") || params.has("sideChat") || params.has("companion"))) {
    const chatParams = new URLSearchParams();
    chatParams.set("view", "chat");
    for (const key of ["session", "drawer", "message", "turn"]) {
      const value = params.get(key);
      if (value) chatParams.set(key, value);
    }
    return <Navigate to={`${location.pathname}?${chatParams}`} replace />;
  }
  if (["model", "application-model"].includes(params.get("view") ?? "")) {
    params.set("view", "chat");
    for (const key of ["object", "edge", "category", "query"]) params.delete(key);
    return <Navigate to={`${location.pathname}?${params}`} replace />;
  }
  return <SessionsPage />;
}

function AssistantShellLanding() {
  const { continuityScope, engagement, engagements, resourceStatus, workspaceState } = useWorkspace();
  if (workspaceState === "starting" || workspaceState === "bootstrapping") return <div className="route-loading" role="status">Opening Butler…</div>;
  const selected = engagement ?? engagements.find(item => item.id === rememberedProjectId(continuityScope)) ?? engagements[0];
  if (selected) return <Navigate to={shellButlerPath(selected.id)} replace />;
  if (resourceStatus.projects.state !== "empty") return null;
  return <section className="page assistant-shell-empty"><h1>No projects yet</h1><p>Set up a project in full Nebula, then return here to talk to Butler.</p><a className="button secondary" href="nebula://settings">App connection</a></section>;
}

/** The iPhone app has no project pages; project links open that project's Butler. */
function ShellProjectRedirect() {
  const { projectId = "" } = useParams();
  return <Navigate to={shellButlerPath(projectId)} replace />;
}

function AssistantShellHandoff() {
  const location = useLocation();
  const url = `${window.location.origin}${location.pathname}${location.search}`;
  return <section className="page assistant-shell-empty"><h1>Open in full Nebula</h1><p>This page is outside the iPhone app, which has only Butler and Projects.</p><label>Page link<input readOnly value={url} onFocus={event => event.currentTarget.select()} /></label><Link className="button primary" to="/">Back to Butler</Link></section>;
}

export function App() {
  const { api, health, reconnect } = useWorkspace();
  const location = useLocation();
  // A start or restart is not a hold: keep the workspace's reconnect handling
  // and send the operator to memory settings only when admission was refused.
  if (health?.apiReady === false && (health.apiAdmission === "starting" || health.apiAdmission === "stopping")) return <main className="page" id="main-content">
    <h1>{health.apiAdmission === "stopping" ? "Core is stopping" : "Core is starting"}</h1>
    <p role="status">The workspace reopens when Core is ready. Saved work and unsent drafts are kept.</p>
    <button className="button quiet" type="button" onClick={reconnect}>Check workspace availability</button>
  </main>;
  if (health?.apiReady === false) return <main className="page" id="main-content">
    <h1>Core recovery</h1>
    <p>Core needs a memory budget before the workspace can start. Saved work remains available after recovery.</p>
    <Link className="button primary" to="/settings#memory-budget-settings">Memory Settings</Link>
    <button className="button quiet" type="button" onClick={reconnect}>Check workspace availability</button>
    <RuntimeContinuityNotice api={api} />
    {location.pathname === "/settings" && <MemoryBudgetSettings api={api} />}
  </main>;
  return <NavigationContinuity><ApplicationRoutes /></NavigationContinuity>;
}

function ApplicationRoutes() {
  if (isNebulaShell()) return <Routes><Route element={<AppShell />}>
    <Route path="projects" element={<ShellProjectsPage />} />
    <Route path="projects/:projectId" element={<CanonicalProjectBoundary />}>
      <Route path="butler" element={<ShellButlerRoute />} />
      <Route index element={<ShellProjectRedirect />} />
      <Route path="*" element={<ShellProjectRedirect />} />
    </Route>
    <Route index element={<AssistantShellLanding />} />
    <Route path="*" element={<AssistantShellHandoff />} />
  </Route></Routes>;
  return <Routes><Route element={<AppShell />}>
    <Route path="projects/:projectId" element={<CanonicalProjectBoundary />}>
      <Route index element={route(<ProjectPage canonicalView="overview" />)} />
      <Route path="workbench" element={route(<WorkbenchRoute />)} />
      <Route path="work/:itemId?" element={route(<ProjectPage canonicalView="work" />)} />
      <Route path="assets/:resourceId?" element={route(<ProjectPage canonicalView="assets" />)} />
      <Route path="evidence/:resourceId?" element={route(<ProjectPage canonicalView="evidence" />)} />
      <Route path="sources/:resourceId?" element={route(<ProjectPage canonicalView="sources" />)} />
      <Route path="results/:resourceId?" element={route(<ProjectPage canonicalView="results" />)} />
      <Route path="application-model" element={<RetiredModelRedirect />} />
      <Route path="findings/:resourceId?" element={route(<FindingsPage />)} />
      <Route path="reports/:resourceId?" element={route(<ReportsPage />)} />
    </Route>
    <Route path="library/:resourceId?" element={route(<LibraryPage />)} />
    <Route path="projects" element={route(<WorkPage />)} />
    <Route path="storage" element={route(<StoragePage />)} />
    <Route path="butler" element={route(<ButlerModePage />)} />
    <Route path="work" element={<Navigate to="/projects" replace />} />
    <Route path="settings" element={route(<SettingsPage />)} />
    <Route index element={<LegacyProjectRedirect surface="workbench" />} />
    <Route path="findings" element={<LegacyProjectRedirect surface="findings" />} />
    <Route path="reports" element={<LegacyProjectRedirect surface="reports" />} />
    <Route path="project" element={<LegacyProjectRedirect surface="project" />} />
    <Route path="project/work" element={<LegacyProjectRedirect surface="work" />} />
    <Route path="sessions" element={<LegacyWorkbenchRedirect view="chat" />} />
    <Route path="agents" element={<LegacyWorkbenchRedirect view="missions" />} />
    <Route path="missions" element={<LegacyWorkbenchRedirect view="missions" />} />
    <Route path="assets" element={<LegacyProjectRedirect surface="assets" />} />
    <Route path="evidence" element={<LegacyProjectRedirect surface="evidence" />} />
    <Route path="knowledge" element={<LegacyProjectRedirect surface="sources" />} />
    <Route path="*" element={<Navigate to="/" replace />} />
  </Route></Routes>;
}
