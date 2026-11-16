import { useLayoutEffect } from "react";
import type { ApiClient } from "../api/client";
import type { HealthResponse } from "../api/types";

const HEALTH_INTERVAL_MS = 5_000;
const HEALTH_DEADLINE_MS = 5_000;
const HEALTH_CONFIRMATION_RETRY_MS = 1_000;
const HEALTH_FAILURE_THRESHOLD = 3;
// Full health reads a database session and asks the API worker for its
// identity. Reachability does not need that, so the full read only refreshes
// reported health, and runs often only while Core reports its API not ready.
const FULL_HEALTH_INTERVAL_MS = 30_000;

type MonitoredApi = Pick<ApiClient, "health"> & Partial<Pick<ApiClient, "liveness" | "lastReachableAt">>;

// Reachability is separate from optional features being degraded. Recovery
// re-bootstraps authoritative state; it never replays a mutation or a tool.
export function useCoreConnectionMonitor(
  api: MonitoredApi | undefined,
  enabled: boolean,
  onHealth: (health: HealthResponse) => void,
  onUnavailable: (message: string) => void,
) {
  useLayoutEffect(() => {
    if (!api || !enabled) return;
    let active = true;
    let unavailable = false;
    let pending: AbortController | undefined;
    let deadline: number | undefined;
    let timer: number | undefined;
    let consecutiveFailures = 0;
    // Bootstrap just read full health.
    let lastFullHealth = Date.now();
    let apiNotReady = false;
    const fail = () => {
      if (!active || unavailable) return;
      unavailable = true;
      onUnavailable("Connection to Nebula Core was lost. Your current view and unsent input remain here. Reconnect to refresh saved state; no action will be replayed.");
    };
    const schedule = (delay = HEALTH_INTERVAL_MS) => {
      if (active) timer = window.setTimeout(check, delay);
    };
    const observeFailure = () => {
      consecutiveFailures += 1;
      if (unavailable || consecutiveFailures >= HEALTH_FAILURE_THRESHOLD) {
        fail();
        schedule();
        return;
      }
      // A single slow or dropped LAN request is weak evidence of a Core outage.
      // Confirm it promptly before replacing the operator's workspace.
      schedule(HEALTH_CONFIRMATION_RETRY_MS);
    };
    const observeReachable = () => {
      consecutiveFailures = 0;
      schedule();
    };
    // Run one probe under our own deadline, settling even if the transport
    // ignores cancellation. A superseded or late answer is ignored.
    const probe = <T,>(run: (signal: AbortSignal) => Promise<T>, settle: (value: T | undefined) => void) => {
      const controller = new AbortController();
      pending = controller;
      const finish = (value?: T) => {
        if (!active || pending !== controller) return;
        window.clearTimeout(deadline);
        pending = undefined;
        settle(value);
      };
      deadline = window.setTimeout(() => { controller.abort(); finish(); }, HEALTH_DEADLINE_MS);
      void run(controller.signal).then(
        value => { if (!controller.signal.aborted) finish(value); },
        () => { if (!controller.signal.aborted) finish(); },
      );
    };
    const fullHealth = (onFailure: () => void) => probe(signal => api.health(signal), health => {
      if (!health) {
        onFailure();
        return;
      }
      consecutiveFailures = 0;
      unavailable = false;
      lastFullHealth = Date.now();
      apiNotReady = health.apiReady === false;
      onHealth(health);
      schedule();
    });
    const liveness = () => {
      if (!api.liveness) return fullHealth(observeFailure);
      probe(async signal => { await api.liveness?.(signal); return true; }, ok => ok ? observeReachable() : observeFailure());
    };
    const check = () => {
      if (!active || pending) return;
      window.clearTimeout(timer);
      if (document.visibilityState === "hidden") {
        schedule();
        return;
      }
      // Recovery needs full health: reconnecting re-bootstraps saved state.
      if (unavailable || !api.liveness) return fullHealth(observeFailure);
      const now = Date.now();
      if (apiNotReady || now - lastFullHealth >= FULL_HEALTH_INTERVAL_MS) {
        // A busy Core can miss the full read's deadline while still answering;
        // only a failed liveness probe counts toward loss. Pace attempts, not
        // successes, so a slow Core is not asked for full health every check.
        lastFullHealth = now;
        return fullHealth(liveness);
      }
      // Any answer to the workspace's own requests already proves reachability.
      if (now - (api.lastReachableAt?.() ?? 0) < HEALTH_INTERVAL_MS) return observeReachable();
      liveness();
    };
    const offline = () => {
      pending?.abort();
      pending = undefined;
      window.clearTimeout(deadline);
      window.clearTimeout(timer);
      consecutiveFailures = HEALTH_FAILURE_THRESHOLD;
      fail();
      schedule();
    };
    // Bootstrap already checked health. Avoid a duplicate initial request.
    schedule();
    window.addEventListener("offline", offline);
    window.addEventListener("online", check);
    document.addEventListener("visibilitychange", check);
    return () => {
      active = false;
      pending?.abort();
      window.clearTimeout(timer);
      window.clearTimeout(deadline);
      window.removeEventListener("offline", offline);
      window.removeEventListener("online", check);
      document.removeEventListener("visibilitychange", check);
    };
  }, [api, enabled, onHealth, onUnavailable]);
}
