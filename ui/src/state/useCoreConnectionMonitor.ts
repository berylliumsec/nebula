import { useEffect } from "react";
import type { ApiClient } from "../api/client";
import type { HealthResponse } from "../api/types";

const HEALTH_INTERVAL_MS = 5_000;
const HEALTH_DEADLINE_MS = 5_000;
const HEALTH_CONFIRMATION_RETRY_MS = 1_000;
const HEALTH_FAILURE_THRESHOLD = 3;

// Reachability is separate from optional features being degraded. Recovery
// re-bootstraps authoritative state; it never replays a mutation or a tool.
export function useCoreConnectionMonitor(
  api: Pick<ApiClient, "health"> | undefined,
  enabled: boolean,
  onHealth: (health: HealthResponse) => void,
  onUnavailable: (message: string) => void,
) {
  useEffect(() => {
    if (!api || !enabled) return;
    let active = true;
    let unavailable = false;
    let pending: AbortController | undefined;
    let deadline: number | undefined;
    let timer: number | undefined;
    let consecutiveFailures = 0;
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
    const check = () => {
      if (!active || pending) return;
      window.clearTimeout(timer);
      if (document.visibilityState === "hidden") {
        schedule();
        return;
      }
      const controller = new AbortController();
      pending = controller;
      const settle = (health?: HealthResponse) => {
        if (!active || pending !== controller) return;
        window.clearTimeout(deadline);
        pending = undefined;
        if (health) {
          consecutiveFailures = 0;
          unavailable = false;
          onHealth(health);
          schedule();
        } else {
          observeFailure();
        }
      };
      // Settle on our own deadline even if the transport ignores cancellation.
      deadline = window.setTimeout(() => { controller.abort(); settle(); }, HEALTH_DEADLINE_MS);
      void api.health(controller.signal).then(
        health => { if (!controller.signal.aborted) settle(health); },
        () => { if (!controller.signal.aborted) settle(); },
      );
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
