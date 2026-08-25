import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent } from "react";
import { ArrowLeft, ArrowRight, BookOpenCheck, BookPlus, Bug, Check, Download, ExternalLink, GitCompareArrows, Globe2, History, LoaderCircle, Network, Plus, RefreshCw, Search, ShieldCheck, Sparkles, Trash2, UserRound, X } from "lucide-react";
import { listen } from "@tauri-apps/api/event";
import { Link } from "react-router-dom";
import { desktopDeviceId, isTauriRuntime } from "../api/runtime";
import {
  buildBrowserScopeAddition,
  evaluateBrowserScope,
  formatBrowserContextForAssistant,
  normalizeBrowserInput,
  workbenchBrowser,
  type BrowserBounds,
  type BrowserCapabilities,
  type BrowserContextEvent,
  type BrowserDownloadEvent,
  type BrowserPageEvent,
  type BrowserScopeRequestEvent,
  type BrowserTrafficEvent,
  type BrowserWebSocketFrameEvent,
  type BrowserActionEvent,
} from "../api/workbenchBrowser";
import type { EngagementScopePolicy, EvidenceSummary, EvidenceUploadRequest } from "../api/types";
import type { ApiClient } from "../api/client";
import type { ProviderHealth, SecurityBrowserAutomationStatus, SecurityBrowserExchange, SecurityBrowserSession, SecurityBrowserWorkspace } from "../api/types";
import { logCaughtDiagnostic } from "../diagnostics";
import { useChrome } from "../state/ChromeContext";
import type { NebulaDraftRequest } from "../state/WorkbenchDraftContext";
import { useConfirmation, useDialogOpen } from "./DialogSystem";

interface BrowserTab {
  id: string;
  address: string;
  url?: string;
  title: string;
  loading: boolean;
  created: boolean;
  error?: string;
}

interface WorkbenchBrowserProps {
  active: boolean;
  api: ApiClient;
  operatorId?: string;
  projectId: string;
  scope?: EngagementScopePolicy;
  scopeLoading?: boolean;
  onAddKnowledgeUrl: (url: string) => Promise<{ id: string; name: string }>;
  onAskNebula: (request: NebulaDraftRequest) => void;
  onOpenFiles: () => void;
  onScopeUpdated: (scope: EngagementScopePolicy) => void;
  onUploadEvidence?: (request: EvidenceUploadRequest) => Promise<EvidenceSummary>;
}

type BrowserNotice =
  | { kind: "download"; message: string }
  | { kind: "knowledge"; message: string; sourceId: string }
  | { kind: "info"; message: string };

const MAX_TABS = 16;
const CLIPPING_OVERFLOW = new Set(["auto", "clip", "hidden", "scroll"]);

