import { useEffect } from "react";
import type { ApiClient } from "../api/client";
import { logCaughtDiagnostic } from "../diagnostics";
import { randomId } from "../randomId";

/** Retry a refused or failed hold sooner than a normal renewal. */
const RETRY_MS = 10_000;

function pageHidden(): boolean {
  return typeof document !== "undefined" && document.visibilityState === "hidden";
}

/**
 * While a project view is open, hold a lease that tells Core the project is in
 * use, so an idle project's worker starts and its views load. Renewed while the
 * page is visible; a hidden page lets it lapse (Core retires the worker after
 * its idle grace) and takes it again on return. Released on leaving the project
 * and when the page itself goes away (closed, reloaded or navigated off).
 */
export function useProjectViewLease(api: Pick<ApiClient, "holdProjectView" | "releaseProjectView" | "projectRuntime"> | null | undefined, projectId: string | undefined): void {
  useEffect(() => {
    if (!api || !projectId || typeof api.holdProjectView !== "function") return;
    const leaseId = `view-${randomId().replace(/[^A-Za-z0-9_-]/g, "").slice(0, 60)}`;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    let holding = false;
    const schedule = (delay: number) => {
      if (timer !== undefined) clearTimeout(timer);
      timer = setTimeout(() => { timer = undefined; void hold(); }, delay);
    };
    const hold = async () => {
      if (controller.signal.aborted || holding || pageHidden()) return;
      holding = true;
      let next = RETRY_MS;
      try {
        const lease = await api.holdProjectView(projectId, leaseId, controller.signal);
        next = Math.max(5, lease.renewAfterSeconds) * 1000;
        // The worker may be on its way: waiting views read again soon.
        api.projectRuntime?.nudge();
      } catch (error) {
        if (controller.signal.aborted) return;
        void logCaughtDiagnostic("interface.project_view.lease_failed", "This project view could not tell Core it is open; it retries shortly.", error, "project-view");
      } finally {
        holding = false;
      }
      if (!controller.signal.aborted && !pageHidden()) schedule(next);
    };
    const visibility = () => {
      if (pageHidden()) {
        if (timer !== undefined) clearTimeout(timer);
        timer = undefined;
        return;
      }
      void hold();
    };
    const release = () => {
      void api.releaseProjectView(projectId, leaseId).catch((error: unknown) => {
        // diagnostic-expected: the lease lapses on its own when the release cannot run.
        void logCaughtDiagnostic("interface.project_view.release_failed", "A closed project view's lease could not be released; it lapses on its own.", error, "project-view");
      });
    };
    // A page that goes away never unmounts; end its lease now (keepalive) so
    // the idle grace starts at once. A page restored from cache holds it again.
    const pageHide = () => {
      if (timer !== undefined) clearTimeout(timer);
      timer = undefined;
      release();
    };
    const pageShow = (event: PageTransitionEvent) => { if (event.persisted) void hold(); };
    void hold();
    document.addEventListener("visibilitychange", visibility);
    window.addEventListener("pagehide", pageHide);
    window.addEventListener("pageshow", pageShow);
    return () => {
      controller.abort();
      if (timer !== undefined) clearTimeout(timer);
      document.removeEventListener("visibilitychange", visibility);
      window.removeEventListener("pagehide", pageHide);
      window.removeEventListener("pageshow", pageShow);
      release();
    };
  }, [api, projectId]);
}
