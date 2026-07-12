import { lazy, Suspense, type ReactNode } from "react";
import { Navigate, Route, Routes } from "react-router-dom";
import { AppShell } from "./components/AppShell";

const OverviewPage = lazy(() => import("./pages/OverviewPage").then((module) => ({ default: module.OverviewPage })));
const SessionsPage = lazy(() => import("./pages/SessionsPage").then((module) => ({ default: module.SessionsPage })));
const AgentsPage = lazy(() => import("./pages/AgentsPage").then((module) => ({ default: module.AgentsPage })));
const AssetsPage = lazy(() => import("./pages/AssetsPage").then((module) => ({ default: module.AssetsPage })));
const FindingsPage = lazy(() => import("./pages/FindingsPage").then((module) => ({ default: module.FindingsPage })));
const EvidencePage = lazy(() => import("./pages/EvidencePage").then((module) => ({ default: module.EvidencePage })));
const KnowledgePage = lazy(() => import("./pages/KnowledgePage").then((module) => ({ default: module.KnowledgePage })));
const ReportsPage = lazy(() => import("./pages/ReportsPage").then((module) => ({ default: module.ReportsPage })));
const SettingsPage = lazy(() => import("./pages/SettingsPage").then((module) => ({ default: module.SettingsPage })));

function route(element: ReactNode) {
  return <Suspense fallback={<div className="route-loading" role="status">Loading workspace…</div>}>{element}</Suspense>;
}

export function App() {
  return (
    <Routes>
      <Route element={<AppShell />}>
        <Route index element={route(<OverviewPage />)} />
        <Route path="sessions" element={route(<SessionsPage />)} />
        <Route path="agents" element={route(<AgentsPage />)} />
        <Route path="assets" element={route(<AssetsPage />)} />
        <Route path="findings" element={route(<FindingsPage />)} />
        <Route path="evidence" element={route(<EvidencePage />)} />
        <Route path="knowledge" element={route(<KnowledgePage />)} />
        <Route path="reports" element={route(<ReportsPage />)} />
        <Route path="settings" element={route(<SettingsPage />)} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Route>
    </Routes>
  );
}
