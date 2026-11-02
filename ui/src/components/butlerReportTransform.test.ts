import { describe, expect, it, vi } from "vitest";
import type { ApiClient } from "../api/client";
import type { ObservationSummary, ReportSummary, WritingTransformResponse } from "../api/types";
import { readButlerReportTransform, saveButlerReportTransform, settleButlerReportTransform, type ButlerReportTransformAction } from "./butlerReportTransform";

const projectId = "11111111-1111-4111-8111-111111111111";
const reportId = "22222222-2222-4222-8222-222222222222";
const noteId = "33333333-3333-4333-8333-333333333333";
const action: ButlerReportTransformAction = {
  kind: "report_transform_note", projectId, reportId, expectedRevision: 4,
  observationId: noteId, expectedSourceRevision: 2, instruction: "Keep uncertainty visible.",
};
const report = {id: reportId, engagementId: projectId, title: "Assessment", status: "draft", revision: 4,
  observationIds: [noteId], noteTransforms: []} as unknown as ReportSummary;
const note = {id: noteId, engagementId: projectId, title: "TLS note", body: "Port 443 observed.", revision: 2} as ObservationSummary;
const result: WritingTransformResponse = {
  content: "Port 443 was observed; certificate posture is unverified.",
  provenance: {backendKind: "provider", providerProfileId: "local-model", model: "writer",
    promptVersion: "writing-transform/v1", sourceSha256: "a".repeat(64),
    instruction: action.instruction, generatedAt: "2026-10-30T12:00:00Z", providerRequestId: "request-1"},
  usage: {inputTokens: 10, outputTokens: 12, totalTokens: 22},
};

describe("Butler report section save", () => {
  it("saves only reviewed prose at exact report and source revisions, then reads Core back", async () => {
    const saved = {...report, revision: 5, noteTransforms: [{observationId: noteId, sourceRevision: 2,
      title: note.title, body: result.content, provenance: result.provenance}]};
    const getReport = vi.fn().mockResolvedValueOnce(report).mockResolvedValueOnce(saved);
    const getObservation = vi.fn().mockResolvedValue(note);
    const updateReport = vi.fn().mockResolvedValue(saved);
    const api = {getReport, getObservation, updateReport} as unknown as ApiClient;

    expect(await saveButlerReportTransform(api, action, result)).toEqual(saved);
    expect(updateReport).toHaveBeenCalledWith(reportId, {expectedRevision: 4,
      noteTransforms: [{observationId: noteId, sourceRevision: 2, title: "TLS note",
        body: result.content, provenance: result.provenance}]});
    expect(getReport).toHaveBeenCalledTimes(2);
  });

  it("refuses a changed source, another project, or a report that no longer links the note", async () => {
    for (const changed of [
      {report, note: {...note, revision: 3}},
      {report: {...report, engagementId: "other-project"}, note},
      {report: {...report, observationIds: []}, note},
    ]) {
      const updateReport = vi.fn();
      const api = {getReport: vi.fn().mockResolvedValue(changed.report),
        getObservation: vi.fn().mockResolvedValue(changed.note), updateReport} as unknown as ApiClient;
      await expect(readButlerReportTransform(api, action)).rejects.toThrow();
      expect(updateReport).not.toHaveBeenCalled();
    }
  });

  it("reconciles a lost write response only when exact reviewed content appears in Core", async () => {
    const saved = {...report, revision: 5, noteTransforms: [{observationId: noteId, sourceRevision: 2,
      title: note.title, body: result.content, provenance: result.provenance}]};
    const api = {getReport: vi.fn().mockResolvedValueOnce(report).mockResolvedValueOnce(saved),
      getObservation: vi.fn().mockResolvedValue(note),
      updateReport: vi.fn().mockRejectedValue(new Error("connection lost"))} as unknown as ApiClient;
    expect(await saveButlerReportTransform(api, action, result)).toEqual(saved);
  });

  it("keeps a confirmed save and current report visible when the later list refresh fails", async () => {
    const saved = {...report, revision: 5, noteTransforms: [{observationId: noteId, sourceRevision: 2,
      title: note.title, body: result.content, provenance: result.provenance}]};
    const updateReport = vi.fn().mockResolvedValue(saved);
    const api = {getReport: vi.fn().mockResolvedValueOnce(report).mockResolvedValueOnce(saved),
      getObservation: vi.fn().mockResolvedValue(note), updateReport} as unknown as ApiClient;
    const adoptConfirmedReport = vi.fn();
    const refreshReports = vi.fn().mockRejectedValue(new Error("list connection lost"));

    expect(await settleButlerReportTransform(api, action, result, adoptConfirmedReport, refreshReports))
      .toEqual({report: saved, refreshFailed: true});
    expect(adoptConfirmedReport).toHaveBeenCalledWith(saved);
    expect(updateReport).toHaveBeenCalledTimes(1);
  });

  it("does not report success when Core read-back lacks the reviewed section", async () => {
    const api = {getReport: vi.fn().mockResolvedValueOnce(report).mockResolvedValueOnce({...report, revision: 5}),
      getObservation: vi.fn().mockResolvedValue(note), updateReport: vi.fn().mockResolvedValue({...report, revision: 5})} as unknown as ApiClient;
    await expect(saveButlerReportTransform(api, action, result)).rejects.toThrow("did not read back");
  });
});
