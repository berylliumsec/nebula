import type { ApiClient } from "../api/client";
import type { StorageSource } from "../api/types";
import { logCaughtDiagnostic } from "../diagnostics";
import type { SettingClientAction } from "./nebulaAssistantActions";

type StorageAction = Extract<SettingClientAction, {kind: "storage_enroll" | "storage_rotate" | "storage_revoke" | "storage_configure_reporting" | "storage_edit_callbacks"}>;

export interface IssuedStorageToken {
  projectId: string;
  name: string;
  token: string;
  webhookUrl: string;
}

export interface StorageActionResult {
  message: string;
  issued?: IssuedStorageToken;
}

/** Perform only the exact browser-owned source action Butler prepared. */
export async function applyButlerStorageAction(
  api: ApiClient,
  action: StorageAction,
  selectedProject: () => string | undefined,
  confirm: (request: {title: string; message: string; confirmLabel: string; tone: "danger"}) => Promise<boolean>,
  openEnrollment: () => void,
  openCallbackSettings?: (source: StorageSource) => void,
): Promise<StorageActionResult> {
  if (selectedProject() !== action.projectId) {
    throw new Error("Select the original project before changing its storage sources.");
  }
  if (action.kind === "storage_enroll") {
    openEnrollment();
    return {message: "Opened Storage enrollment. Choose the runtime and complete the form on this device; the one-time token stays in this browser. No source has been added yet."};
  }
  const readSource = async (): Promise<StorageSource> => {
    const dashboard = await api.storageDashboard();
    const source = dashboard.sources.find((item) => item.id === action.sourceId);
    if (!source || source.engagement_id !== action.projectId) {
      throw new Error("The storage source is no longer in the selected project. Ask Butler to list sources again.");
    }
    return source;
  };
  const source = await readSource();
  if (source.revision !== action.expectedRevision || source.name !== action.name) {
    throw new Error("The storage source changed. Ask Butler to read its current revision before trying again.");
  }
  if (action.kind === "storage_edit_callbacks") {
    if (!openCallbackSettings) throw new Error("Storage callback settings are unavailable on this device.");
    openCallbackSettings(source);
    return {message: `Opened ${source.name}'s callback settings on this device. Review and save the origin and credential reference there; no settings have changed yet.`};
  }
  if (action.kind === "storage_configure_reporting") {
    const desired = action.changes;
    const description = [
      ...(desired.enabled === undefined ? [] : [`${desired.enabled ? "Enable" : "Disable"} reports and alerts`]),
      ...(desired.reporting_interval_seconds === undefined ? [] : [`Set the reporting interval to ${desired.reporting_interval_seconds} seconds`]),
    ].join("; ");
    const approved = await confirm({title: `Change ${source.name}'s storage reporting?`,
      message: `${description}. Core will validate the source runtime and save only if revision ${source.revision} is current.`,
      confirmLabel: "Save reporting", tone: "danger"});
    if (!approved) return {message: `${source.name}'s reporting settings were not changed.`};
    if (selectedProject() !== action.projectId) throw new Error("The selected project changed. Ask Butler to read the storage source again.");
    const current = await readSource();
    if (current.revision !== action.expectedRevision || current.name !== action.name) {
      throw new Error("The storage source changed during confirmation. Ask Butler to read its current revision.");
    }
    const changed = await api.updateStorageSource(current, desired);
    if (selectedProject() !== action.projectId) {
      throw new Error("The selected project changed after the reporting update. Open Storage to inspect the source before retrying.");
    }
    try {
      const observed = await readSource();
      if (observed.revision !== changed.revision
          || (desired.enabled !== undefined && observed.enabled !== desired.enabled)
          || (desired.reporting_interval_seconds !== undefined
              && observed.reporting_interval_seconds !== desired.reporting_interval_seconds)) {
        return {message: `Core accepted ${source.name}'s reporting update, but its saved settings could not be confirmed. Inspect Storage before retrying.`};
      }
      return {message: `Core saved ${source.name}'s reporting settings at revision ${observed.revision}: ${description}.`};
    } catch (caught) {
      logCaughtDiagnostic("interface.storage.reporting_readback_failed", "The storage reporting update could not be confirmed.", caught, "storage-settings");
      return {message: `Core accepted ${source.name}'s reporting update, but readback failed. Inspect Storage before retrying.`};
    }
  }
  const rotation = action.kind === "storage_rotate";
  const approved = await confirm({
    title: `${rotation ? "Rotate" : "Revoke"} ${source.name}'s storage token?`,
    message: rotation
      ? "The current sender token will stop working. Save the new one-time token on this device and update the sender before its next report."
      : "This disables reports and alerts from this source. Its current sender token will stop working.",
    confirmLabel: rotation ? "Rotate token" : "Revoke token",
    tone: "danger",
  });
  if (!approved) return {message: `${source.name}'s token was not ${rotation ? "rotated" : "revoked"}.`};
  if (selectedProject() !== action.projectId) {
    throw new Error("The selected project changed. Ask Butler to read the storage source again.");
  }
  const current = await readSource();
  if (current.revision !== action.expectedRevision || current.name !== action.name) {
    throw new Error("The storage source changed during confirmation. Ask Butler to read its current revision.");
  }
  if (rotation) {
    const changed = await api.rotateStorageSource(current);
    if (selectedProject() !== action.projectId) {
      throw new Error("The selected project changed during token rotation. Open Storage to inspect the source before retrying.");
    }
    const issued = {
      projectId: action.projectId,
      name: source.name,
      token: changed.token,
      webhookUrl: new URL(`${api.baseUrl.replace(/\/$/, "")}/storage/events`, window.location.href).toString(),
    };
    try {
      const observed = await readSource();
      if (observed.revision !== changed.source.revision || observed.enabled !== changed.source.enabled) {
        return {message: `Core returned a new token for ${source.name}, but its saved status could not be confirmed. Save the token shown on this device and inspect Storage before retrying.`, issued};
      }
    } catch (caught) {
      logCaughtDiagnostic("interface.storage.token_rotation_readback_failed", "The storage token rotation could not be confirmed.", caught, "storage-settings");
      return {message: `Core returned a new token for ${source.name}, but readback failed. Save the token shown on this device and inspect Storage before retrying.`, issued};
    }
    return {message: `Rotated ${source.name}'s token in Core at revision ${changed.source.revision}. Save the new token shown on this device and update the sender.`, issued};
  }
  const changed = await api.revokeStorageSource(current);
  if (selectedProject() !== action.projectId) {
    throw new Error("The selected project changed during token revocation. Open Storage to inspect the source.");
  }
  try {
    const observed = await readSource();
    if (observed.revision !== changed.revision || observed.enabled) {
      return {message: `Core accepted ${source.name}'s revocation, but its saved status could not be confirmed. Inspect Storage before retrying.`};
    }
  } catch (caught) {
    logCaughtDiagnostic("interface.storage.source_revoke_readback_failed", "The storage source revocation could not be confirmed.", caught, "storage-settings");
    return {message: `Core accepted ${source.name}'s revocation, but readback failed. Inspect Storage before retrying.`};
  }
  return {message: `Revoked ${source.name}'s token and confirmed that Core disabled its reports.`};
}