function tabId(): string {
  return `tab-${globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random()}`}`.replace(/[^a-zA-Z0-9_-]/g, "-");
}

function blankTab(): BrowserTab {
  return { id: tabId(), address: "", title: "New tab", loading: false, created: false };
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function utf8Base64(value: string): string {
  const bytes = new TextEncoder().encode(value);
  let binary = "";
  for (let offset = 0; offset < bytes.length; offset += 0x8000) {
    binary += String.fromCharCode(...bytes.subarray(offset, offset + 0x8000));
  }
  return btoa(binary);
}

function snapInsideStart(value: number): number {
  const scale = window.devicePixelRatio || 1;
  return Math.ceil(value * scale) / scale;
}

function snapInsideEnd(value: number): number {
  const scale = window.devicePixelRatio || 1;
  return Math.floor(value * scale) / scale;
}

function visibleSurfaceRect(element: HTMLElement): DOMRect {
  const rect = element.getBoundingClientRect();
  let top = Math.max(0, rect.top);
  let right = Math.min(window.innerWidth, rect.right);
  let bottom = Math.min(window.innerHeight, rect.bottom);
  let left = Math.max(0, rect.left);
  for (let ancestor = element.parentElement; ancestor; ancestor = ancestor.parentElement) {
    const style = getComputedStyle(ancestor);
    const ancestorRect = ancestor.getBoundingClientRect();
    if (CLIPPING_OVERFLOW.has(style.overflowX)) {
      left = Math.max(left, ancestorRect.left);
      right = Math.min(right, ancestorRect.right);
    }
    if (CLIPPING_OVERFLOW.has(style.overflowY)) {
      top = Math.max(top, ancestorRect.top);
      bottom = Math.min(bottom, ancestorRect.bottom);
    }
  }
  return new DOMRect(left, top, Math.max(0, right - left), Math.max(0, bottom - top));
}

export function WorkbenchBrowser({ active, api, operatorId = "operator", projectId, scope, scopeLoading = false, onAddKnowledgeUrl, onAskNebula, onOpenFiles, onScopeUpdated, onUploadEvidence }: WorkbenchBrowserProps) {
  const confirm = useConfirmation();
  const dialogOpen = useDialogOpen();
  const { activityOpen, paletteOpen, sidebarCollapsed } = useChrome();
  const desktop = isTauriRuntime();
  const [deviceId, setDeviceId] = useState<string | undefined>(desktop ? undefined : "paired-browser");
  const [tabs, setTabs] = useState<BrowserTab[]>(() => [blankTab()]);
  const [activeId, setActiveId] = useState(() => tabs[0].id);
  const [capabilities, setCapabilities] = useState<BrowserCapabilities>();
  const [notice, setNotice] = useState<BrowserNotice>();
  const [error, setError] = useState<string>();
  useEffect(() => { if (desktop) void desktopDeviceId().then(setDeviceId).catch((caught) => {
    void logCaughtDiagnostic("interface.security_browser.device_identity_unavailable", "The desktop browser identity could not be loaded.", caught, "workbench_browser");
    setError("This desktop's Browser identity is unavailable. Retry after restoring access to the protected app configuration.");
  }); }, [desktop]);
  const [addingKnowledge, setAddingKnowledge] = useState(false);
  const addingScopeRef = useRef(false);
  const [capturingContext, setCapturingContext] = useState(false);
  const [workspace, setWorkspace] = useState<SecurityBrowserWorkspace>();
  const [workspaceLoading, setWorkspaceLoading] = useState(true);
  const [sessionHydrated, setSessionHydrated] = useState(false);
  const [workspaceError, setWorkspaceError] = useState<string>();
  const [sessionId, setSessionId] = useState<string>();
  const [researchOpen, setResearchOpen] = useState(false);
  const [researchView, setResearchView] = useState<"traffic" | "actions" | "identities" | "session">("traffic");
  const [selectedExchangeIds, setSelectedExchangeIds] = useState<string[]>([]);
  const [identityName, setIdentityName] = useState("");
  const [identityBusy, setIdentityBusy] = useState(false);
  const [replayExchange, setReplayExchange] = useState<SecurityBrowserExchange>();
  const [replayMethod, setReplayMethod] = useState("GET");
  const [replayUrl, setReplayUrl] = useState("");
  const [replayHeaders, setReplayHeaders] = useState("{}");
  const [replayBody, setReplayBody] = useState("");
  const [replayBusy, setReplayBusy] = useState(false);
  const [upstreamProxyEnabledDraft, setUpstreamProxyEnabledDraft] = useState(false);
  const [upstreamProxyUrlDraft, setUpstreamProxyUrlDraft] = useState("");
  const [upstreamProxyCredentialRefDraft, setUpstreamProxyCredentialRefDraft] = useState("");
  const [caStatus, setCaStatus] = useState<import("../api/workbenchBrowser").BrowserCaStatus>();
  const [caTrustAcknowledged, setCaTrustAcknowledged] = useState(false);
  const [automationStatus, setAutomationStatus] = useState<SecurityBrowserAutomationStatus>();
  const [automationFormOpen, setAutomationFormOpen] = useState(false);
  const [automationBusy, setAutomationBusy] = useState(false);
  const [automationProviders, setAutomationProviders] = useState<ProviderHealth[]>([]);
  const [automationProviderId, setAutomationProviderId] = useState("");
  const [automationModel, setAutomationModel] = useState("");
  const [automationTarget, setAutomationTarget] = useState("");
  const [automationRisks, setAutomationRisks] = useState<string[]>(["passive", "active_scan", "credential_use"]);
  const [automationCredentialRefs, setAutomationCredentialRefs] = useState("");
  const [automationDuration, setAutomationDuration] = useState(30);
  const [automationMaxCommands, setAutomationMaxCommands] = useState(100);
  const [automationMaxRequests, setAutomationMaxRequests] = useState(1000);
  const toolbarRef = useRef<HTMLDivElement>(null);
  const surfaceRef = useRef<HTMLDivElement>(null);
  const tabsRef = useRef(tabs);
  const activeRef = useRef(activeId);
  const captureRef = useRef<{ requestId: string; tabId: string; purpose: "assistant" | "evidence" } | undefined>(undefined);
  const hydratedProjectRef = useRef<string | undefined>(undefined);
  const websocketExchangeRef = useRef(new Map<string, Promise<SecurityBrowserExchange>>());
  const actionExecutionRef = useRef(new Map<string, import("../api/types").SecurityBrowserAction>());
  const addressDraftRef = useRef(new Map<string, string>());
  const scopeRef = useRef(scope);
  tabsRef.current = tabs;
  activeRef.current = activeId;
  scopeRef.current = scope;

  const activeSession = workspace?.sessions.find((session) => session.id === sessionId)
    ?? workspace?.sessions.find((session) => session.status === "active")
    ?? workspace?.sessions[0];
  const activeIdentity = workspace?.identities.find((identity) => identity.id === activeSession?.identityId);

  useEffect(() => {
    if (!activeSession) return;
    setUpstreamProxyEnabledDraft(activeSession.upstreamProxyEnabled);
    setUpstreamProxyUrlDraft(activeSession.upstreamProxyUrl ?? "");
    setUpstreamProxyCredentialRefDraft(activeSession.upstreamProxyCredentialRef ?? "");
  }, [activeSession?.id]);

  const activeTab = tabs.find((tab) => tab.id === activeId) ?? tabs[0];
  const automationTargetOptions = useMemo(() => Array.from(new Set([
    ...(activeTab?.url && evaluateBrowserScope(activeTab.url, scope).state === "in_scope" ? [activeTab.url] : []),
    ...(scope?.allowedUrls ?? []),
    ...(scope?.allowedDomains ?? []).map((domain) => `https://${domain}/`),
  ].filter(Boolean))), [activeTab?.url, scope]);
  const activeAutomationLease = automationStatus?.leases.find((lease) => lease.sessionId === activeSession?.id && lease.status === "active");
  const scopeDecision = scopeLoading
    ? { state: "unknown" as const, label: "Checking scope", detail: "Loading the durable Project scope." }
    : evaluateBrowserScope(activeTab?.url, scope);
  const browserVisible = desktop && active && !activityOpen && !paletteOpen && !dialogOpen
    && (sidebarCollapsed || !window.matchMedia("(max-width: 760px)").matches);

  const bounds = useCallback((): BrowserBounds | undefined => {
    const surface = surfaceRef.current;
    if (!surface) return undefined;
    const rect = visibleSurfaceRect(surface);
    // Native webviews and the DOM can round fractional high-DPI coordinates in opposite
    // directions. Keep every native edge inside the CSS surface so the page can never bleed
    // upward over the address bar (or outside another clipped ancestor) by a device pixel.
    const x = snapInsideStart(rect.left);
    const toolbarBottom = toolbarRef.current?.getBoundingClientRect().bottom ?? rect.top;
    const y = snapInsideStart(Math.max(0, rect.top, toolbarBottom));
    const right = snapInsideEnd(rect.right);
    const bottom = snapInsideEnd(rect.bottom);
    if (right - x < 1 || bottom - y < 1) return undefined;
    return { x, y, width: right - x, height: bottom - y };
  }, []);

  const updateTab = useCallback((id: string, change: Partial<BrowserTab>) => {
    setTabs((current) => {
      const next = current.map((tab) => tab.id === id ? { ...tab, ...change } : tab);
      tabsRef.current = next;
      return next;
    });
  }, []);

  const addPageToScope = useCallback(async (request: BrowserScopeRequestEvent) => {
    if (request.projectId !== projectId) return;
    if (request.state !== "ready") {
      setError(request.detail ?? "The page changed. Right-click the current page and try Add to scope again.");
      return;
    }
    const tab = tabsRef.current.find((item) => item.id === request.tabId);
    if (!tab?.created || !tab.url || new URL(tab.url).href !== new URL(request.url).href) {
      setError("The page changed after the context menu opened. Right-click the current page and try again.");
      return;
    }
    const currentScope = scopeRef.current;
    if (!currentScope) {
      setError("Project scope is unavailable. Reconnect to Core, then right-click the page and try again.");
      return;
    }
    if (currentScope.allowAllTargets || evaluateBrowserScope(request.url, currentScope).state === "in_scope") {
      setNotice({ kind: "info", message: `${new URL(request.url).origin} is already in Project scope.` });
      setError(undefined);
      return;
    }
    const addition = buildBrowserScopeAddition(request.url, currentScope);
    if (!addition.changed) {
      setError("This target is already listed, but the Project scope is inactive. Review its time window in Settings.");
      return;
    }
    const approved = await confirm({
      title: `Add ${addition.origin} to scope?`,
      message: <>
        Nebula will add this exact HTTP(S) origin to URL-only browser scope revision {currentScope.revision}. This authorizes every path on the same scheme, host, and port, but does not by itself authorize shell networking.
        {addition.addsPort && <> Because the current allowlist excludes port <code>{addition.port}</code>, this also adds that port for every currently scoped target.</>}
      </>,
      confirmLabel: "Add to scope",
      tone: "danger",
    });
    if (!approved || addingScopeRef.current) return;
    addingScopeRef.current = true;
    setError(undefined);
    setNotice(undefined);
    try {
      const latestTab = tabsRef.current.find((item) => item.id === request.tabId);
      if (!latestTab?.url || new URL(latestTab.url).href !== new URL(request.url).href) {
        throw new Error("The page changed before scope was saved. Right-click the current page and try again.");
      }
      const updated = await api.updateEngagementScope(projectId, {
        ...addition.update,
        expectedRevision: currentScope.revision,
      });
      scopeRef.current = updated;
      onScopeUpdated(updated);
      setNotice({ kind: "info", message: `${addition.origin} was added to Project scope revision ${updated.revision}.` });
    } catch (caught) {
      void logCaughtDiagnostic("interface.workbench_browser.scope_add_failed", "A browser origin could not be added to Project scope.", caught, "workbench_browser");
      try {
        const latest = await api.getEngagementScope(projectId);
        if (latest.revision !== currentScope.revision) {
          scopeRef.current = latest;
          onScopeUpdated(latest);
          setError("Project scope changed before this addition could be saved. The latest revision is loaded; right-click the page and review Add to scope again.");
        } else {
          setError(`${errorMessage(caught)} Project scope was not changed; right-click the page to retry.`);
        }
      } catch (refreshCaught) {
        void logCaughtDiagnostic("interface.workbench_browser.scope_refresh_failed", "Project scope could not be refreshed after an Add to scope conflict.", refreshCaught, "workbench_browser");
        setError(`${errorMessage(caught)} Project scope was not changed. Reconnect to Core, then right-click the page to retry.`);
      }
    } finally {
      addingScopeRef.current = false;
    }
  }, [api, confirm, onScopeUpdated, projectId]);

  const openAddress = useCallback(async (id: string, input: string) => {
    setError(undefined);
    let url: string;
    try { url = normalizeBrowserInput(input); }
    catch (caught) {
      // diagnostic-expected: local operator input validation is presented inline.
      setError(errorMessage(caught)); return false;
    }
    const navigationScope = evaluateBrowserScope(url, scope);
    if (navigationScope.state !== "in_scope") {
      setError(`Nebula blocked this navigation because the target is not confirmed in Project scope. ${navigationScope.detail}`);
      return false;
    }
    const tab = tabsRef.current.find((item) => item.id === id);
    const nextBounds = bounds();
    if (!tab) {
      setError("The selected browser tab changed before navigation. Select the tab and try again.");
      return false;
    }
    if (!nextBounds) {
      setError("The embedded browser surface is not visible yet. Restore the Browser panel and try again.");
      return false;
    }
    updateTab(id, { address: url, url, loading: true, error: undefined });
    try {
      if (!activeSession) throw new Error("The durable browser session is unavailable.");
      const durableTabs = tabsRef.current.map((item, position) => {
        const nextUrl = item.id === id ? url : item.url;
        const decision = evaluateBrowserScope(nextUrl, scope);
        return {
          id: item.id,
          url: nextUrl,
          title: item.title,
          position,
          lastScopeState: decision.state,
          lastScopeRevision: decision.revision,
        };
      });
      if (!deviceId) throw new Error("The stable desktop Browser identity is still loading.");
      const persistedSession = await api.syncSecurityBrowserSession(activeSession, durableTabs, id, deviceId);
      setWorkspace((current) => current ? {
        ...current,
        sessions: current.sessions.map((session) => session.id === persistedSession.id ? persistedSession : session),
      } : current);
      if (tab.created) await workbenchBrowser.navigate(id, projectId, url);
      else {
        if (!activeIdentity) throw new Error("Select a healthy browser identity before opening a page.");
        await workbenchBrowser.create(
          id,
          projectId,
          activeIdentity.storagePartition,
          persistedSession.id,
          persistedSession.proxyEnabled,
          url,
          nextBounds,
          {
            enabled: persistedSession.upstreamProxyEnabled,
            url: persistedSession.upstreamProxyUrl,
            credentialRef: persistedSession.upstreamProxyCredentialRef,
          },
          persistedSession.captureMode === "bodies",
        );
        updateTab(id, { created: true });
      }
      return true;
    } catch (caught) {
      void logCaughtDiagnostic("interface.workbench_browser.navigation_failed", "The embedded browser could not navigate.", caught, "workbench_browser");
      updateTab(id, { loading: false, error: errorMessage(caught) });
      return false;
    }
  }, [activeIdentity, activeSession, api, bounds, deviceId, projectId, scope, updateTab]);

  const addTab = useCallback((url?: string) => {
    if (tabsRef.current.length >= MAX_TABS) {
      setError(`A Project may have at most ${MAX_TABS} browser tabs.`);
      return;
    }
    const tab = blankTab();
    if (url) { tab.address = url; tab.url = url; }
    setTabs((current) => [...current, tab]);
    setActiveId(tab.id);
    if (url) requestAnimationFrame(() => void openAddress(tab.id, url));
  }, [openAddress]);

  const closeTab = useCallback(async (id: string) => {
    const tab = tabsRef.current.find((item) => item.id === id);
    if (tab?.created) {
      try { await workbenchBrowser.close(id, projectId); }
      catch (caught) { void logCaughtDiagnostic("interface.workbench_browser.close_failed", "An embedded browser tab could not close.", caught, "workbench_browser"); }
    }
    if (captureRef.current?.tabId === id) {
      captureRef.current = undefined;
      setCapturingContext(false);
    }
    setTabs((current) => {
      const index = current.findIndex((item) => item.id === id);
      const remaining = current.filter((item) => item.id !== id);
      const next = remaining.length ? remaining : [blankTab()];
      if (activeRef.current === id) setActiveId(next[Math.min(index, next.length - 1)].id);
      return next;
    });
  }, [projectId]);

  useEffect(() => {
    if (!desktop) return;
    void workbenchBrowser.capabilities().then(setCapabilities).catch((caught) => {
      void logCaughtDiagnostic("interface.workbench_browser.capabilities_failed", "Browser capabilities could not be read.", caught, "workbench_browser");
      setError(errorMessage(caught));
    });
  }, [desktop]);

  useEffect(() => {
    if (!desktop || !capabilities?.interceptionProxy || typeof workbenchBrowser.proxyCaStatus !== "function") return;
    let disposed = false;
    void workbenchBrowser.proxyCaStatus(projectId).then((status) => {
      if (!disposed) setCaStatus(status);
    }).catch((caught) => {
      void logCaughtDiagnostic("interface.security_browser.proxy_ca_status_failed", "The Project browser CA status could not be read.", caught, "workbench_browser");
    });
    return () => { disposed = true; };
  }, [capabilities?.interceptionProxy, desktop, projectId]);

  const refreshWorkspace = useCallback(async () => {
    setWorkspaceError(undefined);
    try {
      const next = await api.getSecurityBrowserWorkspace(projectId);
      setWorkspace(next);
      setSessionId((current) => next.sessions.some((session) => session.id === current)
        ? current
        : next.sessions.find((session) => session.status === "active")?.id ?? next.sessions[0]?.id);
      return next;
    } catch (caught) {
      void logCaughtDiagnostic("interface.security_browser.workspace_load_failed", "The durable browser research workspace could not be loaded.", caught, "workbench_browser");
      setWorkspaceError(errorMessage(caught));
      return undefined;
    } finally {
      setWorkspaceLoading(false);
    }
  }, [api, projectId]);

  const refreshAutomationStatus = useCallback(async () => {
    if (typeof api.getSecurityBrowserAutomation !== "function") return undefined;
    try {
      const next = await api.getSecurityBrowserAutomation(projectId);
      setAutomationStatus(next);
      return next;
    } catch (caught) {
      void logCaughtDiagnostic("interface.security_browser.automation_status_failed", "Autonomous browser status could not be refreshed.", caught, "workbench_browser");
      return undefined;
    }
  }, [api, projectId]);

  const startAutonomousRun = async (event: FormEvent) => {
    event.preventDefault();
    if (automationBusy || !activeSession || !activeIdentity || !activeTab?.created) return;
    if (!deviceId || activeSession.deviceOwner !== deviceId) {
      setWorkspaceError("This desktop browser is not paired to the selected session yet. Keep the session open, then retry after its device owner is saved.");
      return;
    }
    const target = automationTarget.trim() || activeTab.url || "";
    const targetDecision = evaluateBrowserScope(target, scope);
    if (targetDecision.state !== "in_scope") {
      setWorkspaceError(`The autonomous run was not started because its target is not in Project scope. ${targetDecision.detail}`);
      return;
    }
    const provider = automationProviders.find((item) => item.id === automationProviderId);
    const model = automationModel.trim() || provider?.effectiveDefaultModel || provider?.defaultModel || provider?.models[0] || "";
    if (!provider || !model) {
      setWorkspaceError("Choose a healthy, enabled model provider and model before starting the autonomous web test.");
      return;
    }
    const credentialRefs = automationCredentialRefs.split(",").map((value) => value.trim()).filter(Boolean);
    if (automationRisks.includes("credential_use") && !credentialRefs.length) {
      setWorkspaceError("Credential use is selected, but no credential reference was supplied. Enter an existing vault reference; never enter the secret itself.");
      return;
    }
    setAutomationBusy(true);
    setWorkspaceError(undefined);
    try {
      const run = await api.createMission({
        engagementId: projectId,
        name: "Autonomous browser assessment",
        objective: `Run a bounded web application assessment for ${target}`,
        backend: "native",
        providerId: provider.id,
        model,
        maxDurationSeconds: automationDuration * 60,
        maxToolCalls: automationMaxCommands,
        maxConcurrency: 1,
        browserAutonomy: {
          sessionId: activeSession.id,
          targets: [target],
          allowedRiskClasses: automationRisks,
          credentialRefs,
          durationSeconds: automationDuration * 60,
          maxCommands: automationMaxCommands,
          maxRequests: automationMaxRequests,
          maxBodyBytes: 1_048_576,
        },
      });
      setAutomationFormOpen(false);
      setNotice({ kind: "info", message: `Autonomous browser test ${run.id.slice(0, 12)} started. It remains active if this Browser panel is closed.` });
      await refreshAutomationStatus();
    } catch (caught) {
      void logCaughtDiagnostic("interface.security_browser.automation_start_failed", "The autonomous browser test could not be started.", caught, "workbench_browser");
      setWorkspaceError(`${errorMessage(caught)} No browser lease was started.`);
    } finally {
      setAutomationBusy(false);
    }
  };

  useEffect(() => {
    if (!automationTarget && automationTargetOptions[0]) setAutomationTarget(automationTargetOptions[0]);
  }, [automationTarget, automationTargetOptions]);

  useEffect(() => {
    if (!desktop || !researchOpen || typeof api.listProviders !== "function") return;
    let disposed = false;
    void api.listProviders().then((page) => {
      if (disposed) return;
      const available = page.items.filter((provider) => provider.enabled && provider.state !== "offline" && provider.state !== "unconfigured");
      setAutomationProviders(available);
      setAutomationProviderId((current) => available.some((provider) => provider.id === current) ? current : available[0]?.id ?? "");
    }).catch((caught) => {
      void logCaughtDiagnostic("interface.security_browser.automation_provider_load_failed", "Model providers could not be loaded for the autonomous browser form.", caught, "workbench_browser");
    });
    return () => { disposed = true; };
  }, [api, desktop, researchOpen]);

  useEffect(() => {
    const provider = automationProviders.find((item) => item.id === automationProviderId);
    if (!provider) return;
    const defaultModel = provider.effectiveDefaultModel || provider.defaultModel || provider.models[0] || "";
    if (!automationModel || !provider.models.includes(automationModel)) setAutomationModel(defaultModel);
  }, [automationModel, automationProviderId, automationProviders]);

  useEffect(() => {
    setWorkspace(undefined);
    setWorkspaceLoading(true);
    setSessionHydrated(false);
    setWorkspaceError(undefined);
    setSessionId(undefined);
    hydratedProjectRef.current = undefined;
    void refreshWorkspace();
  }, [refreshWorkspace]);

  useEffect(() => {
    if (!desktop || !active || !activeSession || !deviceId) return;
    let disposed = false;
    let handling = false;
    const process = async () => {
      if (handling || disposed) return;
      handling = true;
      try {
        const next = await api.getSecurityBrowserWorkspace(projectId);
        if (disposed) return;
        const queued = next.handoffs.find((handoff) => handoff.sessionId === activeSession.id && handoff.status === "queued");
        if (!queued) return;
        setWorkspace(next);
        const claimed = await api.claimSecurityBrowserHandoff(queued, deviceId);
        try {
          if (claimed.command === "focus_tab") {
            const target = tabsRef.current.find((tab) => tab.id === claimed.tabId);
            if (!target) throw new Error("The requested durable tab is not available on this desktop.");
            setActiveId(target.id);
          } else {
            if (!claimed.url) throw new Error("The navigation handoff has no URL.");
            let target = tabsRef.current.find((tab) => tab.id === claimed.tabId);
            if (!target) {
              target = blankTab();
              setTabs((current) => [...current, target!].slice(0, MAX_TABS));
            }
            setActiveId(target.id);
            if (!await openAddress(target.id, claimed.url)) throw new Error("The desktop browser could not complete the queued navigation.");
          }
          await api.finishSecurityBrowserHandoff(claimed, "complete", undefined, deviceId);
          setNotice({ kind: "info", message: "Paired-device browser handoff completed on this desktop." });
        } catch (caught) {
          void logCaughtDiagnostic("interface.security_browser.handoff_execution_failed", "A claimed browser handoff could not be completed.", caught, "workbench_browser");
          await api.finishSecurityBrowserHandoff(claimed, "failed", errorMessage(caught), deviceId).catch((receiptCaught) => {
            void logCaughtDiagnostic("interface.security_browser.handoff_receipt_failed", "A failed browser handoff could not be recorded.", receiptCaught, "workbench_browser");
          });
          setWorkspaceError(errorMessage(caught));
        }
        await refreshWorkspace();
      } catch (caught) {
        void logCaughtDiagnostic("interface.security_browser.handoff_poll_failed", "Browser handoffs could not be synchronized.", caught, "workbench_browser");
      } finally {
        handling = false;
      }
    };
    void process();
    const timer = window.setInterval(() => void process(), 3000);
    return () => { disposed = true; window.clearInterval(timer); };
  }, [active, activeSession, api, desktop, deviceId, openAddress, projectId, refreshWorkspace]);

  useEffect(() => {
    if (!activeSession || hydratedProjectRef.current === `${projectId}:${activeSession.id}`) return;
    hydratedProjectRef.current = `${projectId}:${activeSession.id}`;
    const restored = activeSession.tabs.length
      ? activeSession.tabs.map((tab) => ({
          id: tab.id,
          address: tab.url ?? "",
          url: tab.url,
          title: tab.title,
          loading: false,
          created: false,
        }))
      : [blankTab()];
    setTabs(restored);
    setActiveId(activeSession.activeTabId && restored.some((tab) => tab.id === activeSession.activeTabId)
      ? activeSession.activeTabId
      : restored[0].id);
    setSessionHydrated(true);
  }, [activeSession, projectId]);

  useEffect(() => {
    const next = blankTab();
    setTabs([next]);
    setActiveId(next.id);
    setNotice(undefined);
    setError(undefined);
    captureRef.current = undefined;
    setCapturingContext(false);
  }, [projectId]);

  useEffect(() => {
    if (!desktop) return;
    let disposed = false;
    const stops: Array<() => void> = [];
    void Promise.all([
      listen<BrowserPageEvent>("nebula-browser-page", ({ payload }) => {
        if (payload.state === "new_tab") { addTab(payload.url); return; }
        if (payload.state === "blocked") { setError(payload.detail ?? "The navigation was blocked."); return; }
        if (payload.state === "title") { if (payload.title) updateTab(payload.tabId, { title: payload.title }); return; }
        updateTab(payload.tabId, { url: payload.url, address: payload.url, loading: payload.state === "loading", error: undefined });
      }),
      listen<BrowserScopeRequestEvent>("nebula-browser-scope-request", ({ payload }) => {
        void addPageToScope(payload);
      }),
      listen<BrowserDownloadEvent>("nebula-browser-download", ({ payload }) => {
        if (payload.state !== "ready" || !payload.downloadId || !payload.filename) {
          setError(payload.detail ?? "The website download failed.");
          if (payload.state === "rejected") updateTab(payload.tabId, { created: false, loading: false });
          return;
        }
        void (async () => {
          try {
            let result = await workbenchBrowser.importDownload(payload.downloadId!, projectId, false);
            if (result.state === "conflict") {
              const replace = await confirm({ title: `Replace ${payload.filename}?`, message: <>A file with this name already exists in Project Files. Replace it with the website download?</>, confirmLabel: "Replace file", tone: "danger" });
              if (!replace) { await workbenchBrowser.discardDownload(payload.downloadId!, projectId); setNotice({ kind: "info", message: `${payload.filename} was discarded.` }); return; }
              result = await workbenchBrowser.importDownload(payload.downloadId!, projectId, true);
            }
            setNotice({ kind: "download", message: `${result.overwritten ? "Replaced" : "Downloaded"} ${result.path} in Project Files.` });
          } catch (caught) {
            void logCaughtDiagnostic("interface.workbench_browser.download_import_failed", "A website download could not be imported into Project Files.", caught, "workbench_browser");
            setError(errorMessage(caught));
          }
        })();
      }),
      listen<BrowserContextEvent>("nebula-browser-context", ({ payload }) => {
        const pending = captureRef.current;
        if (!pending || pending.requestId !== payload.requestId || pending.tabId !== payload.tabId) return;
        captureRef.current = undefined;
        setCapturingContext(false);
        if (payload.state !== "ready" || !payload.context) {
          setError(payload.detail ?? "The live page context could not be captured. Reload the page and try again.");
          return;
        }
        const decision = evaluateBrowserScope(payload.context.url, scope);
        if (decision.state !== "in_scope") {
          setError(`Nebula did not prepare this capture because the final page is not confirmed in scope. ${decision.detail}`);
          return;
        }
        const captured = formatBrowserContextForAssistant(payload.context, decision);
        let hostname = "page";
        try { hostname = new URL(payload.context.url).hostname; }
        catch {
          // diagnostic-expected: the desktop boundary already validates capture provenance; retain the safe fallback label.
        }
        if (pending.purpose === "assistant") {
          onAskNebula({
            text: captured.text,
            sourceKind: "browser_page",
            sourceId: activeSession?.id,
            sourceLabel: `Browser · ${payload.context.title || hostname}`.slice(0, 500),
            truncated: captured.truncated,
          });
        } else if (onUploadEvidence && activeSession && activeIdentity) {
          const capturedAt = new Date().toISOString();
          void onUploadEvidence({
            engagementId: projectId,
            filename: `browser-page-${hostname.replace(/[^a-z0-9.-]/gi, "-")}-${capturedAt.replace(/[:.]/g, "-")}.txt`,
            title: `Browser page · ${payload.context.title || hostname}`,
            evidenceType: "browser-page-capture",
            contentBase64: utf8Base64(captured.text),
            mediaType: "text/plain; charset=utf-8",
            description: "Immutable, scope-checked semantic capture of the live authenticated browser page. Form values and cookies are excluded.",
            source: "security-browser",
            capturedBy: operatorId,
            sourceVersion: "browser-semantic-context-v1",
            sourceContext: {
              project_id: projectId,
              browser_session_id: activeSession.id,
              browser_tab_id: pending.tabId,
              browser_identity_id: activeIdentity.id,
              url: payload.context.url,
              scope_revision: decision.revision,
              captured_at: capturedAt,
              truncated: captured.truncated,
            },
            metadata: { browser_session_id: activeSession.id, browser_identity_id: activeIdentity.id, url: payload.context.url },
          }).then((evidence) => {
            setNotice({ kind: "info", message: `${evidence.title} was preserved as immutable Evidence.` });
          }).catch((caught) => {
            void logCaughtDiagnostic("interface.security_browser.evidence_failed", "The browser page capture could not be preserved.", caught, "workbench_browser");
            setError(errorMessage(caught));
          });
        }
      }),
      listen<BrowserTrafficEvent>("nebula-browser-traffic", ({ payload }) => {
        if (payload.blocked) {
          setWorkspaceError(`Native proxy blocked ${payload.url}. ${payload.error ?? "Review Project scope and active proxy rules."}`);
        }
        const save = async () => {
          const artifactIds: { requestBodyArtifactId?: string; responseBodyArtifactId?: string } = {};
          for (const [direction, body] of [["request", payload.requestBody], ["response", payload.responseBody]] as const) {
            if (!body) continue;
            try {
              const artifact = await api.uploadSecurityBrowserBodyArtifact(payload.sessionId, {
                direction,
                contentBase64: body.base64,
                mediaType: body.mediaType,
                truncated: body.truncated,
              });
              if (direction === "request") artifactIds.requestBodyArtifactId = artifact.id;
              else artifactIds.responseBodyArtifactId = artifact.id;
            } catch (caught) {
              void logCaughtDiagnostic("interface.security_browser.body_artifact_failed", "A bounded browser body could not be redacted and stored as an artifact.", caught, "workbench_browser");
              setWorkspaceError(`${errorMessage(caught)} The traffic metadata was retained without the unsafe body.`);
            }
          }
          const exchange = await api.recordSecurityBrowserTraffic(payload.sessionId, {
            tabId: payload.tabId,
            method: payload.method,
            url: payload.url,
            protocol: payload.protocol,
            statusCode: payload.statusCode,
            requestHeaders: payload.requestHeaders,
            responseHeaders: payload.responseHeaders,
            ...artifactIds,
            requestBytes: payload.requestBytes,
            responseBytes: payload.responseBytes,
            durationMs: payload.durationMs,
            error: payload.error,
            blocked: payload.blocked,
            truncated: Boolean(payload.requestBody?.truncated || payload.responseBody?.truncated),
          });
          setWorkspace((current) => current ? {
            ...current,
            traffic: current.traffic.some((item) => item.id === exchange.id)
              ? current.traffic
              : [...current.traffic, exchange],
          } : current);
        };
        void save().catch((caught) => {
          void logCaughtDiagnostic("interface.security_browser.traffic_persist_failed", "Captured browser traffic could not be persisted.", caught, "workbench_browser");
          setWorkspaceError(`${errorMessage(caught)} Traffic remained local to the native capture event and was not added to the durable timeline.`);
        });
      }),
      listen<BrowserWebSocketFrameEvent>("nebula-browser-websocket-frame", ({ payload }) => {
        const key = `${payload.sessionId}:${payload.tabId}:${payload.url}`;
        let exchangeRequest = websocketExchangeRef.current.get(key);
        if (!exchangeRequest) {
          exchangeRequest = api.recordSecurityBrowserTraffic(payload.sessionId, {
            tabId: payload.tabId,
            method: "GET",
            url: payload.url.replace(/^ws:/, "http:").replace(/^wss:/, "https:"),
            protocol: "websocket",
            statusCode: 101,
            requestHeaders: {},
            responseHeaders: {},
          });
          websocketExchangeRef.current.set(key, exchangeRequest);
        }
        void exchangeRequest.then((exchange) => api.recordSecurityBrowserWebSocketFrame(payload.sessionId, {
          exchangeId: exchange.id,
          direction: payload.direction,
          opcode: payload.opcode,
          payloadPreview: payload.payloadPreview,
          payloadSha256: payload.payloadSha256,
          payloadBytes: payload.payloadBytes,
          truncated: payload.truncated,
        }).then((frame) => setWorkspace((current) => current ? {
          ...current,
          traffic: current.traffic.some((item) => item.id === exchange.id) ? current.traffic : [...current.traffic, exchange],
          frames: current.frames.some((item) => item.id === frame.id) ? current.frames : [...current.frames, frame],
        } : current))).catch((caught) => {
          websocketExchangeRef.current.delete(key);
          void logCaughtDiagnostic("interface.security_browser.websocket_persist_failed", "A WebSocket frame could not be persisted.", caught, "workbench_browser");
          setWorkspaceError(`${errorMessage(caught)} The frame was not added to the durable timeline.`);
        });
      }),
      listen<BrowserActionEvent>("nebula-browser-action", ({ payload }) => {
        const executing = actionExecutionRef.current.get(payload.actionId);
        if (!executing) return;
        actionExecutionRef.current.delete(payload.actionId);
        void api.finishSecurityBrowserAction(executing, {
          state: payload.state,
          result: payload.result,
          error: payload.detail,
        }).then((updated) => setWorkspace((current) => current ? {
          ...current,
          actions: current.actions.map((action) => action.id === updated.id ? updated : action),
        } : current)).catch((caught) => {
          void logCaughtDiagnostic("interface.security_browser.action_receipt_failed", "A browser action receipt could not be persisted.", caught, "workbench_browser");
          setWorkspaceError(`${errorMessage(caught)} The native action result requires recovery before another action runs.`);
        });
      }),
    ]).then((unlisteners) => { if (disposed) unlisteners.forEach((stop) => stop()); else stops.push(...unlisteners); });
    return () => { disposed = true; stops.forEach((stop) => stop()); };
  }, [activeIdentity, activeSession, addPageToScope, addTab, api, confirm, desktop, onAskNebula, operatorId, projectId, scope, updateTab]);

  useEffect(() => {
    if (typeof api.getSecurityBrowserAutomation !== "function") return;
    let disposed = false;
    const refresh = () => {
      void refreshAutomationStatus().catch((caught) => {
        void logCaughtDiagnostic("interface.security_browser.automation_refresh_failed", "Autonomous browser status could not be refreshed.", caught, "workbench_browser");
      });
    };
    refresh();
    const timer = window.setInterval(() => {
      if (!disposed) refresh();
    }, 3000);
    return () => { disposed = true; window.clearInterval(timer); };
  }, [api, projectId, refreshAutomationStatus]);

  useEffect(() => {
    if (!desktop) return;
    for (const tab of tabs) {
      if (!tab.created) continue;
      void workbenchBrowser.visible(tab.id, projectId, browserVisible && tab.id === activeId).catch((caught) => {
        void logCaughtDiagnostic("interface.workbench_browser.visibility_failed", "An embedded browser tab could not change visibility.", caught, "workbench_browser");
      });
    }
  }, [activeId, browserVisible, desktop, projectId, tabs]);

  useEffect(() => {
    if (!activeSession || !deviceId || hydratedProjectRef.current !== `${projectId}:${activeSession.id}`) return;
    const durableTabs = tabs.map((tab, position) => {
      const decision = evaluateBrowserScope(tab.url, scope);
      return {
        id: tab.id,
        url: tab.url,
        title: tab.title,
        position,
        lastScopeState: decision.state,
        lastScopeRevision: decision.revision,
      };
    });
    const previous = JSON.stringify({ tabs: activeSession.tabs, active: activeSession.activeTabId });
    const next = JSON.stringify({ tabs: durableTabs, active: activeId });
    if (previous === next) return;
    const timer = window.setTimeout(() => {
      void api.syncSecurityBrowserSession(activeSession, durableTabs, activeId, deviceId)
        .then((updated) => setWorkspace((current) => current ? {
          ...current,
          sessions: current.sessions.map((session) => session.id === updated.id ? updated : session),
        } : current))
        .catch((caught) => {
          void logCaughtDiagnostic("interface.security_browser.session_sync_failed", "Browser tabs could not be persisted.", caught, "workbench_browser");
          setWorkspaceError(`${errorMessage(caught)} Refresh the research session and retry.`);
        });
    }, 350);
    return () => window.clearTimeout(timer);
  }, [activeId, activeSession, api, deviceId, projectId, scope, tabs]);

  useEffect(() => {
    if (!desktop || !browserVisible || !activeTab?.created) return;
    let frame = 0;
    const sync = () => {
      cancelAnimationFrame(frame);
      frame = requestAnimationFrame(() => {
        const next = bounds();
        if (next) void workbenchBrowser.bounds(activeTab.id, projectId, next).catch((caught) => void logCaughtDiagnostic("interface.workbench_browser.bounds_failed", "The embedded browser surface could not be resized.", caught, "workbench_browser"));
      });
    };
    const observer = new ResizeObserver(sync);
    if (surfaceRef.current) observer.observe(surfaceRef.current);
    if (toolbarRef.current) observer.observe(toolbarRef.current);
    const layoutRoot = surfaceRef.current?.parentElement;
    const mutationObserver = layoutRoot ? new MutationObserver(sync) : undefined;
    if (layoutRoot) {
      for (let ancestor: HTMLElement | null = layoutRoot; ancestor; ancestor = ancestor.parentElement) observer.observe(ancestor);
      mutationObserver?.observe(layoutRoot, { childList: true });
    }
    window.addEventListener("resize", sync);
    window.addEventListener("scroll", sync, true);
    sync();
    return () => { observer.disconnect(); mutationObserver?.disconnect(); window.removeEventListener("resize", sync); window.removeEventListener("scroll", sync, true); cancelAnimationFrame(frame); };
  }, [activeTab?.created, activeTab?.id, bounds, browserVisible, capabilities?.projectStorage, desktop, error, notice, projectId]);

  useEffect(() => () => {
    for (const tab of tabsRef.current) if (tab.created) void workbenchBrowser.close(tab.id, projectId).catch((caught) => {
      void logCaughtDiagnostic("interface.workbench_browser.cleanup_failed", "An embedded browser tab could not be cleaned up.", caught, "workbench_browser");
    });
  }, [projectId]);

  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (activeTab) {
      const formValue = event.currentTarget.querySelector("input")?.value;
      const latest = addressDraftRef.current.get(activeTab.id)
        ?? formValue
        ?? tabsRef.current.find((tab) => tab.id === activeTab.id)?.address
        ?? activeTab.address;
      void openAddress(activeTab.id, latest);
    }
  };

  const runControl = async (action: "back" | "forward" | "stop" | "reload") => {
    if (!activeTab) return;
    try { await workbenchBrowser.control(activeTab.id, projectId, action); }
    catch (caught) {
      void logCaughtDiagnostic("interface.workbench_browser.control_failed", "An embedded browser control failed.", caught, "workbench_browser");
      setError(errorMessage(caught));
    }
  };

  const clearData = async () => {
    const approved = await confirm({ title: "Clear Project browser data?", message: <>This closes all browser tabs and removes cookies, cache, and site storage for this Project only.</>, confirmLabel: "Clear browser data", tone: "danger" });
    if (!approved) return;
    try {
      await workbenchBrowser.clear(projectId);
      const next = blankTab();
      setTabs([next]); setActiveId(next.id); setNotice({ kind: "info", message: "Project browser data was cleared." });
    } catch (caught) {
      void logCaughtDiagnostic("interface.workbench_browser.clear_failed", "Project browser data could not be cleared.", caught, "workbench_browser");
      setError(errorMessage(caught));
    }
  };

  const addCurrentPageToKnowledge = async () => {
    if (!activeTab?.created || activeTab.loading || !activeTab.url || addingKnowledge) return;
    setAddingKnowledge(true);
    setNotice(undefined);
    setError(undefined);
    try {
      const created = await onAddKnowledgeUrl(activeTab.url);
      setNotice({
        kind: "knowledge",
        message: `${created.name} is ready for cited retrieval.`,
        sourceId: created.id,
      });
    } catch (caught) {
      void logCaughtDiagnostic("interface.workbench_browser.knowledge_ingest_failed", "The current browser page could not be added to Project Sources.", caught, "workbench_browser");
      setError(errorMessage(caught));
    } finally {
      setAddingKnowledge(false);
    }
  };

  const askNebulaAboutCurrentPage = async () => {
    if (!activeTab?.created || activeTab.loading || !activeTab.url || capturingContext) return;
    if (scopeDecision.state !== "in_scope") {
      setError(`Nebula can capture live context only for a page confirmed in scope. ${scopeDecision.detail}`);
      return;
    }
    const requestId = `capture-${globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random()}`}`.replace(/[^a-zA-Z0-9_-]/g, "-");
    captureRef.current = { requestId, tabId: activeTab.id, purpose: "assistant" };
    setCapturingContext(true);
    setError(undefined);
    setNotice(undefined);
    try {
      await workbenchBrowser.captureContext(activeTab.id, projectId, requestId);
    } catch (caught) {
      captureRef.current = undefined;
      setCapturingContext(false);
      void logCaughtDiagnostic("interface.workbench_browser.context_capture_failed", "The live browser page could not be prepared for Nebula.", caught, "workbench_browser");
      setError(`${errorMessage(caught)} Reload the page and try again.`);
    }
  };

  const preserveCurrentPageEvidence = async () => {
    if (!onUploadEvidence || !activeTab?.created || activeTab.loading || !activeTab.url || capturingContext) return;
    if (scopeDecision.state !== "in_scope") {
      setError(`Nebula can preserve page Evidence only for a page confirmed in scope. ${scopeDecision.detail}`);
      return;
    }
    const requestId = `evidence-${globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random()}`}`.replace(/[^a-zA-Z0-9_-]/g, "-");
    captureRef.current = { requestId, tabId: activeTab.id, purpose: "evidence" };
    setCapturingContext(true);
    setError(undefined);
    setNotice(undefined);
    try {
      await workbenchBrowser.captureContext(activeTab.id, projectId, requestId);
    } catch (caught) {
      void logCaughtDiagnostic("interface.security_browser.evidence_capture_failed", "The current browser page could not be captured for Evidence.", caught, "workbench_browser");
      captureRef.current = undefined;
      setCapturingContext(false);
      setError(`${errorMessage(caught)} Reload the page and try again.`);
    }
  };

  const normalizedWebAddress = () => {
    try { return normalizeBrowserInput(activeTab?.address ?? ""); }
    catch (caught) {
      // diagnostic-expected: local operator input validation is presented inline.
      setError(errorMessage(caught));
      return undefined;
    }
  };

  const openWebAddress = (event: FormEvent) => {
    event.preventDefault();
    setError(undefined);
    setNotice(undefined);
    const url = normalizedWebAddress();
    if (!url) return;
    updateTab(activeId, { address: url, url });
    const opened = window.open(url, "_blank", "noopener,noreferrer");
    if (!opened) {
      setError("The browser blocked the new tab. Allow pop-ups for Nebula and try again.");
      return;
    }
    setNotice({ kind: "info", message: "Opened the page in a separate browser tab." });
  };

  const addWebAddressToKnowledge = async () => {
    if (addingKnowledge) return;
    setError(undefined);
    setNotice(undefined);
    const url = normalizedWebAddress();
    if (!url) return;
    updateTab(activeId, { address: url, url });
    setAddingKnowledge(true);
    try {
      const created = await onAddKnowledgeUrl(url);
      setNotice({ kind: "knowledge", message: `${created.name} is ready for cited retrieval.`, sourceId: created.id });
    } catch (caught) {
      void logCaughtDiagnostic("interface.workbench_browser.web_knowledge_ingest_failed", "A browser URL could not be added to Project Sources.", caught, "workbench_browser");
      setError(errorMessage(caught));
    } finally {
      setAddingKnowledge(false);
    }
  };

  const selectResearchSession = async (nextSession: SecurityBrowserSession) => {
    if (nextSession.id === activeSession?.id) return;
    for (const tab of tabsRef.current) {
      if (tab.created) await workbenchBrowser.close(tab.id, projectId).catch((caught) => {
        void logCaughtDiagnostic("interface.security_browser.identity_tab_close_failed", "A browser tab could not close before the identity switch.", caught, "workbench_browser");
      });
    }
    hydratedProjectRef.current = undefined;
    setSessionHydrated(false);
    setSessionId(nextSession.id);
  };

  const selectIdentity = async (identityId: string) => {
    if (!workspace) return;
    const existing = workspace.sessions.find((session) => session.identityId === identityId && session.status === "active");
    if (existing) {
      await selectResearchSession(existing);
      return;
    }
    const identity = workspace.identities.find((item) => item.id === identityId);
    if (!identity) return;
    setIdentityBusy(true);
    try {
      const session = await api.createSecurityBrowserSession(projectId, {
        name: `${identity.name} research`,
        identityId,
        captureMode: activeSession?.captureMode ?? "headers",
      });
      setWorkspace((current) => current ? { ...current, sessions: [...current.sessions, session] } : current);
      await selectResearchSession(session);
    } catch (caught) {
      void logCaughtDiagnostic("interface.security_browser.identity_session_select_failed", "A browser session could not be created for the selected identity.", caught, "workbench_browser");
      setWorkspaceError(errorMessage(caught));
    } finally {
      setIdentityBusy(false);
    }
  };

  const createIdentity = async (event: FormEvent) => {
    event.preventDefault();
    const name = identityName.trim();
    if (!name || identityBusy) return;
    setIdentityBusy(true);
    setWorkspaceError(undefined);
    try {
      const identity = await api.createSecurityBrowserIdentity(projectId, { name });
      const session = await api.createSecurityBrowserSession(projectId, {
        name: `${identity.name} research`,
        identityId: identity.id,
        captureMode: activeSession?.captureMode ?? "headers",
      });
      setWorkspace((current) => current ? {
        ...current,
        identities: [...current.identities, identity],
        sessions: [...current.sessions, session],
      } : current);
      setIdentityName("");
      await selectResearchSession(session);
    } catch (caught) {
      void logCaughtDiagnostic("interface.security_browser.identity_create_failed", "A browser identity and its initial session could not be created.", caught, "workbench_browser");
      setWorkspaceError(errorMessage(caught));
    } finally {
      setIdentityBusy(false);
    }
  };

  const updateCaptureMode = async (captureMode: SecurityBrowserSession["captureMode"]) => {
    if (!activeSession) return;
    setWorkspaceError(undefined);
    try {
      const updated = await api.updateSecurityBrowserCapture(activeSession, {
        captureMode,
        proxyEnabled: activeSession.proxyEnabled,
        trustAcknowledged: activeSession.proxyTrustAcknowledged,
        interceptionEnabled: activeSession.interceptionEnabled,
        upstreamProxyEnabled: activeSession.upstreamProxyEnabled,
        upstreamProxyUrl: activeSession.upstreamProxyUrl,
        upstreamProxyCredentialRef: activeSession.upstreamProxyCredentialRef,
      });
      if (updated.proxyEnabled && typeof workbenchBrowser.configureProxy === "function") {
        await workbenchBrowser.configureProxy(projectId, updated.id, {
          enabled: updated.upstreamProxyEnabled,
          url: updated.upstreamProxyUrl,
          credentialRef: updated.upstreamProxyCredentialRef,
        }, updated.captureMode === "bodies");
      }
      setWorkspace((current) => current ? {
        ...current,
        sessions: current.sessions.map((session) => session.id === updated.id ? updated : session),
      } : current);
    } catch (caught) {
      void logCaughtDiagnostic("interface.security_browser.capture_mode_update_failed", "The browser capture detail could not be updated.", caught, "workbench_browser");
      setWorkspaceError(errorMessage(caught));
    }
  };

  const updateUpstreamProxy = async () => {
    if (!activeSession) return;
    setWorkspaceError(undefined);
    try {
      const updated = await api.updateSecurityBrowserCapture(activeSession, {
        captureMode: activeSession.captureMode,
        proxyEnabled: activeSession.proxyEnabled,
        trustAcknowledged: activeSession.proxyTrustAcknowledged,
        interceptionEnabled: activeSession.interceptionEnabled,
        upstreamProxyEnabled: upstreamProxyEnabledDraft,
        upstreamProxyUrl: upstreamProxyUrlDraft.trim() || undefined,
        upstreamProxyCredentialRef: upstreamProxyCredentialRefDraft.trim() || undefined,
      });
      if (updated.proxyEnabled && typeof workbenchBrowser.configureProxy === "function") {
        await workbenchBrowser.configureProxy(projectId, updated.id, {
          enabled: updated.upstreamProxyEnabled,
          url: updated.upstreamProxyUrl,
          credentialRef: updated.upstreamProxyCredentialRef,
        }, updated.captureMode === "bodies");
      }
      setWorkspace((current) => current ? {
        ...current,
        sessions: current.sessions.map((session) => session.id === updated.id ? updated : session),
      } : current);
      setNotice({ kind: "info", message: updated.upstreamProxyEnabled ? "The native session proxy is now chaining through the protected upstream reference." : "Upstream proxy chaining is disabled for this session." });
    } catch (caught) {
      void logCaughtDiagnostic("interface.security_browser.upstream_proxy_update_failed", "The upstream proxy configuration could not be applied.", caught, "workbench_browser");
      setWorkspaceError(`${errorMessage(caught)} No upstream credentials were returned to the UI or AI.`);
    }
  };

  const setProxyEnabled = async (proxyEnabled: boolean) => {
    if (!activeSession) return;
    if (proxyEnabled) {
      let path: string;
      try { path = await workbenchBrowser.revealProxyCa(projectId); }
      catch (caught) {
        void logCaughtDiagnostic("interface.security_browser.proxy_ca_reveal_failed", "The Project capture proxy CA could not be revealed.", caught, "workbench_browser");
        setWorkspaceError(errorMessage(caught)); return;
      }
      const approved = await confirm({
        title: "Enable Project capture proxy?",
        message: <>Nebula revealed <code>{path}</code>. Trust this Project-local CA in the operating system before enabling HTTPS capture. Its private key remains in protected Nebula application data. Enabling the proxy recreates open tabs in the same identity.</>,
        confirmLabel: "CA is trusted · enable",
      });
      if (!approved) return;
      setCaTrustAcknowledged(true);
    }
    setWorkspaceError(undefined);
    try {
      const updated = await api.updateSecurityBrowserCapture(activeSession, {
        captureMode: activeSession.captureMode,
        proxyEnabled,
        trustAcknowledged: proxyEnabled,
        interceptionEnabled: activeSession.interceptionEnabled,
        upstreamProxyEnabled: activeSession.upstreamProxyEnabled,
        upstreamProxyUrl: activeSession.upstreamProxyUrl,
        upstreamProxyCredentialRef: activeSession.upstreamProxyCredentialRef,
      });
      for (const tab of tabsRef.current) {
        if (tab.created) await workbenchBrowser.close(tab.id, projectId);
      }
      if (!proxyEnabled && typeof workbenchBrowser.stopProxy === "function") {
        await workbenchBrowser.stopProxy(projectId, activeSession.id);
      }
      setTabs((current) => current.map((tab) => ({ ...tab, created: false, loading: false })));
      setWorkspace((current) => current ? {
        ...current,
        sessions: current.sessions.map((session) => session.id === updated.id ? updated : session),
      } : current);
      setNotice({ kind: "info", message: proxyEnabled ? "Capture proxy enabled. Reopen the tab to begin HTTP/2 and WebSocket capture." : "Capture proxy disabled. Reopen the tab to browse directly." });
    } catch (caught) {
      void logCaughtDiagnostic("interface.security_browser.proxy_update_failed", "The browser capture proxy setting could not be updated.", caught, "workbench_browser");
      setWorkspaceError(errorMessage(caught));
    }
  };

  const rotateProjectCa = async () => {
    const approved = await confirm({
      title: "Rotate the Project browser CA?",
      message: "Rotation invalidates the currently trusted certificate. Nebula will disable the session proxy and require a new explicit OS trust acknowledgement before HTTPS capture resumes.",
      confirmLabel: "Rotate CA",
      tone: "danger",
    });
    if (!approved) return;
    try {
      if (activeSession?.proxyEnabled) await setProxyEnabled(false);
      const next = await workbenchBrowser.rotateProxyCa(projectId);
      setCaStatus(next);
      setCaTrustAcknowledged(false);
      setNotice({ kind: "info", message: "The Project CA was rotated. Trust the new certificate before enabling HTTPS capture." });
    } catch (caught) {
      void logCaughtDiagnostic("interface.security_browser.proxy_ca_rotate_failed", "The Project browser CA could not be rotated.", caught, "workbench_browser");
      setWorkspaceError(errorMessage(caught));
    }
  };

  const revealProjectCa = async () => {
    try {
      const path = await workbenchBrowser.revealProxyCa(projectId);
      setCaTrustAcknowledged(false);
      setNotice({ kind: "info", message: `Project certificate revealed at ${path}. Import only this certificate into the OS trust store.` });
    } catch (caught) {
      void logCaughtDiagnostic("interface.security_browser.proxy_ca_reveal_failed", "The Project capture proxy CA could not be revealed.", caught, "workbench_browser");
      setWorkspaceError(errorMessage(caught));
    }
  };

  const revokeProjectCa = async () => {
    const approved = await confirm({
      title: "Revoke the Project browser CA?",
      message: "This deletes the Project-local certificate and key. Existing HTTPS capture will stop; a later status check will generate a new CA, which still requires explicit OS trust.",
      confirmLabel: "Revoke CA",
      tone: "danger",
    });
    if (!approved) return;
    try {
      if (activeSession?.proxyEnabled) await setProxyEnabled(false);
      await workbenchBrowser.revokeProxyCa(projectId);
      setCaStatus({ certificatePath: "", fingerprint: "", state: "revoked" });
      setCaTrustAcknowledged(false);
      setNotice({ kind: "info", message: "The Project CA was revoked and HTTPS capture is disabled." });
    } catch (caught) {
      void logCaughtDiagnostic("interface.security_browser.proxy_ca_revoke_failed", "The Project browser CA could not be revoked.", caught, "workbench_browser");
      setWorkspaceError(errorMessage(caught));
    }
  };

  const executeApprovedAction = async (action: import("../api/types").SecurityBrowserAction) => {
    if (!desktop) { setWorkspaceError("Approved actions execute only in the desktop browser that owns the live tab."); return; }
    const tab = tabsRef.current.find((item) => item.id === action.tabId);
    if (!tab?.created || tab.url !== action.pageUrl) {
      setWorkspaceError("The approved action's exact live tab and page are not open. Restore that tab or propose the action again.");
      return;
    }
    setWorkspaceError(undefined);
    let executing: import("../api/types").SecurityBrowserAction | undefined;
    try {
      executing = await api.startSecurityBrowserAction(action);
      actionExecutionRef.current.set(action.id, executing);
      setWorkspace((current) => current ? {
        ...current,
        actions: current.actions.map((item) => item.id === executing!.id ? executing! : item),
      } : current);
      await workbenchBrowser.executeAction(tab.id, projectId, {
        actionId: executing.id,
        kind: executing.kind,
        locator: executing.locator,
        arguments: executing.arguments,
        pageUrl: executing.pageUrl,
      });
    } catch (caught) {
      actionExecutionRef.current.delete(action.id);
      if (executing) {
        try {
          const failed = await api.finishSecurityBrowserAction(executing, { state: "failed", error: errorMessage(caught) });
          setWorkspace((current) => current ? { ...current, actions: current.actions.map((item) => item.id === failed.id ? failed : item) } : current);
        } catch (receiptError) {
          void logCaughtDiagnostic("interface.security_browser.action_start_recovery_failed", "A failed browser action could not be reconciled.", receiptError, "workbench_browser");
        }
      }
      setWorkspaceError(errorMessage(caught));
    }
  };

  const beginReplay = (exchange: SecurityBrowserExchange) => {
    const reusableHeaders = Object.fromEntries(Object.entries(exchange.requestHeaders).filter(([name, value]) =>
      !/authorization|cookie|csrf|xsrf|api[-_]?key|token/i.test(name) && !value.startsWith("<redacted:"),
    ));
    setReplayExchange(exchange);
    setReplayMethod(exchange.method);
    setReplayUrl(exchange.url);
    setReplayHeaders(JSON.stringify(reusableHeaders, null, 2));
    setReplayBody("");
  };

  const proposeReplay = async (event: FormEvent) => {
    event.preventDefault();
    if (!activeSession || !activeTab?.created || !activeTab.url || !replayExchange || replayBusy) return;
    setReplayBusy(true);
    setWorkspaceError(undefined);
    try {
      const headers = JSON.parse(replayHeaders) as unknown;
      if (!headers || Array.isArray(headers) || typeof headers !== "object" || !Object.values(headers).every((value) => typeof value === "string")) {
        throw new Error("Replay headers must be a JSON object with string values.");
      }
      const action = await api.proposeSecurityBrowserAction(activeSession.id, {
        tabId: activeTab.id,
        kind: "replay",
        locator: {},
        arguments: { method: replayMethod, url: replayUrl, headers, body: replayBody },
        proposal: `Replay ${replayMethod} ${replayUrl} through identity ${activeIdentity?.name ?? activeSession.identityId}; source exchange ${replayExchange.id}.`,
        proposedBy: operatorId,
        pageUrl: activeTab.url,
      });
      setWorkspace((current) => current ? { ...current, actions: [...current.actions, action] } : current);
      setReplayExchange(undefined);
      setResearchView("actions");
      setNotice({ kind: "info", message: "Replay proposal is ready for exact operator approval. No request has been sent." });
    } catch (caught) {
      void logCaughtDiagnostic("interface.security_browser.replay_proposal_failed", "The edited browser request could not be prepared for approval.", caught, "workbench_browser");
      setWorkspaceError(errorMessage(caught));
    } finally {
      setReplayBusy(false);
    }
  };

  const sessionTraffic = workspace?.traffic.filter((exchange) => exchange.sessionId === activeSession?.id) ?? [];
  const sessionAutomationCommands = automationStatus?.commands.filter((command) => command.sessionId === activeSession?.id) ?? [];
  const sessionAutomationRules = automationStatus?.rules.filter((rule) => rule.sessionId === activeSession?.id) ?? [];
  const selectedAutomationProvider = automationProviders.find((provider) => provider.id === automationProviderId);
  const selectedExchanges = selectedExchangeIds
    .map((id) => sessionTraffic.find((exchange) => exchange.id === id))
    .filter((exchange): exchange is SecurityBrowserExchange => Boolean(exchange));
  const comparison = selectedExchanges.length === 2 ? {
    status: selectedExchanges[0].statusCode === selectedExchanges[1].statusCode ? "same" : `${selectedExchanges[0].statusCode ?? "—"} → ${selectedExchanges[1].statusCode ?? "—"}`,
    changedHeaders: Array.from(new Set([
      ...Object.keys(selectedExchanges[0].responseHeaders),
      ...Object.keys(selectedExchanges[1].responseHeaders),
    ])).filter((name) => selectedExchanges[0].responseHeaders[name] !== selectedExchanges[1].responseHeaders[name]),
    bytes: selectedExchanges[0].responseBytes === selectedExchanges[1].responseBytes ? "same" : `${selectedExchanges[0].responseBytes ?? "—"} → ${selectedExchanges[1].responseBytes ?? "—"}`,
  } : undefined;

  const researchPanel = researchOpen ? <aside className="browser-research-panel" aria-label="Security research workbench">
    <header>
      <div><strong>Research workbench</strong><small>{activeSession?.name ?? "Loading durable session"} · {activeIdentity?.name ?? "No identity"}</small></div>
      <button type="button" aria-label="Close research workbench" onClick={() => setResearchOpen(false)}><X size={15} /></button>
    </header>
    <nav aria-label="Research tools">
      <button type="button" className={researchView === "traffic" ? "active" : ""} onClick={() => setResearchView("traffic")}><Network size={14} /> Traffic <span>{sessionTraffic.length}</span></button>
      <button type="button" className={researchView === "actions" ? "active" : ""} onClick={() => setResearchView("actions")}><Sparkles size={14} /> Actions <span>{workspace?.actions.filter((action) => action.sessionId === activeSession?.id).length ?? 0}</span></button>
      <button type="button" className={researchView === "identities" ? "active" : ""} onClick={() => setResearchView("identities")}><UserRound size={14} /> Identities <span>{workspace?.identities.length ?? 0}</span></button>
      <button type="button" className={researchView === "session" ? "active" : ""} onClick={() => setResearchView("session")}><History size={14} /> Session</button>
    </nav>
    {workspaceLoading ? <div className="browser-research-empty"><LoaderCircle className="spin" size={18} /> Loading durable browser state…</div> : workspaceError ? <div className="browser-research-empty error" role="alert"><strong>Research state is unavailable</strong><span>{workspaceError}</span><button className="button secondary" type="button" onClick={() => void refreshWorkspace()}>Try again</button></div> : researchView === "traffic" ? <div className="browser-traffic-workbench">
      <div className="browser-research-toolbar"><span>Metadata and redacted headers</span><button type="button" disabled={selectedExchangeIds.length !== 2} onClick={() => setSelectedExchangeIds([])}><GitCompareArrows size={13} /> {selectedExchangeIds.length === 2 ? "Clear comparison" : "Select two to compare"}</button></div>
      {comparison && <div className="browser-exchange-diff"><strong>Authorization response diff</strong><span>Status: {comparison.status}</span><span>Bytes: {comparison.bytes}</span><span>{comparison.changedHeaders.length ? `${comparison.changedHeaders.length} response headers changed: ${comparison.changedHeaders.join(", ")}` : "Response headers are identical."}</span></div>}
      {sessionTraffic.length ? <ol className="browser-traffic-list">{[...sessionTraffic].reverse().map((exchange) => <li key={exchange.id} className={selectedExchangeIds.includes(exchange.id) ? "selected" : ""}>
        <label><input type="checkbox" checked={selectedExchangeIds.includes(exchange.id)} onChange={(event) => setSelectedExchangeIds((current) => event.target.checked ? [...current.filter((id) => id !== exchange.id), exchange.id].slice(-2) : current.filter((id) => id !== exchange.id))} /><span className={`browser-method method-${exchange.method.toLowerCase()}`}>{exchange.method}</span><code>{exchange.blocked ? "BLOCKED" : exchange.statusCode ?? "…"}</code><span title={exchange.url}>{exchange.url}</span><small>{exchange.blocked ? (exchange.error ?? "Rejected by Project scope or proxy rule") : `${exchange.protocol} · ${exchange.durationMs === undefined ? "—" : `${exchange.durationMs} ms`}`}</small></label>
        <details><summary>Request and response</summary><section><strong>Request headers</strong><pre>{JSON.stringify(exchange.requestHeaders, null, 2)}</pre><strong>Response headers</strong><pre>{JSON.stringify(exchange.responseHeaders, null, 2)}</pre><button className="button secondary" type="button" disabled={!desktop || !activeTab?.created} onClick={() => beginReplay(exchange)}>Edit and replay with {activeIdentity?.name ?? "active identity"}</button></section></details>
      </li>)}</ol> : <div className="browser-research-empty"><Network size={20} /><strong>No captured traffic</strong><span>Traffic appears here when the native interception proxy is available and capture is enabled.</span></div>}
      {replayExchange && <form className="browser-replay-editor" onSubmit={proposeReplay}><header><strong>Edit request · no request sent yet</strong><button type="button" aria-label="Close request editor" onClick={() => setReplayExchange(undefined)}><X size={14} /></button></header><label>Method<input value={replayMethod} maxLength={16} onChange={(event) => setReplayMethod(event.target.value.toUpperCase())} /></label><label>URL<input value={replayUrl} maxLength={16384} onChange={(event) => setReplayUrl(event.target.value)} /></label><label>Headers JSON<textarea value={replayHeaders} rows={5} onChange={(event) => setReplayHeaders(event.target.value)} /></label><label>Body<textarea value={replayBody} rows={5} maxLength={65536} onChange={(event) => setReplayBody(event.target.value)} /></label><p>Cookies remain inside the selected identity and are attached by the system webview. Reusable secret headers are never copied into this editor.</p><button className="button primary" type="submit" disabled={replayBusy || !replayUrl.trim()}>{replayBusy ? <LoaderCircle className="spin" size={14} /> : <Sparkles size={14} />} Create approval proposal</button></form>}
    </div> : researchView === "actions" ? <div className="browser-action-list">
      {(workspace?.actions.filter((action) => action.sessionId === activeSession?.id).length ?? 0) > 0 ? workspace?.actions.filter((action) => action.sessionId === activeSession?.id).map((action) => <article key={action.id}><header><span className={`browser-action-status ${action.status}`}>{action.status.replace("_", " ")}</span><strong>{action.kind}</strong><code>{action.actionSha256.slice(0, 12)}</code></header><p>{action.proposal}</p><small>{action.pageUrl} · scope r{action.scopePolicyRevision}</small>{action.status === "proposed" && <div><button className="button secondary" type="button" onClick={() => void api.decideSecurityBrowserAction(action, "reject", operatorId).then(refreshWorkspace).catch((caught) => { void logCaughtDiagnostic("interface.security_browser.action_reject_failed", "The browser action could not be rejected.", caught, "workbench_browser"); setWorkspaceError(errorMessage(caught)); })}>Reject</button><button className="button primary" type="button" onClick={() => void api.decideSecurityBrowserAction(action, "approve", operatorId).then(refreshWorkspace).catch((caught) => { void logCaughtDiagnostic("interface.security_browser.action_approve_failed", "The browser action could not be approved.", caught, "workbench_browser"); setWorkspaceError(errorMessage(caught)); })}>Approve action</button></div>}{action.status === "approved" && <div><button className="button primary" type="button" disabled={!desktop || actionExecutionRef.current.size > 0} onClick={() => void executeApprovedAction(action)}>Execute once</button></div>}</article>) : <div className="browser-research-empty"><Sparkles size={20} /><strong>No browser action proposals</strong><span>AI suggestions are inert until they appear here and you approve them.</span></div>}
    </div> : researchView === "identities" ? <div className="browser-identity-workbench">
      <label>Active identity<select value={activeIdentity?.id ?? ""} disabled={identityBusy} onChange={(event) => void selectIdentity(event.target.value)}>{workspace?.identities.filter((identity) => !identity.revokedAt).map((identity) => <option key={identity.id} value={identity.id}>{identity.name}{identity.isDefault ? " · default" : ""}</option>)}</select></label>
      <p>Each identity has a separate cookie jar, cache, and site storage partition. Switching identities closes the visible native tabs before restoring that identity's durable session.</p>
      <form onSubmit={createIdentity}><label htmlFor="browser-new-identity">New identity</label><div><input id="browser-new-identity" value={identityName} maxLength={120} placeholder="e.g. Admin, Member, Anonymous" onChange={(event) => setIdentityName(event.target.value)} /><button className="button secondary" type="submit" disabled={!identityName.trim() || identityBusy}>{identityBusy ? <LoaderCircle className="spin" size={14} /> : <Plus size={14} />} Create isolated identity</button></div></form>
      <ul>{workspace?.identities.map((identity) => <li key={identity.id}><span style={{ background: identity.color }} /><div><strong>{identity.name}</strong><small>{identity.ephemeral ? "Ephemeral" : "Persistent"} · {workspace.sessions.filter((session) => session.identityId === identity.id).length} sessions</small></div></li>)}</ul>
    </div> : <div className="browser-session-workbench">
      <label>Research session<select value={activeSession?.id ?? ""} onChange={(event) => { const next = workspace?.sessions.find((session) => session.id === event.target.value); if (next) void selectResearchSession(next); }}>{workspace?.sessions.map((session) => <option key={session.id} value={session.id}>{session.name} · {session.status}</option>)}</select></label>
      <label>Capture detail<select value={activeSession?.captureMode ?? "headers"} onChange={(event) => void updateCaptureMode(event.target.value as SecurityBrowserSession["captureMode"])}><option value="metadata">Metadata only</option><option value="headers">Redacted headers</option><option value="bodies">Redacted bounded bodies (1 MiB)</option></select></label>
      <fieldset className="browser-upstream-controls" disabled={!desktop || !activeSession?.proxyEnabled}>
        <legend>Protected upstream proxy</legend>
        <label><input type="checkbox" checked={upstreamProxyEnabledDraft} onChange={(event) => setUpstreamProxyEnabledDraft(event.target.checked)} /> Chain in-scope traffic through an upstream proxy</label>
        <label>Proxy URL<input value={upstreamProxyUrlDraft} onChange={(event) => setUpstreamProxyUrlDraft(event.target.value)} placeholder="http://proxy.example:8080 or socks5://proxy.example:1080" /></label>
        <label>Credential reference<input value={upstreamProxyCredentialRefDraft} onChange={(event) => setUpstreamProxyCredentialRefDraft(event.target.value)} placeholder="vault:… or env:NEBULA_PROXY" autoComplete="off" /></label>
        <small>Use an opaque vault/env reference to a <code>username:password</code> pair. The secret is resolved only inside the native connector.</small>
        <button className="button secondary" type="button" onClick={() => void updateUpstreamProxy()}>Apply upstream settings</button>
      </fieldset>
      <dl><div><dt>Durable tabs</dt><dd>{activeSession?.tabs.length ?? 0}</dd></div><div><dt>Device owner</dt><dd>{activeSession?.deviceOwner ?? "Unclaimed"}</dd></div><div><dt>Capture proxy</dt><dd>{capabilities?.interceptionProxy ? "HTTP history enabled when CA is trusted" : "Native proxy unavailable"}</dd></div><div><dt>HTTP/2 · WebSocket</dt><dd>{capabilities?.http2Capture && capabilities.websocketCapture ? "Captured" : "Unavailable in this build"}</dd></div></dl>
      {desktop && capabilities?.interceptionProxy && <>
        <section className="browser-ca-card" aria-labelledby="browser-ca-title">
          <header><strong id="browser-ca-title">Project capture CA</strong><span className={`browser-action-status ${caStatus?.state ?? "pending"}`}>{caStatus?.state ?? "checking"}</span></header>
          <p>CA generation is automatic and Project-local. OS trust is always an explicit operator action; Nebula never installs it silently.</p>
          {caStatus?.fingerprint ? <dl><div><dt>SHA-256 fingerprint</dt><dd><code>{caStatus.fingerprint}</code></dd></div><div><dt>Expires</dt><dd>{caStatus.expiresAt ? new Date(caStatus.expiresAt).toLocaleString() : "Unknown"}</dd></div></dl> : <small>Reading the durable certificate status…</small>}
          {caStatus?.trustInstructions && <small>{caStatus.trustInstructions}</small>}
          <div className="browser-proxy-controls"><button className="button secondary" type="button" onClick={() => void revealProjectCa()}>Reveal certificate</button><button className="button secondary" type="button" disabled={!caStatus || caStatus.state === "revoked"} onClick={() => void rotateProjectCa()}>Rotate</button><button className="button quiet danger" type="button" disabled={!caStatus || caStatus.state === "revoked"} onClick={() => void revokeProjectCa()}>Revoke</button></div>
          {caTrustAcknowledged && <small role="status">Trust acknowledgement recorded for this Nebula session. The operating-system trust store remains under your control.</small>}
        </section>
        <div className="browser-proxy-controls"><button className={activeSession?.proxyEnabled ? "button danger" : "button primary"} type="button" onClick={() => void setProxyEnabled(!activeSession?.proxyEnabled)}>{activeSession?.proxyEnabled ? "Disable capture proxy" : "I trust the CA · enable capture proxy"}</button></div>
      </>}
      <section className="browser-automation-card" aria-labelledby="browser-automation-title">
        <header><div><strong id="browser-automation-title">Autonomous web test</strong><small>Run-owned browser and proxy control inside the frozen Project scope.</small></div>{activeAutomationLease && <span className="browser-action-status executing">{activeAutomationLease.status}</span>}</header>
        {activeAutomationLease ? <>
          <dl><div><dt>Run</dt><dd><code>{activeAutomationLease.runId.slice(0, 12)}</code></dd></div><div><dt>Scope revision</dt><dd>{activeAutomationLease.scopePolicyRevision}</dd></div><div><dt>Commands</dt><dd>{activeAutomationLease.commandsUsed} / {activeAutomationLease.maxCommands}</dd></div><div><dt>Requests</dt><dd>{activeAutomationLease.requestsUsed} / {activeAutomationLease.maxRequests}</dd></div></dl>
          <p>Allowed risks: {activeAutomationLease.allowedRiskClasses.join(", ") || "none"}. Native commands remain desktop-only; this panel can monitor from mobile.</p>
          <div className="browser-proxy-controls"><button className="button danger" type="button" onClick={() => void api.stopSecurityBrowserAutomation(activeAutomationLease.runId).then((next) => { setAutomationStatus(next); setNotice({ kind: "info", message: "Emergency stop requested. Pending commands and run-owned proxy rules are being revoked." }); }).catch((caught) => { void logCaughtDiagnostic("interface.security_browser.automation_stop_failed", "The autonomous browser run could not be stopped.", caught, "workbench_browser"); setWorkspaceError(errorMessage(caught)); })}>Emergency stop</button></div>
          <details><summary>Recent autonomous activity ({sessionAutomationCommands.length} commands · {sessionAutomationRules.filter((rule) => rule.enabled).length} active rules)</summary><ol>{[...sessionAutomationCommands].slice(-8).reverse().map((command) => <li key={command.id}><span className={`browser-action-status ${command.status}`}>{command.status}</span><code>{command.kind}</code><small>{command.error ?? (command.result.untrusted_page_data ? "untrusted page data returned" : "")}</small></li>)}</ol></details>
        </> : <>
          <p>Start only after opening the intended in-scope page in this desktop identity. The run survives closing this panel, uses credential references only, and cannot expand scope.</p>
          {!desktop ? <p className="browser-automation-mobile-note">Mobile can observe and stop an existing run. Pair a desktop browser to start or execute native commands.</p> : <button className="button primary" type="button" disabled={!activeTab?.created || scopeDecision.state !== "in_scope"} onClick={() => setAutomationFormOpen((value) => !value)}>{automationFormOpen ? "Hide start form" : "Start autonomous web test"}</button>}
          {automationFormOpen && desktop && <form className="browser-automation-form" onSubmit={startAutonomousRun}>
            <label>Target subset<select value={automationTarget} onChange={(event) => setAutomationTarget(event.target.value)} required><option value="">Choose an authorized target</option>{automationTargetOptions.map((target) => <option value={target} key={target}>{target}</option>)}</select></label>
            <label>Model provider<select value={automationProviderId} onChange={(event) => setAutomationProviderId(event.target.value)} required><option value="">Choose an enabled provider</option>{automationProviders.map((provider) => <option key={provider.id} value={provider.id}>{provider.name} · {provider.state}</option>)}</select></label>
            <label>Model<select value={automationModel} onChange={(event) => setAutomationModel(event.target.value)} required disabled={!selectedAutomationProvider}><option value="">Choose a verified model</option>{selectedAutomationProvider?.models.map((model) => <option key={model} value={model}>{model}</option>)}</select></label>
            <fieldset><legend>Allowed risk classes</legend>{[["passive", "Passive observation"], ["active_scan", "Active scan and replay"], ["credential_use", "Credential use by reference"]].map(([value, label]) => <label key={value}><input type="checkbox" checked={automationRisks.includes(value)} onChange={(event) => setAutomationRisks((current) => event.target.checked ? [...current, value] : current.filter((item) => item !== value))} /><span>{label}</span></label>)}</fieldset>
            {automationRisks.includes("credential_use") && <label>Credential references<input value={automationCredentialRefs} placeholder="vault:reference-1, session:reference-2" onChange={(event) => setAutomationCredentialRefs(event.target.value)} /><small>References only. Never paste a password, cookie, token, or API key.</small></label>}
            <div className="resource-form-grid"><label>Duration (minutes)<input type="number" min={1} max={60} value={automationDuration} onChange={(event) => setAutomationDuration(Number(event.target.value))} /></label><label>Tool-call budget<input type="number" min={1} max={100} value={automationMaxCommands} onChange={(event) => setAutomationMaxCommands(Number(event.target.value))} /></label><label>Request budget<input type="number" min={1} max={1000000} value={automationMaxRequests} onChange={(event) => setAutomationMaxRequests(Number(event.target.value))} /></label></div>
            <p>Exploitation, persistence, destructive actions, and scope changes require a separate durable step-up grant and are not enabled by this form.</p>
            <button className="button primary" type="submit" disabled={automationBusy || !automationProviderId || !automationModel || !automationTarget || !automationRisks.length}>{automationBusy ? <LoaderCircle className="spin" size={14} /> : <Sparkles size={14} />} {automationBusy ? "Starting…" : "Approve and start run"}</button>
          </form>}
        </>}
      </section>
      {desktop && capabilities?.devtools && <button className="button secondary" type="button" disabled={!activeTab?.created} onClick={() => activeTab && void workbenchBrowser.openDevtools(activeTab.id, projectId).catch((caught) => { void logCaughtDiagnostic("interface.security_browser.devtools_open_failed", "Browser DevTools could not be opened.", caught, "workbench_browser"); setError(errorMessage(caught)); })}><Bug size={14} /> Open DevTools</button>}
      {!desktop && activeSession && activeTab?.url && <button className="button primary" type="button" onClick={() => void api.createSecurityBrowserHandoff(activeSession.id, { requestedByDeviceId: "paired-browser", command: "navigate", tabId: activeTab.id, url: activeTab.url }).then(() => { setNotice({ kind: "info", message: "Navigation queued for the desktop browser for five minutes." }); return refreshWorkspace(); }).catch((caught) => { void logCaughtDiagnostic("interface.security_browser.handoff_create_failed", "A browser navigation handoff could not be queued for the desktop.", caught, "workbench_browser"); setWorkspaceError(errorMessage(caught)); })}>Send page to desktop</button>}
    </div>}
  </aside> : null;

  if (!desktop) return <div className={`workbench-browser web-browser-fallback${researchOpen ? " research-open" : ""}`}>
    <div className="browser-web-research-bar"><button className="button secondary" type="button" aria-expanded={researchOpen} onClick={() => setResearchOpen((value) => !value)}><Network size={14} /> Research workbench</button><span>Durable history and desktop handoff are available on paired devices.</span></div>
    {error && <div className="browser-notice error" role="alert"><span>{error}</span><button type="button" aria-label="Dismiss browser error" onClick={() => setError(undefined)}><X size={14} /></button></div>}
    {notice && <div className="browser-notice" role="status">
      {notice.kind === "knowledge" ? <BookOpenCheck size={14} /> : <Check size={14} />}
      <span>{notice.message}</span>
      {notice.kind === "knowledge" && <Link to={`/project?view=sources&source=${encodeURIComponent(notice.sourceId)}`}>View source <ExternalLink size={12} /></Link>}
      <button type="button" aria-label="Dismiss browser notice" onClick={() => setNotice(undefined)}><X size={14} /></button>
    </div>}
    <div className="browser-surface">
      <div className="browser-start">
        <Globe2 size={34} />
        <strong>Browse from this device</strong>
        <p>The isolated embedded webview is a desktop-app capability. From the web interface, open a page in a separate tab or add its URL directly to Project Sources.</p>
        <span className={`browser-start-scope ${scopeDecision.state}`}><ShieldCheck size={14} aria-hidden="true" /> {scopeDecision.label} · {scopeDecision.detail}</span>
        <form onSubmit={openWebAddress}>
          <Search size={16} />
          <input aria-label="Web address" autoFocus={active} value={activeTab?.address ?? ""} placeholder="Search or enter an address" onChange={(event) => activeTab && updateTab(activeTab.id, { address: event.target.value })} />
          <button className="button primary" type="submit">Open</button>
        </form>
        <button className="button secondary" type="button" disabled={addingKnowledge || !activeTab?.address.trim()} onClick={() => void addWebAddressToKnowledge()}>{addingKnowledge ? <LoaderCircle className="spin" size={15} /> : <BookPlus size={15} />} Add to Sources</button>
      </div>
    </div>
    {researchPanel}
  </div>;

  return (
    <div className={`workbench-browser${researchOpen ? " research-open" : ""}`}>
      <div className="browser-tab-strip" role="tablist" aria-label="Browser tabs">
        {tabs.map((tab) => <div className={tab.id === activeId ? "browser-tab active" : "browser-tab"} key={tab.id}><button type="button" role="tab" aria-selected={tab.id === activeId} title={tab.title} onClick={() => setActiveId(tab.id)}>{tab.loading ? <LoaderCircle className="spin" size={13} /> : <Globe2 size={13} />}<span>{tab.title}</span></button><button type="button" aria-label={`Close ${tab.title}`} onClick={() => void closeTab(tab.id)}><X size={13} /></button></div>)}
        <button className="browser-new-tab" type="button" aria-label="New browser tab" disabled={tabs.length >= MAX_TABS} onClick={() => addTab()}><Plus size={15} /></button>
      </div>
      <div className="browser-toolbar" ref={toolbarRef}>
        <button type="button" aria-label="Back" disabled={!activeTab?.created} onClick={() => void runControl("back")}><ArrowLeft size={16} /></button>
        <button type="button" aria-label="Forward" disabled={!activeTab?.created} onClick={() => void runControl("forward")}><ArrowRight size={16} /></button>
        <button type="button" aria-label={activeTab?.loading ? "Stop loading" : "Reload"} disabled={!activeTab?.created} onClick={() => void runControl(activeTab?.loading ? "stop" : "reload")}>{activeTab?.loading ? <X size={15} /> : <RefreshCw size={15} />}</button>
        <form onSubmit={submit}><Search size={15} aria-hidden="true" /><label className="sr-only" htmlFor="browser-address">Address or search</label><input id="browser-address" value={activeTab?.address ?? ""} placeholder="Search or enter an address" autoComplete="off" spellCheck={false} onChange={(event) => { if (activeTab) { addressDraftRef.current.set(activeTab.id, event.target.value); updateTab(activeTab.id, { address: event.target.value }); } }} /><span className={`browser-scope-badge ${scopeDecision.state}`} title={scopeDecision.detail}><ShieldCheck size={13} aria-hidden="true" /><span>{scopeDecision.label}</span></span></form>
        <button type="button" aria-label="Ask Nebula about the live page" title={scopeDecision.state === "in_scope" ? "Capture rendered text, form metadata, and links for a reviewed chat attachment. Input values and cookies are excluded." : `Live-page AI capture is unavailable until this address is confirmed in scope. ${scopeDecision.detail}`} disabled={!activeTab?.created || activeTab.loading || !activeTab.url || capturingContext || scopeDecision.state !== "in_scope"} onClick={() => void askNebulaAboutCurrentPage()}>{capturingContext ? <LoaderCircle className="spin" size={15} /> : <Sparkles size={15} />}</button>
        {onUploadEvidence && <button type="button" aria-label="Preserve live page as Evidence" title="Preserve a scope-checked semantic page capture as immutable Evidence. Form values and cookies are excluded." disabled={!activeTab?.created || activeTab.loading || !activeTab.url || capturingContext || scopeDecision.state !== "in_scope"} onClick={() => void preserveCurrentPageEvidence()}><BookOpenCheck size={15} /></button>}
        <button type="button" aria-label="Add current page to Project Sources" title="Add this public page to Project Sources" disabled={!activeTab?.created || activeTab.loading || !activeTab.url || addingKnowledge} onClick={() => void addCurrentPageToKnowledge()}>{addingKnowledge ? <LoaderCircle className="spin" size={15} /> : <BookPlus size={15} />}</button>
        <button type="button" className={researchOpen ? "active" : ""} aria-label="Security research workbench" aria-expanded={researchOpen} title="Traffic, replay, actions, identities, and durable session" onClick={() => setResearchOpen((value) => !value)}><Network size={15} /></button>
        <button type="button" aria-label="Clear Project browser data" title="Clear Project browser data" onClick={() => void clearData()}><Trash2 size={15} /></button>
      </div>
      {capabilities?.projectStorage === "ephemeral" && <div className="browser-privacy-notice"><ShieldCheck size={14} /> macOS 13 browser data is isolated and cleared when Nebula closes.</div>}
      {error && <div className="browser-notice error" role="alert"><span>{error}</span><button type="button" aria-label="Dismiss browser error" onClick={() => setError(undefined)}><X size={14} /></button></div>}
      {notice && <div className="browser-notice" role="status">
        {notice.kind === "knowledge" ? <BookOpenCheck size={14} /> : notice.kind === "download" ? <Download size={14} /> : <Check size={14} />}
        <span>{notice.message}</span>
        {notice.kind === "download" && <button type="button" onClick={onOpenFiles}>Open Files <ExternalLink size={12} /></button>}
        {notice.kind === "knowledge" && <Link to={`/project?view=sources&source=${encodeURIComponent(notice.sourceId)}`}>View source <ExternalLink size={12} /></Link>}
        <button type="button" aria-label="Dismiss browser notice" onClick={() => setNotice(undefined)}><X size={14} /></button>
      </div>}
      <div className={`browser-surface${activeTab?.created ? " is-live" : ""}`} ref={surfaceRef}>
        {!activeTab?.created && <div className="browser-start"><Globe2 size={34} /><strong>Browse from the Workbench</strong><p>Pages run in an isolated {capabilities?.engine ?? "system webview"} profile for the selected research identity. Nebula captures live page context only when you ask.</p><span className={`browser-start-scope ${scopeDecision.state}`}><ShieldCheck size={14} aria-hidden="true" /> {scopeDecision.label} · {scopeDecision.detail}</span><form onSubmit={submit}><Search size={16} /><input aria-label="Start browsing" autoFocus={active} value={activeTab?.address ?? ""} placeholder={!sessionHydrated || !deviceId ? "Loading isolated identity…" : "Search or enter an address"} disabled={!sessionHydrated || !activeIdentity || !deviceId} onChange={(event) => { if (activeTab) { addressDraftRef.current.set(activeTab.id, event.target.value); updateTab(activeTab.id, { address: event.target.value }); } }} /><button className="button primary" type="submit" disabled={!sessionHydrated || !activeIdentity || !deviceId}>Go</button></form>{activeTab?.error && <small role="alert">{activeTab.error}</small>}</div>}
      </div>
      {researchPanel}
    </div>
  );
}
