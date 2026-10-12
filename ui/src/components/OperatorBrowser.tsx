import { ArrowRight, MessageSquareText, Plus, RefreshCw, X } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import type { ApiClient } from "../api/client";
import { logCaughtDiagnostic } from "../diagnostics";
import type { NebulaDraftRequest } from "../state/WorkbenchDraftContext";
import { OperatorBrowserSurface } from "./OperatorBrowserSurface";

interface Tab { id: string; url: string; title: string }
interface Session { session_id: string; active_tab_id?: string; tabs: Tab[]; page_state_reset?: boolean }
interface Capture { url: string; title: string; text: string; page_revision: string; captured_at: string }

function browserAddress(value: string): string {
  const trimmed = value.trim();
  if (!trimmed) return "";
  return /^[a-z][a-z0-9+.-]*:\/\//i.test(trimmed) ? trimmed : `https://${trimmed}`;
}

export function OperatorBrowser({ api, projectId, active, initialOpenUrl, initialOpenRequest, onContext }: {
  api: ApiClient;
  projectId: string;
  active: boolean;
  initialOpenUrl?: string;
  initialOpenRequest?: string;
  onContext: (request: NebulaDraftRequest) => void;
}) {
  const [session, setSession] = useState<Session>();
  const [tabId, setTabId] = useState("");
  const [tabs, setTabs] = useState<Tab[]>([]);
  const [address, setAddress] = useState("");
  const [frame, setFrame] = useState("");
  const [connected, setConnected] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [connectionEpoch, setConnectionEpoch] = useState(0);
  const socket = useRef<WebSocket | undefined>(undefined);
  const addressEdited = useRef(false);
  const mounted = useRef(true);
  const openedRequest = useRef<string | undefined>(undefined);

  const request = useCallback(async <T,>(path: string, body?: unknown, method = "POST"): Promise<T> => api.request<T>(path, {
    method,
    ...(body === undefined ? {} : { body: JSON.stringify(body), headers: { "Content-Type": "application/json" } }),
  }), [api]);
  const logCaughtDiagnosticFailure = (caught: unknown) => {
    void logCaughtDiagnostic("interface.operator_browser.operation_failed", "The interactive browser could not complete an operation.", caught, "operator_browser");
    if (mounted.current) setError(caught instanceof Error ? caught.message : String(caught));
  };
  const operation = useCallback(<T,>(kind: string, extra: Record<string, unknown> = {}) => {
    if (!session) throw new Error("Connect the browser first.");
    return request<T>(`browser-companion/${session.session_id}/operator-operations`, { operation: kind, tab_id: tabId, ...extra });
  }, [request, session, tabId]);

  const open = useCallback(async () => {
    setBusy(true); setError("");
    try {
      const next = await request<Session>(`engagements/${encodeURIComponent(projectId)}/browser-companion?mode=operator`);
      if (!mounted.current) return;
      const selected = next.tabs.find(tab => tab.id === next.active_tab_id) ?? next.tabs[0];
      setSession(next); setTabs(next.tabs); setTabId(selected?.id ?? "");
      if (!addressEdited.current) setAddress(selected?.url === "about:blank" ? "" : selected?.url ?? "");
      if (next.page_state_reset) setError("The browser restarted and its live tabs were lost. Reopen the page to continue.");
      setConnectionEpoch(value => value + 1);
    } catch (caught) { logCaughtDiagnosticFailure(caught); }
    finally { if (mounted.current) setBusy(false); }
  }, [projectId, request]);

  useEffect(() => {
    mounted.current = true;
    if (active && !session) void open();
    return () => { mounted.current = false; };
  }, [active, open, session]);

  useEffect(() => {
    if (!active || !session || !tabId) return;
    setConnected(false); setFrame("");
    const connection = api.openOperatorBrowserStream(session.session_id, tabId);
    socket.current = connection;
    connection.onopen = () => setConnected(true);
    connection.onmessage = event => {
      try {
        const value = JSON.parse(String(event.data));
        if (value.kind === "frame" && typeof value.data === "string") setFrame(`data:image/jpeg;base64,${value.data}`);
      } catch (caught) { logCaughtDiagnosticFailure(caught); }
    };
    connection.onerror = () => setError("The page stream failed. Reconnect to continue.");
    connection.onclose = () => { setConnected(false); setError(current => current || "The page stream ended. Reconnect to continue."); };
    return () => {
      connection.onclose = null; connection.onmessage = null; connection.onerror = null; connection.onopen = null;
      connection.close();
      if (socket.current === connection) socket.current = undefined;
    };
  }, [active, api, connectionEpoch, session?.session_id, tabId]);

  useEffect(() => {
    if (!active || !session) return;
    let cancelled = false;
    const refresh = async () => {
      try {
        const latest = await request<{ tabs: Tab[] }>(`browser-companion/${session.session_id}/operator-operations`, { operation: "tabs" });
        if (cancelled) return;
        setTabs(latest.tabs);
        const selected = latest.tabs.find(tab => tab.id === tabId);
        if (selected && !addressEdited.current) setAddress(selected.url === "about:blank" ? "" : selected.url);
      } catch (caught) { if (!cancelled) logCaughtDiagnosticFailure(caught); }
    };
    const timer = window.setInterval(() => void refresh(), 3000);
    return () => { cancelled = true; window.clearInterval(timer); };
  }, [active, request, session?.session_id, tabId]);

  const navigate = async (value: string) => {
    const url = browserAddress(value);
    if (!url || busy) return;
    setBusy(true); setError("");
    try {
      const result = await operation<Capture>("navigate", { url });
      addressEdited.current = false;
      setAddress(result.url);
    } catch (caught) { logCaughtDiagnosticFailure(caught); }
    finally { if (mounted.current) setBusy(false); }
  };

  useEffect(() => {
    if (!active || !session || !tabId || !initialOpenUrl || !initialOpenRequest) return;
    const requestId = `${projectId}:${initialOpenRequest}`;
    if (openedRequest.current === requestId) return;
    openedRequest.current = requestId;
    void navigate(initialOpenUrl);
  }, [active, initialOpenRequest, initialOpenUrl, projectId, session?.session_id, tabId]);

  const manageTab = async (kind: "new_tab" | "close_tab") => {
    if (busy) return;
    setBusy(true); setError("");
    try {
      const next = await operation<{ tabs: Tab[]; active_tab_id?: string }>(kind);
      const selected = next.tabs.find(tab => tab.id === next.active_tab_id) ?? next.tabs[0];
      setTabs(next.tabs); setTabId(selected?.id ?? "");
      addressEdited.current = false;
      setAddress(selected?.url === "about:blank" ? "" : selected?.url ?? "");
    } catch (caught) { logCaughtDiagnosticFailure(caught); }
    finally { if (mounted.current) setBusy(false); }
  };

  const capture = async () => {
    setBusy(true); setError("");
    try {
      const result = await operation<Capture>("capture");
      const source = new URL(result.url);
      source.username = ""; source.password = ""; source.search = ""; source.hash = "";
      onContext({ text: `BROWSER CONTENT\nURL: ${source}\nTitle: ${result.title}\nCaptured: ${result.captured_at}\nTab: ${tabId}\nPage revision: ${result.page_revision}\n\n${result.text}`,
        sourceKind: "browser_companion", sourceId: session?.session_id ?? "", sourceLabel: result.title || source.toString(), truncated: result.text.length >= 12000 });
    } catch (caught) { logCaughtDiagnosticFailure(caught); }
    finally { if (mounted.current) setBusy(false); }
  };

  const send = useCallback((event: Record<string, unknown>) => {
    if (socket.current?.readyState === WebSocket.OPEN) socket.current.send(JSON.stringify(event));
  }, []);

  return <section className="operator-browser" aria-label="Interactive browser">
    <form className="operator-browser-toolbar" onSubmit={event => { event.preventDefault(); void navigate(address); }}>
      <select aria-label="Browser tab" value={tabId} disabled={!session || busy} onChange={event => {
        const selected = event.target.value;
        setTabId(selected);
        addressEdited.current = false;
        setAddress(tabs.find(tab => tab.id === selected)?.url ?? "");
        if (session) void request(`browser-companion/${session.session_id}/active-tab/${encodeURIComponent(selected)}`, undefined, "PUT").catch(caught => logCaughtDiagnosticFailure(caught));
      }}>{tabs.map(tab => <option key={tab.id} value={tab.id}>{tab.title || "New tab"}</option>)}</select>
      <button type="button" className="button quiet managed-browser-icon" aria-label="New tab" title="New tab" disabled={!session || busy} onClick={() => void manageTab("new_tab")}><Plus size={18} aria-hidden="true" /></button>
      <button type="button" className="button quiet managed-browser-icon" aria-label="Close tab" title="Close tab" disabled={!session || busy} onClick={() => void manageTab("close_tab")}><X size={18} aria-hidden="true" /></button>
      <input aria-label="Browser address" value={address} placeholder="https://…" onChange={event => { addressEdited.current = true; setAddress(event.target.value); }} />
      <button className="button quiet managed-browser-icon" aria-label="Go" title="Go" disabled={!session || busy}><ArrowRight size={18} aria-hidden="true" /></button>
      <button type="button" className="button quiet managed-browser-icon" aria-label="Ask about page" title="Ask about page" disabled={!session || busy} onClick={() => void capture()}><MessageSquareText size={18} aria-hidden="true" /></button>
      <button type="button" className="button quiet managed-browser-icon" aria-label="Reconnect browser" title="Reconnect browser" disabled={busy} onClick={() => void open()}><RefreshCw size={18} aria-hidden="true" /></button>
    </form>
    <p role="status">{busy ? "Loading browser…" : connected ? "Browser connected" : "Browser disconnected"}</p>
    {error && <div className="operator-browser-error" role="alert">{error}<button type="button" className="button quiet" onClick={() => void open()}>Reconnect</button></div>}
    <OperatorBrowserSurface frame={frame} connected={connected} send={send} />
  </section>;
}
