import { invoke } from "@tauri-apps/api/core";
import { isTauriRuntime } from "./runtime";

export interface ReleaseInfo {
  version: string;
  commit: string;
  buildTarget: string;
  builtAt: string;
  distribution: "direct" | "managed" | "development" | string;
  updateChannel?: "stable" | "prerelease" | string;
  updaterEnabled: boolean;
}

export interface AvailableUpdate {
  currentVersion: string;
  version: string;
  notes?: string;
  publishedAt?: string;
}

const browserRelease: ReleaseInfo = {
  version: "browser",
  commit: "development",
  buildTarget: "browser",
  builtAt: "unknown",
  distribution: "development",
  updaterEnabled: false,
};

export async function getReleaseInfo(): Promise<ReleaseInfo> {
  if (!isTauriRuntime()) return browserRelease;
  return invoke<ReleaseInfo>("release_info");
}

export async function checkForUpdate(): Promise<AvailableUpdate | undefined> {
  if (!isTauriRuntime()) return undefined;
  return (await invoke<AvailableUpdate | null>("check_for_update")) ?? undefined;
}

export async function installAvailableUpdate(): Promise<boolean> {
  if (!isTauriRuntime()) return false;
  return invoke<boolean>("install_available_update");
}
