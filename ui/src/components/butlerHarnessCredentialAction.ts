import type {ApiClient} from "../api/client";
import type {HarnessProfile} from "../api/types";
import {logCaughtDiagnostic} from "../diagnostics";
import type {SettingClientAction} from "./nebulaAssistantActions";

type AttachAction = Extract<SettingClientAction, {kind: "harness_attach_credential"}>;
type HarnessCredentialApi = Pick<ApiClient, "getHarness" | "credentialStatus" | "updateHarness">;

/** Confirm an exact harness's authentication on the requesting browser. */
export async function attachButlerHarnessCredential(
  api: HarnessCredentialApi,
  action: AttachAction,
  selectedProject: () => string | undefined,
  confirm: (request: {title: string; message: string; confirmLabel: string}) => Promise<boolean>,
): Promise<string> {
  const selected = () => selectedProject() === action.projectId;
  if (!selected()) throw new Error("Select the original project before attaching a harness credential.");
  const readHarness = async (): Promise<HarnessProfile> => {
    const harness = await api.getHarness(action.harnessId);
    if (!selected()) throw new Error("The selected project changed. Read the harness again.");
    if (harness.id !== action.harnessId || harness.name !== action.name
        || harness.revision !== action.expectedRevision) {
      throw new Error("The harness changed. Ask Butler to read its current name and revision.");
    }
    if (harness.kind === "grok_acp" || (harness.kind === "claude_agent_sdk" && harness.nativeCapabilities.shell)) {
      throw new Error("This harness requires its existing vendor login and cannot use a managed credential.");
    }
    const mode = harness.connectionMode === "endpoint" ? "endpoint_bearer" : "secret_ref";
    if (mode !== action.authMode) throw new Error("The harness authentication mode changed. Ask Butler to read it again.");
    return harness;
  };
  const readCredential = async (): Promise<void> => {
    const status = await api.credentialStatus(action.reference);
    if (status.reference !== action.reference || !status.available || status.state !== "available") {
      throw new Error("The credential is unavailable. Check its status or create a replacement before attaching it.");
    }
  };

  const harness = await readHarness();
  await readCredential();
  const replacing = harness.secretRef ? " This replaces its current credential reference." : "";
  const approved = await confirm({
    title: `Attach credential to ${harness.name}?`,
    message: `Attach ${action.reference} to ${harness.name} for ${action.authMode === "endpoint_bearer" ? "endpoint bearer" : "spawned harness"} authentication?${replacing} Existing running sessions keep their own state.`,
    confirmLabel: "Attach credential",
  });
  if (!approved) return `No credential was attached to ${harness.name}.`;
  if (!selected()) throw new Error("The selected project changed. Ask Butler to read the harness again.");
  await readHarness();
  await readCredential();

  let writeFailed = false;
  try {
    await api.updateHarness(action.harnessId, {auth_mode: action.authMode, secret_ref: action.reference}, action.expectedRevision);
  } catch (caught) {
    writeFailed = true;
    logCaughtDiagnostic("interface.butler_harness.credential_write_failed", "Harness credential attachment response failed; checking Core before retry.", caught, "harnesses");
  }
  let observed: HarnessProfile;
  try {
    observed = await api.getHarness(action.harnessId);
  } catch (caught) {
    logCaughtDiagnostic("interface.butler_harness.credential_readback_failed", "Harness credential attachment could not be read back.", caught, "harnesses");
    return `Core did not confirm ${harness.name}'s credential. Inspect the harness before asking Butler to retry; the write may have succeeded.`;
  }
  if (!selected()) return `The selected project changed during ${harness.name}'s credential update. Inspect that harness before retrying.`;
  if (observed.id !== action.harnessId || observed.name !== action.name
      || observed.revision <= action.expectedRevision || observed.secretRef !== action.reference
      || observed.authMode !== action.authMode) {
    if (writeFailed && observed.revision !== action.expectedRevision) {
      throw new Error("The harness changed before the credential could be attached. Ask Butler to read its current state.");
    }
    return `Core did not confirm ${harness.name}'s credential. Inspect the harness before retrying.`;
  }
  try {
    await readCredential();
  } catch (caught) {
    logCaughtDiagnostic("interface.butler_harness.credential_status_readback_failed", "Attached harness credential status could not be confirmed.", caught, "harnesses");
    return `Core saved ${harness.name}'s credential at revision ${observed.revision}, but its availability could not be confirmed. Check status before use.`;
  }
  return `Attached ${action.reference} to ${observed.name} at Core revision ${observed.revision}. Core confirmed the harness authentication mode and credential availability. Check harness health before starting a new turn.`;
}
