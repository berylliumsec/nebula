import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";
import { ArrowLeft, ArrowRight, Download, ExternalLink, Globe2, LoaderCircle, Plus, RefreshCw, Search, ShieldCheck, Trash2, X } from "lucide-react";
import { listen } from "@tauri-apps/api/event";
import { isTauriRuntime } from "../api/runtime";
import {
  normalizeBrowserInput,
  workbenchBrowser,
  type BrowserBounds,
  type BrowserCapabilities,
  type BrowserDownloadEvent,
  type BrowserPageEvent,
} from "../api/workbenchBrowser";
import { logCaughtDiagnostic } from "../diagnostics";
import { useChrome } from "../state/ChromeContext";
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
  projectId: string;
  onOpenFiles: () => void;
}

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

export function WorkbenchBrowser({ active, projectId, onOpenFiles }: WorkbenchBrowserProps) {
  const confirm = useConfirmation();
  const dialogOpen = useDialogOpen();
  const { activityOpen, paletteOpen, sidebarCollapsed } = useChrome();
  const desktop = isTauriRuntime();
  const [tabs, setTabs] = useState<BrowserTab[]>(() => [blankTab()]);
  const [activeId, setActiveId] = useState(() => tabs[0].id);
  const [capabilities, setCapabilities] = useState<BrowserCapabilities>();
  const [notice, setNotice] = useState<string>();
  const [error, setError] = useState<string>();
  const toolbarRef = useRef<HTMLDivElement>(null);
  const surfaceRef = useRef<HTMLDivElement>(null);
  const tabsRef = useRef(tabs);
  const activeRef = useRef(activeId);
  tabsRef.current = tabs;
  activeRef.current = activeId;

  const activeTab = tabs.find((tab) => tab.id === activeId) ?? tabs[0];
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
    setTabs((current) => current.map((tab) => tab.id === id ? { ...tab, ...change } : tab));
  }, []);

  const openAddress = useCallback(async (id: string, input: string) => {
    setError(undefined);
    let url: string;
    try { url = normalizeBrowserInput(input); }
    catch (caught) {
      // diagnostic-expected: local operator input validation is presented inline.
      setError(errorMessage(caught)); return;
    }
    const tab = tabsRef.current.find((item) => item.id === id);
    const nextBounds = bounds();
    if (!tab || !nextBounds) return;
    updateTab(id, { address: url, url, loading: true, error: undefined });
    try {
      if (tab.created) await workbenchBrowser.navigate(id, projectId, url);
      else {
        await workbenchBrowser.create(id, projectId, url, nextBounds);
        updateTab(id, { created: true });
      }
    } catch (caught) {
      void logCaughtDiagnostic("interface.workbench_browser.navigation_failed", "The embedded browser could not navigate.", caught, "workbench_browser");
      updateTab(id, { loading: false, error: errorMessage(caught) });
    }
  }, [bounds, projectId, updateTab]);

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
    const next = blankTab();
    setTabs([next]);
    setActiveId(next.id);
    setNotice(undefined);
    setError(undefined);
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
              if (!replace) { await workbenchBrowser.discardDownload(payload.downloadId!, projectId); setNotice(`${payload.filename} was discarded.`); return; }
              result = await workbenchBrowser.importDownload(payload.downloadId!, projectId, true);
            }
            setNotice(`${result.overwritten ? "Replaced" : "Downloaded"} ${result.path} in Project Files.`);
          } catch (caught) {
            void logCaughtDiagnostic("interface.workbench_browser.download_import_failed", "A website download could not be imported into Project Files.", caught, "workbench_browser");
            setError(errorMessage(caught));
          }
        })();
      }),
    ]).then((unlisteners) => { if (disposed) unlisteners.forEach((stop) => stop()); else stops.push(...unlisteners); });
    return () => { disposed = true; stops.forEach((stop) => stop()); };
  }, [addTab, confirm, desktop, projectId, updateTab]);

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
    if (activeTab) void openAddress(activeTab.id, activeTab.address);
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
      setTabs([next]); setActiveId(next.id); setNotice("Project browser data was cleared.");
    } catch (caught) {
      void logCaughtDiagnostic("interface.workbench_browser.clear_failed", "Project browser data could not be cleared.", caught, "workbench_browser");
      setError(errorMessage(caught));
    }
  };

  if (!desktop) return <div className="browser-unavailable empty-state"><Globe2 size={26} /><strong>Browser is available in the Nebula desktop app</strong><p>Native child webviews are intentionally unavailable in the browser-development workspace.</p></div>;

  return (
    <div className="workbench-browser">
      <div className="browser-tab-strip" role="tablist" aria-label="Browser tabs">
        {tabs.map((tab) => <div className={tab.id === activeId ? "browser-tab active" : "browser-tab"} key={tab.id}><button type="button" role="tab" aria-selected={tab.id === activeId} title={tab.title} onClick={() => setActiveId(tab.id)}>{tab.loading ? <LoaderCircle className="spin" size={13} /> : <Globe2 size={13} />}<span>{tab.title}</span></button><button type="button" aria-label={`Close ${tab.title}`} onClick={() => void closeTab(tab.id)}><X size={13} /></button></div>)}
        <button className="browser-new-tab" type="button" aria-label="New browser tab" disabled={tabs.length >= MAX_TABS} onClick={() => addTab()}><Plus size={15} /></button>
      </div>
      <div className="browser-toolbar" ref={toolbarRef}>
        <button type="button" aria-label="Back" disabled={!activeTab?.created} onClick={() => void runControl("back")}><ArrowLeft size={16} /></button>
        <button type="button" aria-label="Forward" disabled={!activeTab?.created} onClick={() => void runControl("forward")}><ArrowRight size={16} /></button>
        <button type="button" aria-label={activeTab?.loading ? "Stop loading" : "Reload"} disabled={!activeTab?.created} onClick={() => void runControl(activeTab?.loading ? "stop" : "reload")}>{activeTab?.loading ? <X size={15} /> : <RefreshCw size={15} />}</button>
        <form onSubmit={submit}><Search size={15} aria-hidden="true" /><label className="sr-only" htmlFor="browser-address">Address or search</label><input id="browser-address" value={activeTab?.address ?? ""} placeholder="Search or enter an address" autoComplete="off" spellCheck={false} onChange={(event) => activeTab && updateTab(activeTab.id, { address: event.target.value })} /></form>
        <button type="button" aria-label="Clear Project browser data" title="Clear Project browser data" onClick={() => void clearData()}><Trash2 size={15} /></button>
      </div>
      {capabilities?.projectStorage === "ephemeral" && <div className="browser-privacy-notice"><ShieldCheck size={14} /> macOS 13 browser data is isolated and cleared when Nebula closes.</div>}
      {error && <div className="browser-notice error" role="alert"><span>{error}</span><button type="button" aria-label="Dismiss browser error" onClick={() => setError(undefined)}><X size={14} /></button></div>}
      {notice && <div className="browser-notice" role="status"><Download size={14} /><span>{notice}</span><button type="button" onClick={onOpenFiles}>Open Files <ExternalLink size={12} /></button><button type="button" aria-label="Dismiss browser notice" onClick={() => setNotice(undefined)}><X size={14} /></button></div>}
      <div className={`browser-surface${activeTab?.created ? " is-live" : ""}`} ref={surfaceRef}>
        {!activeTab?.created && <div className="browser-start"><Globe2 size={34} /><strong>Browse from the Workbench</strong><p>Pages run in an isolated {capabilities?.engine ?? "system webview"} profile for this Project.</p><form onSubmit={submit}><Search size={16} /><input aria-label="Start browsing" autoFocus={active} value={activeTab?.address ?? ""} placeholder="Search or enter an address" onChange={(event) => activeTab && updateTab(activeTab.id, { address: event.target.value })} /><button className="button primary" type="submit">Go</button></form>{activeTab?.error && <small role="alert">{activeTab.error}</small>}</div>}
      </div>
    </div>
  );
}
