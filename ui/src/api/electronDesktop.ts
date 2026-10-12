export interface ElectronTab {
  id: string;
  url: string;
  title: string;
  loading: boolean;
  error: string | null;
}

export interface ElectronBrowserState {
  projectId: string | null;
  activeTabId: string | null;
  tabs: ElectronTab[];
}

export interface ElectronRect { x: number; y: number; width: number; height: number }
export interface ElectronLayout {
  viewport: ElectronRect;
  panel: ElectronRect;
  controls: ElectronRect;
  page: ElectronRect;
  dialogOpen: boolean;
}

export interface ElectronDesktopBridge {
  kind: "electron";
  resolveBackendConnection(): Promise<{ endpoint: string; token: string; source: "local"; protocol: string }>;
  deviceId(): Promise<string>;
  setZoom(scale: number): Promise<number>;
  browser: {
    open(projectId: string): Promise<ElectronBrowserState>;
    newTab(projectId: string, url?: string): Promise<ElectronBrowserState>;
    selectTab(projectId: string, tabId: string): Promise<ElectronBrowserState>;
    closeTab(projectId: string, tabId: string): Promise<ElectronBrowserState>;
    navigate(projectId: string, tabId: string, url: string): Promise<ElectronBrowserState>;
    command(projectId: string, tabId: string, action: "back" | "forward" | "reload"): Promise<ElectronBrowserState>;
    layout(value: ElectronLayout | null): Promise<void>;
    onState(callback: (state: ElectronBrowserState) => void): () => void;
  };
}

declare global {
  interface Window { nebulaDesktop?: ElectronDesktopBridge }
}

export function electronDesktop(): ElectronDesktopBridge | undefined {
  return typeof window === "undefined" ? undefined : window.nebulaDesktop;
}
