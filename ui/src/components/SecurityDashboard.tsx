import { useCallback, useEffect, useState } from "react";
import { RefreshCw, Square } from "lucide-react";
import type { ApiClient } from "../api/client";
import type { SecurityBrowserAutomationStatus, SecurityBrowserResearchWorkspace, SecurityBrowserWorkspace } from "../api/types";
import { logCaughtDiagnostic } from "../diagnostics";

interface DashboardState {
  workspace: SecurityBrowserWorkspace;
  research: SecurityBrowserResearchWorkspace;
  automation: SecurityBrowserAutomationStatus;
}

export function SecurityDashboard({ api, projectId }: { api: ApiClient; projectId: string }) {
  const [data, setData] = useState<DashboardState>();
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const refresh = useCallback(async () => {
    try {
      const [workspace, research, automation] = await Promise.all([
        api.getSecurityBrowserWorkspace(projectId),
        api.getSecurityBrowserResearch(projectId),
        api.getSecurityBrowserAutomation(projectId),
      ]);
      setData({ workspace, research, automation }); setError("");
    } catch (caught) {
      void logCaughtDiagnostic("interface.security_dashboard.refresh_failed", "The agent security dashboard could not refresh.", caught, "security_dashboard");
      setError(caught instanceof Error ? caught.message : String(caught));
    } finally { setLoading(false); }
  }, [api, projectId]);
  useEffect(() => {
    setLoading(true); void refresh();
    const timer = window.setInterval(() => void refresh(), 3000);
    return () => clearInterval(timer);
  }, [refresh]);

  const securitySessions = data?.workspace.sessions.filter((item) => item.runtimeKind === "agent_security") ?? [];
  const sessionIds = new Set(securitySessions.map((item) => item.id));
  const traffic = data?.workspace.traffic.filter((item) => sessionIds.has(item.sessionId)) ?? [];
  const frames = data?.workspace.frames.filter((item) => sessionIds.has(item.sessionId)) ?? [];
  const intercepts = data?.research.intercepts.filter((item) => sessionIds.has(item.sessionId)) ?? [];
  const attacks = data?.research.attacks.filter((item) => sessionIds.has(item.sessionId)) ?? [];
  const repeater = data?.research.repeaterResults.filter((item) => {
    const tab = data.research.repeaterTabs.find((candidate) => candidate.id === item.tabId);
    return Boolean(tab && sessionIds.has(tab.sessionId));
  }) ?? [];
  const activeLeases = data?.automation.leases.filter((item) => sessionIds.has(item.sessionId) && item.status === "active") ?? [];
  const oldSessions = data?.workspace.sessions.filter((item) => item.runtimeKind !== "agent_security" && item.runtimeKind !== "electron_visible") ?? [];

  const stop = async () => {
    setBusy(true); setError("");
    try {
      await api.stopAgentSecurity(projectId);
      for (const runId of new Set(activeLeases.map((lease) => lease.runId))) {
        await api.stopSecurityBrowserAutomation(runId);
      }
      await refresh();
    } catch (caught) {
      void logCaughtDiagnostic("interface.security_dashboard.stop_failed", "The agent security session could not be stopped.", caught, "security_dashboard");
      setError(caught instanceof Error ? caught.message : String(caught));
    }
    finally { setBusy(false); }
  };
  const decide = async (interceptId: string, decision: "forward" | "drop") => {
    const intercept = intercepts.find((item) => item.id === interceptId);
    if (!intercept) return;
    setBusy(true); setError("");
    try { await api.decideSecurityBrowserIntercept(intercept, decision); await refresh(); }
    catch (caught) {
      void logCaughtDiagnostic("interface.security_dashboard.intercept_decision_failed", "The intercept decision could not be saved.", caught, "security_dashboard");
      setError(caught instanceof Error ? caught.message : String(caught));
    }
    finally { setBusy(false); }
  };

  return <section className="security-dashboard" aria-label="Security dashboard">
    <header><strong>Agent security</strong><span>{loading ? "Loading…" : `${securitySessions.length} session${securitySessions.length === 1 ? "" : "s"}`}</span>
      <button className="icon-button subtle" type="button" aria-label="Refresh security dashboard" title="Refresh security dashboard" onClick={() => void refresh()}><RefreshCw size={17} aria-hidden="true" /></button>
      <button className="button secondary" type="button" disabled={(!activeLeases.length && !securitySessions.some((item) => item.status === "active")) || busy} onClick={() => void stop()}><Square size={15} aria-hidden="true" /> Emergency stop</button>
    </header>
    {error && <div role="alert">Security dashboard could not refresh: {error} <button className="button quiet" onClick={() => void refresh()}>Retry</button></div>}
    {!loading && !securitySessions.length && <p className="security-dashboard-empty">No agent security session yet. Enable browser-security-testing-tools in Chat to start authorized research.</p>}
    <div className="security-dashboard-counts" role="status"><span>{traffic.length} HTTP exchanges</span><span>{frames.length} WebSocket frames</span><span>{intercepts.filter((item) => item.state === "paused").length} paused</span><span>{repeater.length} Repeater results</span><span>{attacks.length} Intruder runs</span></div>
    {intercepts.filter((item) => item.state === "paused").map((item) => <div className="security-dashboard-intercept" key={item.id}>
      <strong>{item.phase === "request" ? "Request paused" : "Response paused"}</strong><span>{item.method} {item.url}</span>
      <button className="button secondary" type="button" disabled={busy} onClick={() => void decide(item.id, "forward")}>Forward</button>
      <button className="button quiet" type="button" disabled={busy} onClick={() => void decide(item.id, "drop")}>Drop</button>
    </div>)}
    {traffic.length > 0 && <details><summary>Recent traffic</summary><ul>{traffic.slice(-30).reverse().map((item) => <li key={item.id}>{item.method} {item.url} · {item.statusCode ?? item.error ?? "pending"}</li>)}</ul></details>}
    {attacks.length > 0 && <details><summary>Intruder results</summary><ul>{attacks.slice(-20).reverse().map((item) => <li key={item.id}>{item.name} · {item.state} · {item.requestCount}/{item.maxRequests} requests</li>)}</ul></details>}
    {oldSessions.length > 0 && <details><summary>Previous browser research ({oldSessions.length})</summary><p>Historical Native and Assistant records remain available. They cannot receive new agent security commands.</p></details>}
  </section>;
}
