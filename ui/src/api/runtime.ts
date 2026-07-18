import { invoke } from "@tauri-apps/api/core";
import { configureBrowserDiagnostics, logDiagnostic } from "../diagnostics";

export interface ApiRuntime {
  baseUrl?: string;
  token?: string;
  mode: "browser" | "desktop";
  state: "ready" | "unavailable";
  message?: string;
  reason?: "browser_session_token_missing";
}

interface BackendSession {
  endpoint: string;
  token: string;
  protocol: "nebula-sidecar-v1";
}

export function browserSessionRequiresRelaunch(
  token: string | undefined,
  development: boolean,
): boolean {
  return !token && !development;
}

let browserRuntimeToken: string | undefined;

function consumeBrowserFragmentToken(): string | undefined {
  if (typeof window === "undefined") return browserRuntimeToken;
  const fragment = window.location.hash.replace(/^#/, "");
  if (!fragment) return browserRuntimeToken;

  const parameters = new URLSearchParams(fragment);
  const suppliedToken = parameters.get("token")?.trim();
  if (!suppliedToken) return browserRuntimeToken;

  browserRuntimeToken = suppliedToken;
  parameters.delete("token");
  const remainingFragment = parameters.toString();
  const cleanUrl = `${window.location.pathname}${window.location.search}${remainingFragment ? `#${remainingFragment}` : ""}`;
  window.history.replaceState(window.history.state, "", cleanUrl);
  return browserRuntimeToken;
}

export function isTauriRuntime(): boolean {
  return typeof window !== "undefined" && "__TAURI_INTERNALS__" in window;
}

export async function resolveApiRuntime(): Promise<ApiRuntime> {
  if (isTauriRuntime()) {
    try {
      const session = await invoke<BackendSession>("start_local_backend");
      void logDiagnostic({
        level: "info",
        eventCode: "interface.runtime.desktop_ready",
        message: "The interface connected to the supervised local Core.",
        outcome: "success",
        stage: "runtime-resolution",
      });
      return {
        baseUrl: session.endpoint,
        token: session.token,
        mode: "desktop",
        state: "ready",
      };
    } catch (error) {
      void logDiagnostic({
        level: "error",
        eventCode: "interface.runtime.desktop_unavailable",
        message: "The interface could not connect to the supervised local Core.",
        outcome: "failure",
        stage: "runtime-resolution",
        retryable: true,
        safeFailureCause: "The local Core did not become ready.",
        exception: error,
      });
      return {
        mode: "desktop",
        state: "unavailable",
        message: error instanceof Error ? error.message : String(error),
      };
    }
  }

  const baseUrl = import.meta.env.VITE_NEBULA_API_URL;
  const token = consumeBrowserFragmentToken() ?? import.meta.env.VITE_NEBULA_API_TOKEN;
  const normalizedBase = (baseUrl?.trim() || globalThis.location?.origin || "http://127.0.0.1")
    .replace(/\/+$/, "");
  if (browserSessionRequiresRelaunch(token, import.meta.env.DEV)) {
    return {
      baseUrl,
      mode: "browser",
      state: "unavailable",
      reason: "browser_session_token_missing",
      message: "This browser session no longer has its one-time Core token.",
    };
  }
  configureBrowserDiagnostics(
    normalizedBase.endsWith("/api/v1") ? normalizedBase : `${normalizedBase}/api/v1`,
    token,
  );
  void logDiagnostic({
    level: "info",
    eventCode: "interface.runtime.browser_ready",
    message: "The browser-development API runtime was resolved.",
    outcome: "success",
    stage: "runtime-resolution",
  });
  return {
    baseUrl,
    token,
    mode: "browser",
    state: "ready",
  };
}
