import type { BrowserScopeDecision } from "../api/workbenchBrowser";

export interface ProxyScopeSignal { blocked?: boolean; error?: string; statusCode?: number; }
export function proxyScopeSignal(event: ProxyScopeSignal): string | null | undefined {
  if (event.blocked && /no compiled Project scope is active/i.test(event.error ?? "")) return event.error;
  if (!event.blocked && event.statusCode !== undefined && event.statusCode >= 200 && event.statusCode < 400) return null;
  return undefined;
}
export function browserScopeStatus(decision: BrowserScopeDecision, nativeScopeError?: string | null): BrowserScopeDecision {
  if (nativeScopeError) return {
    state: "inactive", label: "Browser scope unavailable",
    detail: "The native proxy has no active scope for this browser session. Project permissions alone do not mean that navigation is ready.",
  };
  return decision.label === "All targets" ? {
    ...decision, state: "unknown", label: "Project: all targets",
    detail: `${decision.detail} This describes Project permissions, not native browser readiness.`,
  } : decision;
}
