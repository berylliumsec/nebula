import { useCallback, useEffect, useLayoutEffect, useRef, useState, type FormEvent } from "react";
import { ArrowLeft, ArrowRight, Globe2, MessageSquareText, Plus, RefreshCw, ShieldCheck, X } from "lucide-react";
import type { ApiClient } from "../api/client";
import { electronDesktop, type ElectronBrowserState, type ElectronRect } from "../api/electronDesktop";
import { normalizeBrowserInput } from "../api/workbenchBrowser";
import { logCaughtDiagnostic } from "../diagnostics";
import type { NebulaDraftRequest } from "../state/WorkbenchDraftContext";
import { SecurityDashboard } from "./SecurityDashboard";

interface CompanionSession { session_id: string; conversation_id?: string; active_tab_id?: string }
interface PendingAction {
  id: string; status: string; expires_at: string;
  request: { operation: string; tab_id: string; element_id?: string; url?: string; text?: string };
}
interface ApprovalChoice { allow_all: boolean; revision: number }

const rect = (value: DOMRect): ElectronRect => ({
  x: value.x, y: value.y, width: value.width, height: value.height,
});

export function ElectronBrowser({ active, api, projectId, conversationId, draftId,
  initialOpenUrl, initialOpenRequest, onContext, onControlChange }: {
  active: boolean; api: ApiClient; projectId: string; conversationId?: string; draftId?: string;
  initialOpenUrl?: string; initialOpenRequest?: string;
  onContext?: (request: NebulaDraftRequest) => void;
  onControlChange?: (enabled: boolean) => void;
}) {
  const desktop = electronDesktop();
  const [state, setState] = useState<ElectronBrowserState>();
  const [session, setSession] = useState<CompanionSession>();
  const [address, setAddress] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [paused, setPaused] = useState(true);
  const [actions, setActions] = useState<PendingAction[]>([]);
  const [approval, setApproval] = useState<ApprovalChoice>();
  const [confirmAllowAll, setConfirmAllowAll] = useState(false);
  const [approvalBusy, setApprovalBusy] = useState(false);
  const rootRef = useRef<HTMLElement>(null);
  const controlsRef = useRef<HTMLDivElement>(null);
  const pageRef = useRef<HTMLDivElement>(null);
  const openedRequest = useRef<string | undefined>(undefined);

  const request = useCallback(<T,>(path: string, body?: unknown, method = "GET") =>
    api.request<T>(path, { method, ...(body === undefined ? {} : {
      body: JSON.stringify(body), headers: { "Content-Type": "application/json" },
    }) }), [api]);
  const logCaughtDiagnosticAndShow = (caught: unknown) => {
    void logCaughtDiagnostic("interface.browser.visible_operation_failed", "The visible browser could not complete an operation.", caught, "electron_browser");
    setError(caught instanceof Error ? caught.message : String(caught));
  };

  useEffect(() => {
    if (!desktop) return;
    return desktop.browser.onState((next) => {
      if (next.projectId === projectId) setState(next);
    });
  }, [desktop, projectId]);

  useEffect(() => {
    if (!desktop || (!active && !session)) return;
    let current = true;
    setBusy(true); setError("");
    void (async () => {
      if (active) {
        const opened = await desktop.browser.open(projectId);
        if (current) setState(opened);
      }
      const params = new URLSearchParams();
      if (conversationId) params.set("conversation_id", conversationId);
      else if (draftId) params.set("draft_id", draftId);
      const attached = await request<CompanionSession>(
        `engagements/${encodeURIComponent(projectId)}/browser-companion${params.size ? `?${params}` : ""}`,
        {}, "POST",
      );
      if (!current) return;
      setSession(attached);
      const [control, choice] = await Promise.all([
        request<{ paused: boolean }>(`browser-companion/${attached.session_id}/control`),
        request<ApprovalChoice>(`engagements/${encodeURIComponent(projectId)}/browser-security-approval`),
      ]);
      if (current) { setPaused(control.paused); setApproval(choice); }
    })().catch((caught) => { if (current) logCaughtDiagnosticAndShow(caught); })
      .finally(() => { if (current) setBusy(false); });
    return () => { current = false; };
  }, [active, api, conversationId, desktop, draftId, projectId]);

  useEffect(() => {
    onControlChange?.(Boolean(session && !paused));
    return () => onControlChange?.(false);
  }, [onControlChange, paused, session?.session_id]);

  useEffect(() => {
    const selected = state?.tabs.find((tab) => tab.id === state.activeTabId);
    if (selected) setAddress(selected.url === "about:blank" ? "" : selected.url);
  }, [state?.activeTabId, state?.tabs]);

  useEffect(() => {
    if (!session || !state?.activeTabId) return;
    void request(`browser-companion/${session.session_id}/active-tab/${state.activeTabId}`, {}, "PUT")
      .catch((caught) => logCaughtDiagnosticAndShow(caught));
  }, [request, session?.session_id, state?.activeTabId]);

  useEffect(() => {
    if (!session) return;
    let current = true;
    const refresh = () => void request<PendingAction[]>(`browser-companion/${session.session_id}/actions`)
      .then((next) => { if (current) setActions(next.filter((item) => item.status === "pending")); })
      .catch((caught) => { if (current) logCaughtDiagnosticAndShow(caught); });
    refresh();
    const timer = window.setInterval(refresh, 1200);
    return () => { current = false; clearInterval(timer); };
  }, [request, session?.session_id]);

  const place = useCallback(() => {
    if (!desktop || !active || !rootRef.current || !controlsRef.current || !pageRef.current) {
      void desktop?.browser.layout(null); return;
    }
    const root = rootRef.current;
    const hasDialog = [...document.querySelectorAll<HTMLElement>(
      '[role="dialog"],[role="alertdialog"],dialog[open],[aria-modal="true"],.electron-browser-status details[open]',
    )].some((element) => element.getClientRects().length > 0);
    void desktop.browser.layout({
      viewport: { x: 0, y: 0, width: innerWidth, height: innerHeight },
      panel: rect(root.getBoundingClientRect()),
      controls: rect(controlsRef.current.getBoundingClientRect()),
      page: rect(pageRef.current.getBoundingClientRect()),
      dialogOpen: hasDialog || root.offsetParent === null,
    });
  }, [active, desktop]);

  useLayoutEffect(() => {
    const nodes = [rootRef.current, controlsRef.current, pageRef.current].filter((node): node is HTMLElement => Boolean(node));
    const observer = new ResizeObserver(() => requestAnimationFrame(place));
    nodes.forEach((node) => observer.observe(node));
    const mutations = new MutationObserver(() => requestAnimationFrame(place));
    mutations.observe(document.body, { childList: true, subtree: true, attributes: true,
      attributeFilter: ["open", "aria-modal", "hidden", "class", "style"] });
    window.addEventListener("resize", place);
    window.addEventListener("nebula:ui-zoom", place);
    requestAnimationFrame(place);
    return () => {
      observer.disconnect(); mutations.disconnect();
      window.removeEventListener("resize", place); window.removeEventListener("nebula:ui-zoom", place);
      void desktop?.browser.layout(null);
    };
  }, [desktop, place]);

  const selected = state?.tabs.find((tab) => tab.id === state.activeTabId);
  const navigate = async (event: FormEvent) => {
    event.preventDefault();
    if (!desktop || !state?.activeTabId) return;
    setBusy(true); setError("");
    try {
      setState(await desktop.browser.navigate(projectId, state.activeTabId, normalizeBrowserInput(address)));
    } catch (caught) { logCaughtDiagnosticAndShow(caught); }
    finally { setBusy(false); }
  };
  const move = async (action: "back" | "forward" | "reload") => {
    if (!desktop || !state?.activeTabId) return;
    try { setState(await desktop.browser.command(projectId, state.activeTabId, action)); }
    catch (caught) { logCaughtDiagnosticAndShow(caught); }
  };
  const selectTab = async (tabId: string) => {
    if (!desktop) return;
    try {
      setState(await desktop.browser.selectTab(projectId, tabId));
    } catch (caught) { logCaughtDiagnosticAndShow(caught); }
  };
  const setControl = async (nextPaused: boolean) => {
    if (!session) return;
    try {
      await request(`browser-companion/${session.session_id}/control?paused=${nextPaused}`, {}, "PUT");
      setPaused(nextPaused);
    } catch (caught) { logCaughtDiagnosticAndShow(caught); }
  };
  const decide = async (action: PendingAction, decision: "approve" | "reject") => {
    if (!session) return;
    try {
      await request(`browser-companion/${session.session_id}/actions/${action.id}`, { decision }, "POST");
      setActions((current) => current.filter((item) => item.id !== action.id));
    } catch (caught) { logCaughtDiagnosticAndShow(caught); }
  };
  const setAllowAll = async (allowAll: boolean) => {
    if (!approval) return;
    setApprovalBusy(true); setError("");
    try {
      const next = await request<ApprovalChoice>(`engagements/${encodeURIComponent(projectId)}/browser-security-approval`,
        { allow_all: allowAll, expected_revision: approval.revision }, "PUT");
      setApproval(next); setConfirmAllowAll(false);
      if (session) await setControl(!allowAll);
    } catch (caught) { logCaughtDiagnosticAndShow(caught); }
    finally { setApprovalBusy(false); }
  };
  const capture = async () => {
    if (!session || !state?.activeTabId || !onContext) return;
    try {
      const page = await request<{ url: string; title: string; text: string; page_revision: string }>(
        `browser-companion/${session.session_id}/operations`,
        { operation: "capture", tab_id: state.activeTabId }, "POST");
      onContext({ text: `BROWSER CONTENT\nURL: ${page.url}\nTitle: ${page.title}\nPage revision: ${page.page_revision}\n\n${page.text}`,
        sourceKind: "browser_companion", sourceId: session.session_id,
        sourceLabel: page.title || page.url, truncated: page.text.length >= 12000 });
    } catch (caught) { logCaughtDiagnosticAndShow(caught); }
  };

  useEffect(() => {
    if (!desktop || !session || !state?.activeTabId || !initialOpenUrl || !initialOpenRequest) return;
    const key = `${projectId}:${initialOpenRequest}`;
    if (openedRequest.current === key) return;
    openedRequest.current = key;
    void desktop.browser.navigate(projectId, state.activeTabId, initialOpenUrl)
      .then(setState).catch((caught) => logCaughtDiagnosticAndShow(caught));
  }, [desktop, initialOpenRequest, initialOpenUrl, projectId, session?.session_id, state?.activeTabId]);

  if (!desktop) return <><div className="electron-browser-handoff">Open this Project in the Nebula desktop app to use its visible browser. The browser-security-testing-tools MCP and research results remain available here.</div><SecurityDashboard api={api} projectId={projectId} /></>;
  return <section className="electron-browser" ref={rootRef} aria-label="Project browser">
    <div className="electron-browser-controls" ref={controlsRef}>
      <div className="electron-browser-tabs" role="tablist" aria-label="Browser tabs">
        {state?.tabs.map((tab) => <div className={`electron-browser-tab${tab.id === state.activeTabId ? " selected" : ""}`} key={tab.id}>
          <button type="button" role="tab" aria-selected={tab.id === state.activeTabId}
            onClick={() => void selectTab(tab.id)} title={tab.title}>{tab.title || "New tab"}</button>
          <button className="icon-button subtle" type="button" aria-label={`Close ${tab.title || "New tab"}`}
            title={`Close ${tab.title || "New tab"}`} onClick={() => void desktop.browser.closeTab(projectId, tab.id).then(setState).catch((caught) => logCaughtDiagnosticAndShow(caught))}><X size={14} aria-hidden="true" /></button>
        </div>)}
        <button className="icon-button subtle" type="button" aria-label="New browser tab" title="New browser tab"
          onClick={() => void desktop.browser.newTab(projectId).then(setState).catch((caught) => logCaughtDiagnosticAndShow(caught))}><Plus size={17} aria-hidden="true" /></button>
      </div>
      <form className="electron-browser-navigation" onSubmit={(event) => void navigate(event)}>
        <button className="icon-button subtle" type="button" aria-label="Back" title="Back" onClick={() => void move("back")}><ArrowLeft size={17} aria-hidden="true" /></button>
        <button className="icon-button subtle" type="button" aria-label="Forward" title="Forward" onClick={() => void move("forward")}><ArrowRight size={17} aria-hidden="true" /></button>
        <button className="icon-button subtle" type="button" aria-label="Reload page" title="Reload page" onClick={() => void move("reload")}><RefreshCw size={17} aria-hidden="true" /></button>
        <label className="sr-only" htmlFor="electron-browser-address">Browser address</label>
        <input id="electron-browser-address" value={address} onChange={(event) => setAddress(event.target.value)} placeholder="Enter an address or search" autoComplete="off" spellCheck={false} />
        <button className="button quiet" type="submit" disabled={busy || !state?.activeTabId}>Go</button>
        <button className="icon-button subtle" type="button" aria-label="Ask about page" title="Ask about page" disabled={!session || !selected?.url || !onContext}
          onClick={() => void capture()}><MessageSquareText size={17} aria-hidden="true" /></button>
      </form>
      <div className="electron-browser-status">
        <span><Globe2 size={14} aria-hidden="true" />{busy ? "Connecting…" : selected?.loading ? "Loading…" : session ? "Browser connected" : "Browser unavailable"}</span>
        {session && <button className="button quiet" type="button" onClick={() => void setControl(!paused)}>{paused ? "Allow chat control" : "Pause chat control"}</button>}
        <details><summary><ShieldCheck size={15} aria-hidden="true" /> Approvals</summary>
          <div className="electron-browser-approval-menu"><p>Project scope and high-risk grants always apply.</p>
            {approval?.allow_all ? <button className="button secondary" type="button" disabled={approvalBusy} onClick={() => void setAllowAll(false)}>Revoke Allow all</button>
              : <button className="button secondary" type="button" disabled={approvalBusy} onClick={() => setConfirmAllowAll(true)}>Allow all browser and security actions until revoked</button>}</div>
        </details>
      </div>
      {error && <div className="electron-browser-error" role="alert">{error} <button type="button" className="button quiet" onClick={() => { setError(""); void desktop.browser.open(projectId).then(setState).catch((caught) => logCaughtDiagnosticAndShow(caught)); }}>Retry browser</button></div>}
      {selected?.error && <div className="electron-browser-error" role="alert">{selected.error}</div>}
      {actions.map((action) => <div className="electron-browser-action" key={action.id} role="group" aria-label="Browser action approval">
        <strong>Assistant requests {action.request.operation}</strong><span>{action.request.url || action.request.element_id || "Current page"}</span>
        <button type="button" className="button secondary" onClick={() => void decide(action, "approve")}>Approve action</button>
        <button type="button" className="button quiet" onClick={() => void decide(action, "reject")}>Reject</button>
      </div>)}
      <details className="electron-browser-dashboard"><summary>Security dashboard</summary><SecurityDashboard api={api} projectId={projectId} /></details>
    </div>
    <div className="electron-browser-page" ref={pageRef} aria-label="Live browser page" />
    {confirmAllowAll && <div className="electron-browser-confirm" role="alertdialog" aria-modal="true" aria-label="Allow all browser and security actions">
      <strong>Allow all browser and security actions until revoked?</strong>
      <p>Agents can act in the visible browser and run browser-security-testing-tools without repeated prompts. Project scope, high-risk grants, limits, and audit still apply.</p>
      <button className="button primary" type="button" disabled={approvalBusy} onClick={() => void setAllowAll(true)}>Allow all until revoked</button>
      <button className="button quiet" type="button" onClick={() => setConfirmAllowAll(false)}>Cancel</button>
    </div>}
  </section>;
}
