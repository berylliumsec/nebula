import { lazy, Suspense, useEffect, type ReactNode } from "react";
import { Link, Navigate, Outlet, Route, Routes, useLocation, useParams } from "react-router-dom";
import { AppShell } from "./components/AppShell";
import { projectRoot, projectSurface, type ProjectSurface } from "./resourceRoutes";
import { useWorkspace } from "./state/WorkspaceContext";

const SessionsPage = lazy(() => import("./pages/SessionsPage").then((module) => ({ default: module.SessionsPage })));
const FindingsPage = lazy(() => import("./pages/FindingsPage").then((module) => ({ default: module.FindingsPage })));
const ProjectPage = lazy(() => import("./pages/ProjectPage").then((module) => ({ default: module.ProjectPage })));
const LibraryPage = lazy(() => import("./pages/LibraryPage").then((module) => ({ default: module.LibraryPage })));
const ReportsPage = lazy(() => import("./pages/ReportsPage").then((module) => ({ default: module.ReportsPage })));
const SettingsPage = lazy(() => import("./pages/SettingsPage").then((module) => ({ default: module.SettingsPage })));

function route(element: ReactNode) {
  return <Suspense fallback={<div className="route-loading" role="status">Loading workspace…</div>}>{element}</Suspense>;
}

function CanonicalProjectBoundary() {
  const { projectId = "" } = useParams();
  const { engagement, engagements, selectEngagement, workspaceState } = useWorkspace();
  const requested = engagements.find((item) => item.id === projectId);

  useEffect(() => {
    if (requested && engagement?.id !== requested.id) selectEngagement(requested.id);
  }, [engagement?.id, requested, selectEngagement]);

  if (workspaceState === "starting" || workspaceState === "bootstrapping") return <div className="route-loading" role="status">Opening project…</div>;
  if (!requested) {
    return <section className="page"><div className="standard-empty-state" role="alert"><h1>Project unavailable</h1><p>This link points to a deleted, inaccessible, or unknown project. Nebula did not substitute another project.</p>{engagement && <Link className="button primary" to={projectRoot(engagement.id)}>Open {engagement.name}</Link>}</div></section>;
  }
  if (engagement?.id !== requested.id) return <div className="route-loading" role="status">Switching project…</div>;
  return <Outlet />;
}

function LegacyProjectRedirect({ surface, legacyView }: { surface?: ProjectSurface | "project"; legacyView?: string }) {
  const { coreState, engagement, workspaceState } = useWorkspace();
  const location = useLocation();
  if (legacyView && !new URLSearchParams(location.search).has("view")) {
    const legacyParams = new URLSearchParams(location.search);
    legacyParams.set("view", legacyView);
    return <Navigate to={`${location.pathname}?${legacyParams.toString()}`} replace />;
  }
  if (!engagement && (workspaceState === "starting" || workspaceState === "bootstrapping")) return <div className="route-loading" role="status">Opening project…</div>;
  if (!engagement && coreState === "online") return <div className="route-loading" role="status">Opening project…</div>;
  if (!engagement) {
    if (surface === "findings") return route(<FindingsPage />);
    if (surface === "reports") return route(<ReportsPage />);
    if (surface === "project" || surface === "assets" || surface === "evidence" || surface === "sources") {
      const view = surface === "project" ? undefined : surface;
      return route(<ProjectPage canonicalView={view ?? "overview"} />);
    }
    return route(<SessionsPage />);
  }
  const params = new URLSearchParams(location.search);
  let targetSurface = surface;
  if (surface === "project") {
    const requested = params.get("view");
    targetSurface = requested === "assets" || requested === "evidence" || requested === "sources" ? requested : undefined;
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

export function App() {
  return <Routes><Route element={<AppShell />}>
    <Route path="projects/:projectId" element={<CanonicalProjectBoundary />}>
      <Route index element={route(<ProjectPage canonicalView="overview" />)} />
      <Route path="workbench" element={route(<SessionsPage />)} />
      <Route path="assets/:resourceId?" element={route(<ProjectPage canonicalView="assets" />)} />
      <Route path="evidence/:resourceId?" element={route(<ProjectPage canonicalView="evidence" />)} />
      <Route path="sources/:resourceId?" element={route(<ProjectPage canonicalView="sources" />)} />
      <Route path="findings/:resourceId?" element={route(<FindingsPage />)} />
      <Route path="reports/:resourceId?" element={route(<ReportsPage />)} />
    </Route>
    <Route path="library/:resourceId?" element={route(<LibraryPage />)} />
    <Route path="settings" element={route(<SettingsPage />)} />
    <Route index element={<LegacyProjectRedirect surface="workbench" />} />
    <Route path="findings" element={<LegacyProjectRedirect surface="findings" />} />
    <Route path="reports" element={<LegacyProjectRedirect surface="reports" />} />
    <Route path="project" element={<LegacyProjectRedirect surface="project" />} />
    <Route path="sessions" element={<LegacyWorkbenchRedirect view="chat" />} />
    <Route path="agents" element={<LegacyWorkbenchRedirect view="missions" />} />
    <Route path="missions" element={<LegacyWorkbenchRedirect view="missions" />} />
    <Route path="assets" element={<LegacyProjectRedirect surface="assets" />} />
    <Route path="evidence" element={<LegacyProjectRedirect surface="evidence" />} />
    <Route path="knowledge" element={<LegacyProjectRedirect surface="sources" />} />
    <Route path="*" element={<Navigate to="/" replace />} />
  </Route></Routes>;
}
