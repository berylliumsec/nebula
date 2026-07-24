import { lazy, Suspense, type ReactNode } from "react";
import { Navigate, Route, Routes, useLocation } from "react-router-dom";
import { AppShell } from "./components/AppShell";

const SessionsPage = lazy(() => import("./pages/SessionsPage").then((module) => ({ default: module.SessionsPage })));
const FindingsPage = lazy(() => import("./pages/FindingsPage").then((module) => ({ default: module.FindingsPage })));
const ProjectPage = lazy(() => import("./pages/ProjectPage").then((module) => ({ default: module.ProjectPage })));
const LibraryPage = lazy(() => import("./pages/LibraryPage").then((module) => ({ default: module.LibraryPage })));
const ReportsPage = lazy(() => import("./pages/ReportsPage").then((module) => ({ default: module.ReportsPage })));
const SettingsPage = lazy(() => import("./pages/SettingsPage").then((module) => ({ default: module.SettingsPage })));

function route(element: ReactNode) {
  return <Suspense fallback={<div className="route-loading" role="status">Loading workspace…</div>}>{element}</Suspense>;
}

function LegacyRedirect({ destination, view }: { destination: "/" | "/project"; view: string }) {
  const location = useLocation();
  const params = new URLSearchParams(location.search);
  params.set("view", view);
  return <Navigate to={`${destination}?${params.toString()}`} replace />;
}

export function App() {
  return (
    <Routes>
      <Route element={<AppShell />}>
        <Route index element={route(<SessionsPage />)} />
        <Route path="findings" element={route(<FindingsPage />)} />
        <Route path="reports" element={route(<ReportsPage />)} />
        <Route path="project" element={route(<ProjectPage />)} />
        <Route path="library" element={route(<LibraryPage />)} />
        <Route path="settings" element={route(<SettingsPage />)} />
        <Route path="sessions" element={<LegacyRedirect destination="/" view="chat" />} />
        <Route path="agents" element={<LegacyRedirect destination="/" view="missions" />} />
        <Route path="missions" element={<LegacyRedirect destination="/" view="missions" />} />
        <Route path="assets" element={<LegacyRedirect destination="/project" view="assets" />} />
        <Route path="evidence" element={<LegacyRedirect destination="/project" view="evidence" />} />
        <Route path="knowledge" element={<LegacyRedirect destination="/project" view="sources" />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Route>
    </Routes>
  );
}
