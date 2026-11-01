import type {ApiClient} from "../api/client";
import type {McpServerProfile} from "../api/types";
import {logCaughtDiagnostic} from "../diagnostics";
import type {SettingClientAction} from "./nebulaAssistantActions";

type AttachAction = Extract<SettingClientAction, {kind: "mcp_attach_credential"}>;
type McpCredentialApi = Pick<ApiClient, "getMcpServer" | "credentialStatus" | "updateMcpServer">;

function slotLabel(action: AttachAction): string {
  return action.slot === "bearer" ? "HTTP bearer"
    : action.slot === "header" ? `HTTP header ${action.key}` : `stdio environment ${action.key}`;
}

function slotReference(server: McpServerProfile, action: AttachAction): string | undefined {
  return action.slot === "bearer" ? server.bearerSecretRef
    : action.slot === "header" ? server.headerSecretRefs?.[action.key!] : server.environmentSecretRefs?.[action.key!];
}

/** Confirm one exact MCP credential slot on the requesting browser. */
export async function attachButlerMcpCredential(
  api: McpCredentialApi,
  action: AttachAction,
  selectedProject: () => string | undefined,
  confirm: (request: {title: string; message: string; confirmLabel: string}) => Promise<boolean>,
): Promise<string> {
  const selected = () => selectedProject() === action.projectId;
  if (!selected()) throw new Error("Select the original project before attaching an MCP credential.");
  const readServer = async (): Promise<McpServerProfile> => {
    const server = await api.getMcpServer(action.serverId);
    if (!selected()) throw new Error("The selected project changed. Read the MCP server again.");
    if (server.transport === "builtin") throw new Error("Built-in MCP servers cannot accept credentials.");
    if (server.id !== action.serverId || server.name !== action.name || server.revision !== action.expectedRevision
        || server.transport !== action.transport || (server.projectId && server.projectId !== action.projectId)) {
      throw new Error("The MCP server changed. Ask Butler to read its current identity and revision.");
    }
    if ((action.slot === "environment") !== (server.transport === "stdio")) {
      throw new Error("The MCP credential slot does not match its transport.");
    }
    if (action.slot === "header" && Object.keys(server.headerSecretRefs ?? {}).some(name =>
      name.toLowerCase() === action.key!.toLowerCase() && name !== action.key)) {
      throw new Error("That HTTP header already exists with different capitalization. Select its exact name.");
    }
    return server;
  };
  const readCredential = async (): Promise<void> => {
    const status = await api.credentialStatus(action.reference);
    if (status.reference !== action.reference || !status.available || status.state !== "available") {
      throw new Error("The credential is unavailable. Check its status or create a replacement before attaching it.");
    }
  };

  const server = await readServer();
  await readCredential();
  const replacing = slotReference(server, action) ? " This replaces the current reference in that slot." : "";
  const approved = await confirm({
    title: `Attach credential to ${server.name}?`,
    message: `Attach ${action.reference} to ${slotLabel(action)} on ${server.name}?${replacing} The secret stays in Core's credential store.`,
    confirmLabel: "Attach credential",
  });
  if (!approved) return `No credential was attached to ${server.name}.`;
  if (!selected()) throw new Error("The selected project changed. Ask Butler to read the MCP server again.");
  const current = await readServer();
  await readCredential();

  const changes: Record<string, unknown> = action.slot === "bearer"
    ? {auth_mode: "bearer", bearer_secret_ref: action.reference, header_secret_refs: {}}
    : action.slot === "header"
      ? {auth_mode: "headers", bearer_secret_ref: null,
        header_secret_refs: {...current.headerSecretRefs, [action.key!]: action.reference}}
      : {environment_secret_refs: {...current.environmentSecretRefs, [action.key!]: action.reference}};
  let writeFailed = false;
  try {
    await api.updateMcpServer(action.serverId, changes, action.expectedRevision);
  } catch (caught) {
    writeFailed = true;
    logCaughtDiagnostic("interface.butler_mcp.credential_write_failed", "MCP credential attachment response failed; checking Core before retry.", caught, "harness_settings");
  }
  let observed: McpServerProfile;
  try {
    observed = await api.getMcpServer(action.serverId);
  } catch (caught) {
    logCaughtDiagnostic("interface.butler_mcp.credential_readback_failed", "MCP credential attachment could not be read back.", caught, "harness_settings");
    return `Core did not confirm ${server.name}'s MCP credential. Inspect the server before asking Butler to retry; the write may have succeeded.`;
  }
  if (!selected()) return `The selected project changed during ${server.name}'s credential update. Inspect that server before retrying.`;
  const coherent = action.slot === "environment"
    ? observed.authMode === "none" && Object.entries(current.environmentSecretRefs ?? {}).every(([key, ref]) => observed.environmentSecretRefs?.[key] === ref)
    : action.slot === "bearer"
      ? observed.authMode === "bearer" && Object.keys(observed.headerSecretRefs ?? {}).length === 0
      : observed.authMode === "headers" && !observed.bearerSecretRef
        && Object.entries(current.headerSecretRefs ?? {}).every(([key, ref]) => key === action.key || observed.headerSecretRefs?.[key] === ref);
  if (observed.id !== action.serverId || observed.name !== action.name || observed.transport !== action.transport
      || observed.revision <= action.expectedRevision || slotReference(observed, action) !== action.reference || !coherent) {
    if (writeFailed && observed.revision !== action.expectedRevision) {
      throw new Error("The MCP server changed before the credential could be confirmed. Ask Butler to read its current state.");
    }
    return `Core did not confirm ${server.name}'s ${slotLabel(action)} credential. Inspect the server before retrying.`;
  }
  try {
    await readCredential();
  } catch (caught) {
    logCaughtDiagnostic("interface.butler_mcp.credential_status_readback_failed", "Attached MCP credential status could not be confirmed.", caught, "harness_settings");
    return `Core saved ${server.name}'s ${slotLabel(action)} reference at revision ${observed.revision}, but its availability could not be confirmed. Check status before use.`;
  }
  window.dispatchEvent(new Event("nebula:mcp-profiles-changed"));
  return `Attached ${action.reference} to ${observed.name} for ${slotLabel(action)} at Core revision ${observed.revision}. Core confirmed the slot, mode and credential availability. Check MCP health before use.`;
}
