import type {ApiClient} from "../api/client";
import {logCaughtDiagnostic} from "../diagnostics";
import type {SettingClientAction} from "./nebulaAssistantActions";

type DeleteAction = Extract<SettingClientAction, {kind: "credential_delete"}>;

/** Read an exact managed reference on the requesting browser before deleting it. */
export async function deleteButlerCredential(
  api: Pick<ApiClient, "credentialStatus" | "deleteCredential">,
  action: DeleteAction,
  selectedProject: () => string | undefined,
  confirm: (request: {title: string; message: string; confirmLabel: string; tone: "danger"}) => Promise<boolean>,
): Promise<string> {
  if (selectedProject() !== action.projectId) throw new Error("Select the original project before deleting a credential.");
  const current = await api.credentialStatus(action.reference);
  if (current.reference !== action.reference || !current.available || current.state !== "available") {
    throw new Error("This credential is unavailable or changed. Read its current status before retrying.");
  }
  const approved = await confirm({title: "Delete credential?",
    message: `Delete ${action.reference}? Providers or integrations using it may stop working immediately.`,
    confirmLabel: "Delete credential", tone: "danger"});
  if (!approved) return `${action.reference} was not deleted.`;
  if (selectedProject() !== action.projectId) throw new Error("The selected project changed. Read the credential again.");
  const latest = await api.credentialStatus(action.reference);
  if (latest.reference !== action.reference || !latest.available || latest.state !== "available"
      || latest.persistence !== current.persistence) throw new Error("The credential changed before deletion. Read it again.");
  await api.deleteCredential(action.reference);
  try {
    const observed = await api.credentialStatus(action.reference);
    if (observed.reference === action.reference && !observed.available
        && (observed.state === "missing" || observed.state === "session_expired")) {
      return `Deleted ${action.reference}; Core confirms it is unavailable. Profiles using it may need a replacement.`;
    }
  } catch (caught) {
    logCaughtDiagnostic("interface.butler_credential.delete_readback_failed", "Credential deletion could not be confirmed.", caught, "credentials");
  }
  return `Core accepted deletion of ${action.reference}, but its absence could not be confirmed. Inspect credential status before retrying.`;
}
