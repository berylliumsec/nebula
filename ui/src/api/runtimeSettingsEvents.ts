/** Core receipts invalidate catalogs; forms retain the revision they actually read. */
export const RUNTIME_SETTINGS_CHANGED_EVENT = "nebula:runtime-settings-changed";
export type RuntimeSettingsResource = "runner-profiles" | "ssh-environments";

export function notifyRuntimeSettingsChanged(resource: RuntimeSettingsResource) {
  window.dispatchEvent(new CustomEvent(RUNTIME_SETTINGS_CHANGED_EVENT, {detail: resource}));
}
