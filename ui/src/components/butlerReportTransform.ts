import { ApiError, type ApiClient } from "../api/client";
import type { ObservationSummary, ReportSummary, WritingTransformResponse } from "../api/types";
import { logDiagnostic } from "../diagnostics";
import type { SettingClientAction } from "./nebulaAssistantActions";

export type ButlerReportTransformAction = Extract<SettingClientAction, {kind: "report_transform_note"}>;

export async function readButlerReportTransform(
  api: ApiClient,
  action: ButlerReportTransformAction,
): Promise<{report: ReportSummary; note: ObservationSummary}> {
  const [report, note] = await Promise.all([
    api.getReport(action.reportId),
    api.getObservation(action.observationId),
  ]);
  if (report.engagementId !== action.projectId || note.engagementId !== action.projectId) {
    throw new Error("The report or note is outside the selected project. Ask Butler to select them again.");
  }
  if (report.revision !== action.expectedRevision || note.revision !== action.expectedSourceRevision) {
    throw new Error("The report or source note changed. Ask Butler to read their current revisions before transforming again.");
  }
  if (report.status === "final" || !report.observationIds.includes(note.id)) {
    throw new Error("The report is final or no longer includes this note. Inspect Reports before retrying.");
  }
  if (!note.body.trim()) throw new Error("The selected source note has no body to transform.");
  return {report, note};
}

export async function saveButlerReportTransform(
  api: ApiClient,
  action: ButlerReportTransformAction,
  result: WritingTransformResponse,
): Promise<ReportSummary> {
  const {report, note} = await readButlerReportTransform(api, action);
  const transform = {
    observationId: note.id,
    sourceRevision: note.revision,
    title: note.title,
    body: result.content,
    provenance: result.provenance,
  };
  let writeError: unknown;
  try {
    await api.updateReport(report.id, {
      expectedRevision: report.revision,
      noteTransforms: [transform, ...report.noteTransforms.filter((item) => item.observationId !== note.id)],
    });
  } catch (caught) {
    writeError = caught;
    void logDiagnostic({level: "warning", eventCode: "interface.nebula_assistant.report_transform_response_failed",
      message: "Butler could not confirm the reviewed report section response and is checking Core.",
      outcome: "failure", stage: "report-transform", retryable: true, exception: caught});
    if (caught instanceof ApiError && caught.status >= 400 && caught.status < 500 && caught.status !== 409) throw caught;
  }
  let observed: ReportSummary;
  try {
    observed = await api.getReport(report.id);
  } catch (caught) {
    void logDiagnostic({level: "error", eventCode: "interface.nebula_assistant.report_transform_readback_failed",
      message: "Butler could not read the reviewed report section after saving.",
      outcome: "failure", stage: "report-transform", retryable: true, exception: caught});
    throw new Error("Core did not confirm the report section. Inspect the exact report before retrying; it may already be saved.");
  }
  const saved = observed.noteTransforms.find((item) => item.observationId === note.id);
  if (observed.engagementId !== action.projectId || observed.revision <= action.expectedRevision
      || !saved || saved.sourceRevision !== note.revision || saved.body !== result.content
      || JSON.stringify(saved.provenance) !== JSON.stringify(result.provenance)) {
    throw new Error(writeError
      ? "Core did not confirm the reviewed report section after an uncertain response. Inspect Reports before retrying."
      : "Core did not read back the reviewed report section. Inspect Reports before retrying.");
  }
  return observed;
}

export async function settleButlerReportTransform(
  api: ApiClient,
  action: ButlerReportTransformAction,
  result: WritingTransformResponse,
  adoptConfirmedReport: (report: ReportSummary) => void,
  refreshReports: () => Promise<void>,
): Promise<{report: ReportSummary; refreshFailed: boolean}> {
  const report = await saveButlerReportTransform(api, action, result);
  adoptConfirmedReport(report);
  try {
    await refreshReports();
    return {report, refreshFailed: false};
  } catch (caught) {
    void logDiagnostic({level: "warning", eventCode: "interface.nebula_assistant.report_transform_list_refresh_failed",
      message: "Core saved the reviewed report section, but the Reports list could not refresh.",
      outcome: "failure", stage: "report-transform-refresh", retryable: true, exception: caught});
    return {report, refreshFailed: true};
  }
}
