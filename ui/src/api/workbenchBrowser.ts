import { invoke } from "@tauri-apps/api/core";

export interface BrowserBounds { x: number; y: number; width: number; height: number; scaleFactor: number }
export interface BrowserCapabilities { engine: string; projectStorage: "persistent" | "ephemeral" }
export interface BrowserPageEvent {
  tabId: string;
  url: string;
  state: "loading" | "loaded" | "title" | "new_tab" | "blocked";
  title?: string;
  detail?: string;
}
export interface BrowserDownloadEvent {
  tabId: string;
  downloadId?: string;
  filename?: string;
  size?: number;
  state: "ready" | "failed" | "rejected";
  detail?: string;
}
export interface BrowserImportResult {
  state: "imported" | "conflict";
  path: string;
  size: number;
  sha256?: string;
  overwritten: boolean;
  detail?: string;
}

export function normalizeBrowserInput(value: string): string {
  const input = value.trim();
  if (!input) throw new Error("Enter an address or search terms.");
  if (/^https?:\/\//i.test(input)) return new URL(input).toString();
  if (/^[\w.-]+(?::\d+)?(?:\/[^\s]*)?$/.test(input) && (input.includes(".") || input.startsWith("localhost") || /^\d{1,3}(?:\.\d{1,3}){3}/.test(input))) {
    return new URL(`https://${input}`).toString();
  }
  if (/^[a-z][a-z0-9+.-]*:/i.test(input)) throw new Error("Nebula Browser permits only HTTP and HTTPS addresses.");
  return `https://duckduckgo.com/?q=${encodeURIComponent(input)}`;
}

export const workbenchBrowser = {
  capabilities: () => invoke<BrowserCapabilities>("browser_capabilities"),
  create: (tabId: string, projectId: string, url: string, bounds: BrowserBounds) => invoke<void>("browser_create_tab", { tabId, projectId, url, bounds }),
  navigate: (tabId: string, projectId: string, url: string) => invoke<void>("browser_navigate", { tabId, projectId, url }),
  control: (tabId: string, projectId: string, action: "back" | "forward" | "stop" | "reload") => invoke<void>("browser_control", { tabId, projectId, action }),
  bounds: (tabId: string, projectId: string, bounds: BrowserBounds) => invoke<void>("browser_set_bounds", { tabId, projectId, bounds }),
  visible: (tabId: string, projectId: string, visible: boolean) => invoke<void>("browser_set_visible", { tabId, projectId, visible }),
  close: (tabId: string, projectId: string) => invoke<void>("browser_close_tab", { tabId, projectId }),
  clear: (projectId: string) => invoke<void>("browser_clear_project_data", { projectId }),
  importDownload: (downloadId: string, projectId: string, overwrite: boolean) => invoke<BrowserImportResult>("browser_import_download", { downloadId, projectId, overwrite }),
  discardDownload: (downloadId: string, projectId: string) => invoke<void>("browser_discard_download", { downloadId, projectId }),
};
