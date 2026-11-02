import {ApiError, type ApiClient} from "../api/client";
import {logCaughtDiagnostic} from "../diagnostics";
import type {SettingClientAction} from "./nebulaAssistantActions";

type Action = Extract<SettingClientAction, {kind: "result_delete"}>;
type ResultApi = Pick<ApiClient, "getStructuredResult" | "deleteStructuredResultAtRevision">;

export async function applyButlerResultDelete(
  api: ResultApi, action: Action, project: () => string | undefined,
  confirm: (request: {title: string; message: string; confirmLabel: string; tone?: "danger"}) => Promise<boolean>,
): Promise<{message: string; deleted: boolean}> {
  const current = async () => {
    if (project() !== action.projectId) throw new Error("Select the original project before deleting its result.");
    const result = await api.getStructuredResult(action.projectId, action.resultId);
    if (project() !== action.projectId || result.projectId !== action.projectId || result.id !== action.resultId
        || result.revision !== action.expectedRevision || result.title !== action.title) {
      throw new Error("The result changed. Ask Butler to read its current title and revision.");
    }
  };
  await current();
  if (!await confirm({title: "Delete published result?", message: `Permanently delete “${action.title}” and its presentation hints? Other project records are retained.`, confirmLabel: "Delete result", tone: "danger"})) {
    return {message: `Result “${action.title}” was not deleted.`, deleted: false};
  }
  await current();
  try {
    await api.deleteStructuredResultAtRevision(action.projectId, action.resultId, action.expectedRevision);
  } catch (caught) {
    logCaughtDiagnostic("interface.butler_results.write_response_failed", "Result deletion response failed; reading exact Core state before retry.", caught, "results");
  }
  let absent = false;
  try { await api.getStructuredResult(action.projectId, action.resultId); }
  catch (caught) {
    // diagnostic-expected: only exact authenticated absence confirms deletion.
    if (caught instanceof ApiError && caught.status === 404) absent = true;
    else logCaughtDiagnostic("interface.butler_results.readback_failed", "Core did not confirm result deletion.", caught, "results");
  }
  if (project() !== action.projectId) return {message: "The selected project changed during deletion. Inspect the original project's Results before retrying.", deleted: false};
  return absent ? {message: `Deleted result “${action.title}”. Core confirms its absence.`, deleted: true}
    : {message: "Core did not confirm result deletion. Inspect Results before retrying; the write may have succeeded.", deleted: false};
}
