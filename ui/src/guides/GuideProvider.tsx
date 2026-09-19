import { createContext, type PropsWithChildren, useCallback, useContext, useEffect, useMemo, useRef, useState } from "react";
import { useLocation } from "react-router-dom";
import { ApiError } from "../api/client";
import type { GuideProgress, GuideProgressStatus } from "../api/types";
import { logCaughtDiagnostic } from "../diagnostics";
import { useWorkspace } from "../state/WorkspaceContext";
import { guideById, guideCatalog } from "./catalog";
import { GuideOverlay } from "./GuideOverlay";
import { GuidesDrawer } from "./GuidesDrawer";
import type { GuideDefinition, GuidePage } from "./types";

export interface ActiveGuide {
  guide: GuideDefinition;
  stepIndex: number;
  minimized: boolean;
  values: Record<string, string>;
}

interface GuidesContextValue {
  guides: GuideDefinition[];
  page: GuidePage;
  progress: Record<string, GuideProgress>;
  progressState: "loading" | "ready" | "failed";
  progressError?: string;
  hubOpen: boolean;
  active?: ActiveGuide;
  openHub(): void;
  closeHub(): void;
  retryProgress(): void;
  /** Start a guide, or resume it where the operator left off when no step is given. */
  start(guideId: string, stepIndex?: number): void;
  restart(guideId: string): void;
}

const GuidesContext = createContext<GuidesContextValue | undefined>(undefined);

export function pageForPath(pathname: string): GuidePage {
  if (pathname.startsWith("/settings")) return "settings";
  if (/^\/projects\/[^/]+\/workbench/.test(pathname) || pathname === "/") return "workbench";
  return "project";
}

export function GuideProvider({ children }: PropsWithChildren) {
  const { api, workspaceState } = useWorkspace();
  const location = useLocation();
  const [progress, setProgressState] = useState<Record<string, GuideProgress>>({});
  const [progressState, setLoadState] = useState<"loading" | "ready" | "failed">("loading");
  const [progressError, setProgressError] = useState<string>();
  const [hubOpen, setHubOpen] = useState(false);
  const [active, setActive] = useState<ActiveGuide>();
  const progressRef = useRef(progress);
  const saves = useRef(new Map<string, Promise<void>>());
  const connected = Boolean(api) && ["ready", "degraded"].includes(workspaceState);

  const setProgress = useCallback((next: Record<string, GuideProgress>) => {
    progressRef.current = next;
    setProgressState(next);
  }, []);

  const reload = useCallback(async (signal?: AbortSignal) => {
    if (!api || typeof api.listGuideProgress !== "function") return;
    try {
      const items = await api.listGuideProgress(signal);
      if (signal?.aborted) return;
      setProgress(Object.fromEntries(items.map(item => [item.guideId, item])));
      setLoadState("ready");
      setProgressError(undefined);
    } catch (error) {
      if (signal?.aborted) return;
      setLoadState("failed");
      setProgressError("Guide progress could not be loaded from Nebula Core. Guides still work; progress will not resume until Core responds.");
      logCaughtDiagnostic("interface.guides.progress_load_failed", "Guide progress could not be loaded.", error, "guides");
    }
  }, [api, setProgress]);

  useEffect(() => {
    if (!connected) return;
    const controller = new AbortController();
    void reload(controller.signal);
    return () => controller.abort();
  }, [connected, reload]);

  const persist = useCallback((guideId: string, status: GuideProgressStatus, stepIndex: number) => {
    if (!api || typeof api.saveGuideProgress !== "function") return;
    const save = async () => {
      for (let attempt = 0; attempt < 2; attempt += 1) {
        const current = progressRef.current[guideId];
        try {
          const saved = await api.saveGuideProgress(guideId, { status, stepIndex, expectedRevision: current?.revision ?? 0 });
          setProgress({ ...progressRef.current, [guideId]: saved });
          setProgressError(undefined);
          return;
        } catch (error) {
          // Another device moved this guide on; take Core's revision and write once more.
          if (attempt === 0 && error instanceof ApiError && (error.status === 409 || error.status === 404)) {
            await reload();
            continue;
          }
          setProgressError("Guide progress could not be saved to Nebula Core. You can keep going; it may not resume on other devices.");
          logCaughtDiagnostic("interface.guides.progress_save_failed", "Guide progress could not be saved.", error, "guides");
          return;
        }
      }
    };
    const next = (saves.current.get(guideId) ?? Promise.resolve()).then(save);
    saves.current.set(guideId, next);
  }, [api, reload, setProgress]);

  const start = useCallback((guideId: string, stepIndex?: number) => {
    const guide = guideById(guideId);
    if (!guide) return;
    const saved = progressRef.current[guideId];
    const resumeAt = stepIndex ?? (saved?.status === "in_progress" ? saved.stepIndex : 0);
    const index = Math.min(Math.max(resumeAt, 0), guide.steps.length - 1);
    setHubOpen(false);
    setActive({ guide, stepIndex: index, minimized: false, values: {} });
    persist(guideId, "in_progress", index);
  }, [persist]);

  const restart = useCallback((guideId: string) => start(guideId, 0), [start]);

  // Updaters can run twice in development; keep Core writes out of them.
  const activeRef = useRef(active);
  activeRef.current = active;

  const goTo = useCallback((stepIndex: number) => {
    const current = activeRef.current;
    if (!current) return;
    persist(current.guide.id, "in_progress", stepIndex);
    setActive({ ...current, stepIndex, minimized: false });
  }, [persist]);

  const finish = useCallback(() => {
    const current = activeRef.current;
    if (current) persist(current.guide.id, "completed", current.guide.steps.length - 1);
    setActive(undefined);
  }, [persist]);

  const exit = useCallback(() => setActive(undefined), []);
  const setMinimized = useCallback((minimized: boolean) => setActive(current => current && { ...current, minimized }), []);
  const setValue = useCallback((key: string, value: string) => setActive(current => current && { ...current, values: { ...current.values, [key]: value } }), []);
  const openHub = useCallback(() => {
    setHubOpen(true);
    if (connected) void reload();
  }, [connected, reload]);
  const closeHub = useCallback(() => setHubOpen(false), []);
  const retryProgress = useCallback(() => { setLoadState("loading"); void reload(); }, [reload]);

  const page = pageForPath(location.pathname);
  const value = useMemo<GuidesContextValue>(() => ({
    guides: guideCatalog,
    page,
    progress,
    progressState,
    progressError,
    hubOpen,
    active,
    openHub,
    closeHub,
    retryProgress,
    start,
    restart,
  }), [active, closeHub, hubOpen, openHub, page, progress, progressError, progressState, restart, retryProgress, start]);

  return <GuidesContext.Provider value={value}>
    {children}
    {hubOpen && <GuidesDrawer onClose={closeHub} />}
    {active && <GuideOverlay
      key={`${active.guide.id}:${active.stepIndex}`}
      active={active}
      onBack={() => goTo(active.stepIndex - 1)}
      onNext={() => active.stepIndex + 1 < active.guide.steps.length ? goTo(active.stepIndex + 1) : finish()}
      onExit={exit}
      onMinimize={setMinimized}
      onValue={setValue}
    />}
  </GuidesContext.Provider>;
}

export function useGuides(): GuidesContextValue {
  const context = useContext(GuidesContext);
  if (!context) throw new Error("useGuides must be used inside GuideProvider");
  return context;
}

export function useOptionalGuides(): GuidesContextValue | undefined {
  return useContext(GuidesContext);
}
