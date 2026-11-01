import type {ApiClient} from "../api/client";
import type {ProviderHealth} from "../api/types";
import {logCaughtDiagnostic} from "../diagnostics";
import type {SettingClientAction} from "./nebulaAssistantActions";

type AttachAction = Extract<SettingClientAction, {kind: "provider_attach_credential"}>;
type ProviderCredentialApi = Pick<ApiClient, "getProvider" | "credentialStatus" | "attachProviderCredential">;

function slotReference(provider: ProviderHealth, slot: AttachAction["slot"]): string | undefined {
  return slot === "management" ? provider.openrouterManagementSecretRef : provider.credentialRef;
}

/** Confirm and apply one provider credential slot on the requesting browser. */
export async function attachButlerProviderCredential(
  api: ProviderCredentialApi,
  action: AttachAction,
  selectedProject: () => string | undefined,
  confirm: (request: {title: string; message: string; confirmLabel: string}) => Promise<boolean>,
): Promise<string> {
  const selected = () => selectedProject() === action.projectId;
  if (!selected()) throw new Error("Select the original project before attaching a provider credential.");
  const readProvider = async (): Promise<ProviderHealth> => {
    const provider = await api.getProvider(action.providerId);
    if (!selected()) throw new Error("The selected project changed. Read the provider again.");
    if (provider.name !== action.name || provider.revision !== action.expectedRevision) {
      throw new Error("The provider changed. Ask Butler to read its current name and revision.");
    }
    if (action.slot === "management" && provider.providerType !== "openrouter") {
      throw new Error("Only OpenRouter providers have a management credential slot.");
    }
    const other = slotReference(provider, action.slot === "management" ? "inference" : "management");
    if (other === action.reference) {
      throw new Error("Choose a different credential: inference and management must use separate references.");
    }
    return provider;
  };
  const readCredential = async (): Promise<void> => {
    const status = await api.credentialStatus(action.reference);
    if (status.reference !== action.reference || !status.available || status.state !== "available") {
      throw new Error("The credential is unavailable. Check its status or create a replacement before attaching it.");
    }
  };

  const provider = await readProvider();
  await readCredential();
  const slotLabel = action.slot === "management" ? "OpenRouter management" : "model inference";
  const replacing = slotReference(provider, action.slot) ? " This replaces the current reference in that slot." : "";
  const approved = await confirm({
    title: `Attach credential to ${provider.name}?`,
    message: `Attach ${action.reference} for ${slotLabel} on ${provider.name}?${replacing} The secret stays in Core's credential store.`,
    confirmLabel: "Attach credential",
  });
  if (!approved) return `No credential was attached to ${provider.name}.`;
  if (!selected()) throw new Error("The selected project changed. Ask Butler to read the provider again.");
  await readProvider();
  await readCredential();

  let writeFailed = false;
  try {
    await api.attachProviderCredential(action.providerId, action.expectedRevision, action.reference, action.slot);
  } catch (caught) {
    writeFailed = true;
    logCaughtDiagnostic("interface.butler_provider.credential_write_failed", "Provider credential attachment response failed; checking Core before retry.", caught, "providers");
  }
  let observed: ProviderHealth;
  try {
    observed = await api.getProvider(action.providerId);
  } catch (caught) {
    logCaughtDiagnostic("interface.butler_provider.credential_readback_failed", "Provider credential attachment could not be read back.", caught, "providers");
    return `Core did not confirm ${provider.name}'s ${slotLabel} credential. Inspect the provider before asking Butler to retry; the write may have succeeded.`;
  }
  if (!selected()) return `The selected project changed during ${provider.name}'s credential update. Inspect that provider before retrying.`;
  if (observed.revision <= action.expectedRevision || slotReference(observed, action.slot) !== action.reference) {
    if (writeFailed && observed.revision !== action.expectedRevision) {
      throw new Error("The provider changed before the credential could be attached. Ask Butler to read its current state.");
    }
    return `Core did not confirm ${provider.name}'s ${slotLabel} credential. Inspect the provider before retrying.`;
  }
  try {
    await readCredential();
  } catch (caught) {
    logCaughtDiagnostic("interface.butler_provider.credential_status_readback_failed", "Attached credential status could not be confirmed.", caught, "providers");
    return `Core saved ${provider.name}'s ${slotLabel} reference at revision ${observed.revision}, but the credential's availability could not be confirmed. Check its status before use.`;
  }
  return `Attached ${action.reference} to ${observed.name} for ${slotLabel} at Core revision ${observed.revision}. Core confirmed the reference and credential availability. Refresh provider health and tool verification before use.`;
}
