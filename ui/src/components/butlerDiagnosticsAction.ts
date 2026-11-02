import type {ApiClient} from "../api/client";
import type {SettingClientAction} from "./nebulaAssistantActions";

type ExportAction = Extract<SettingClientAction, {kind: "diagnostics_export"}>;

export function startButlerDiagnosticsDownload(blob: Blob): void {
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = "nebula-diagnostics.zip";
  link.click();
  // Firefox and WebKit start the download after click() returns.
  setTimeout(() => URL.revokeObjectURL(url), 0);
}

/** Confirm the sensitive export and verify ZIP bytes before requesting a download. */
export async function applyButlerDiagnosticsExport(
  api: Pick<ApiClient, "exportDiagnostics">,
  action: ExportAction,
  selectedProject: () => string | undefined,
  confirm: (request: {title: string; message: string; confirmLabel: string}) => Promise<boolean>,
  download: (blob: Blob) => void = startButlerDiagnosticsDownload,
): Promise<string> {
  if (selectedProject() !== action.projectId) throw new Error("Select the original project before exporting diagnostics.");
  const approved = await confirm({title: "Export local diagnostics?",
    message: "The ZIP contains unredacted logs, build/platform metadata, logger health, settings, and a SHA-256 manifest. It excludes project databases, workspaces, evidence, terminal results, and provider configuration. Treat the exported logs as sensitive data.",
    confirmLabel: "Export diagnostics"});
  if (!approved) return "No diagnostics ZIP was requested.";
  if (selectedProject() !== action.projectId) throw new Error("The selected project changed. Ask Butler to prepare the export again.");
  const blob = await api.exportDiagnostics();
  const signature = new Uint8Array(await blob.slice(0, 4).arrayBuffer());
  if (blob.size < 4 || signature[0] !== 0x50 || signature[1] !== 0x4b
      || !((signature[2] === 0x03 && signature[3] === 0x04)
        || (signature[2] === 0x05 && signature[3] === 0x06))) {
    throw new Error("Core did not return a valid diagnostics ZIP. No download was started.");
  }
  if (selectedProject() !== action.projectId) throw new Error("The selected project changed before download. No ZIP was started.");
  download(blob);
  return "Started a verified diagnostics ZIP download on this browser. Butler received no bundle bytes; check this device's downloads for the final file.";
}
