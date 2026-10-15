import { useCallback, useEffect, useRef, useState } from "react";
import type { ApiClient } from "../api/client";
import { butlerResourceKey, getButlerOverview, type ButlerOverview } from "../api/butlerOverview";
import { logCaughtDiagnostic } from "../diagnostics";

type OverviewState = {
  projectId: string;
  overview?: ButlerOverview;
  loading: boolean;
  loadingMore?: "watch" | "incident" | "action" | "resource";
  error?: string;
};

/** Core owns oversight; this hook retains only the last snapshot for one project. */
export function useButlerOverview(api: ApiClient | undefined, projectId: string, coreState: "checking" | "online" | "offline") {
  const [state, setState] = useState<OverviewState>({ projectId, loading: true });
  const request = useRef<AbortController | undefined>(undefined);

  const refresh = useCallback(async () => {
    request.current?.abort();
    if (!api || coreState !== "online") {
      setState((current) => ({
        projectId,
        overview: current.projectId === projectId ? current.overview : undefined,
        loading: false,
        error: api ? undefined : "Core is unavailable. Reconnect and retry.",
      }));
      return;
    }
    const controller = new AbortController();
    request.current = controller;
    setState((current) => ({
      projectId,
      overview: current.projectId === projectId ? current.overview : undefined,
      loading: true,
    }));
    try {
      const overview = await getButlerOverview(api, projectId, controller.signal);
      if (!controller.signal.aborted) setState({ projectId, overview, loading: false });
    } catch (caught) {
      if (controller.signal.aborted) return;
      void logCaughtDiagnostic("interface.butler_oversight.load_failed", "Butler oversight could not be loaded.", caught, "butler_oversight");
      setState((current) => ({
        projectId,
        overview: current.projectId === projectId ? current.overview : undefined,
        loading: false,
        error: caught instanceof Error ? caught.message : "Could not load Butler oversight.",
      }));
    } finally {
      if (request.current === controller) request.current = undefined;
    }
  }, [api, projectId, coreState]);

  useEffect(() => {
    void refresh();
    return () => { request.current?.abort(); };
  }, [refresh]);

  useEffect(() => {
    const resume = () => { if (document.visibilityState === "visible") void refresh(); };
    document.addEventListener("visibilitychange", resume);
    window.addEventListener("online", resume);
    return () => {
      document.removeEventListener("visibilitychange", resume);
      window.removeEventListener("online", resume);
    };
  }, [refresh]);

  // Core owns Watch and its event stream. Until live project events are exposed,
  // refresh the visible panel at a bounded interval so other agents' work appears.
  useEffect(() => {
    if (!api || coreState !== "online") return;
    const interval = window.setInterval(() => {
      if (document.visibilityState === "visible") void refresh();
    }, 15_000);
    return () => window.clearInterval(interval);
  }, [api, coreState, refresh]);

  const loadMore = useCallback(async (kind: "watch" | "incident" | "action" | "resource") => {
    const current = state.projectId === projectId ? state.overview : undefined;
    const offset = kind === "watch" ? current?.nextWatchOffset : kind === "incident" ? current?.nextIncidentOffset
      : kind === "action" ? current?.nextActionOffset : current?.nextResourceOffset;
    if (!api || coreState !== "online" || !current || offset === undefined || state.loading || state.loadingMore) return;
    request.current?.abort();
    const controller = new AbortController();
    request.current = controller;
    setState((previous) => ({ ...previous, loadingMore: kind, error: undefined }));
    try {
      const next = await getButlerOverview(api, projectId, controller.signal,
        kind === "watch" ? { watch: offset } : kind === "incident" ? { incident: offset }
          : kind === "action" ? { action: offset } : { resource: offset });
      if (!controller.signal.aborted) setState((previous) => {
        if (previous.projectId !== projectId || !previous.overview) return previous;
        const before = previous.overview;
        return { projectId, loading: false, overview: {
          ...next,
          watchedGoals: kind === "watch"
            ? [...new Map([...before.watchedGoals, ...next.watchedGoals].map((goal) => [goal.goalId, goal])).values()]
            : before.watchedGoals,
          incidents: kind === "incident"
            ? [...new Map([...before.incidents, ...next.incidents].map((incident) => [incident.id, incident])).values()]
            : before.incidents,
          actions: kind === "action"
            ? [...new Map([...before.actions, ...next.actions].map((action) => [action.id, action])).values()]
            : before.actions,
          resources: kind === "resource"
            ? [...new Map([...before.resources, ...next.resources].map((resource) => [butlerResourceKey(resource), resource])).values()]
            : before.resources,
          nextWatchOffset: kind === "watch" ? next.nextWatchOffset : before.nextWatchOffset,
          nextIncidentOffset: kind === "incident" ? next.nextIncidentOffset : before.nextIncidentOffset,
          nextActionOffset: kind === "action" ? next.nextActionOffset : before.nextActionOffset,
          nextResourceOffset: kind === "resource" ? next.nextResourceOffset : before.nextResourceOffset,
        } };
      });
    } catch (caught) {
      if (controller.signal.aborted) return;
      void logCaughtDiagnostic("interface.butler_oversight.more_failed", "More Butler oversight records could not be loaded.", caught, "butler_oversight");
      setState((previous) => previous.projectId === projectId ? {
        ...previous, loadingMore: undefined,
        error: caught instanceof Error ? caught.message : "Could not load more oversight records.",
      } : previous);
    } finally {
      if (request.current === controller) request.current = undefined;
    }
  }, [api, projectId, coreState, state]);

  const current: OverviewState = state.projectId === projectId ? state : { projectId, loading: coreState === "online" };
  return { overview: current.overview, loading: current.loading, loadingMore: current.loadingMore, error: current.error, refresh, loadMore };
}
