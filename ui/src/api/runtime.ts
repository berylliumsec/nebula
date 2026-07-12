import { invoke } from "@tauri-apps/api/core";

export interface ApiRuntime {
  baseUrl?: string;
  token?: string;
  mode: "browser" | "desktop";
  state: "ready" | "unavailable";
  message?: string;
}

interface BackendSession {
  endpoint: string;
  token: string;
  protocol: "nebula-sidecar-v1";
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
      return {
        baseUrl: session.endpoint,
        token: session.token,
        mode: "desktop",
        state: "ready",
      };
    } catch (error) {
      return {
        mode: "desktop",
        state: "unavailable",
        message: error instanceof Error ? error.message : String(error),
      };
    }
  }

  return {
    baseUrl: import.meta.env.VITE_NEBULA_API_URL,
    token: consumeBrowserFragmentToken() ?? import.meta.env.VITE_NEBULA_API_TOKEN,
    mode: "browser",
    state: "ready",
  };
}
