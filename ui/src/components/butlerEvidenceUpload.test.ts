import {describe, expect, it, vi} from "vitest";
import type {ApiClient} from "../api/client";
import type {EvidenceSummary} from "../api/types";
import {sha256Hex} from "../sha256";
import type {SettingClientAction} from "./nebulaAssistantActions";
import {MAX_EVIDENCE_BYTES, uploadButlerEvidence} from "./butlerEvidenceUpload";

type UploadAction = Extract<SettingClientAction, {kind: "evidence_upload"}>;

const action: UploadAction = {
  kind: "evidence_upload", projectId: "project-1", title: "Synthetic proof", description: "Fixture",
  evidenceType: "operator_upload", findingId: "finding-1", assetIds: ["asset-1"],
};

function deviceFile(bytes: Uint8Array = new TextEncoder().encode("Synthetic proof\n")): File {
  const file = new File([Uint8Array.from(bytes).buffer as ArrayBuffer], "proof.txt", {type: "text/plain"});
  Object.defineProperty(file, "stream", {value: () => new ReadableStream<Uint8Array>({
    start(controller) {controller.enqueue(bytes); controller.close();},
  })});
  Object.defineProperty(file, "arrayBuffer", {value: async () => bytes.slice().buffer});
  return file;
}

function evidence(file: File, sha256: string): EvidenceSummary {
  return {
    id: "evidence-1", engagementId: "project-1", evidenceType: "operator_upload", title: "Synthetic proof",
    description: "Fixture", artifactId: "artifact-1", findingId: "finding-1", assetIds: ["asset-1"], sha256,
    capturedAt: "2026-10-30T00:00:00Z", createdAt: "2026-10-30T00:00:00Z",
    updatedAt: "2026-10-30T00:00:00Z", metadata: {filename: file.name, size: file.size},
  };
}

function client(record: EvidenceSummary) {
  const methods = {
    uploadEvidence: vi.fn().mockResolvedValue(record),
    getEvidence: vi.fn().mockResolvedValue(record),
    listEvidence: vi.fn().mockResolvedValue({items: [record]}),
    listFindings: vi.fn().mockResolvedValue({items: [{id: "finding-1", evidenceIds: [record.id]}]}),
  };
  return {methods, api: methods as unknown as Pick<ApiClient, "uploadEvidence" | "getEvidence" | "listEvidence" | "listFindings">};
}

describe("Butler evidence device upload", () => {
  it("sends only device-owned bytes and reports success after exact Core readback", async () => {
    const file = deviceFile();
    const hash = sha256Hex(new TextEncoder().encode("Synthetic proof\n"));
    const {api, methods} = client(evidence(file, hash));
    const observed = await uploadButlerEvidence(api, action, file, () => "project-1", "operator-1");
    expect(observed.id).toBe("evidence-1");
    expect(methods.uploadEvidence).toHaveBeenCalledWith(expect.objectContaining({
      engagementId: "project-1", filename: "proof.txt", title: "Synthetic proof",
      contentBase64: btoa("Synthetic proof\n"), findingId: "finding-1", assetIds: ["asset-1"],
      capturedBy: "operator-1",
    }), undefined);
    expect(methods.getEvidence).toHaveBeenCalledWith("evidence-1");
    expect(methods.listEvidence).toHaveBeenCalledWith("project-1");
    expect(methods.listFindings).toHaveBeenCalledWith("project-1");
  });

  it("does not post after a project switch or oversized selection", async () => {
    const file = deviceFile();
    const {api, methods} = client(evidence(file, sha256Hex(new TextEncoder().encode("Synthetic proof\n"))));
    await expect(uploadButlerEvidence(api, action, file, () => "other-project")).rejects.toThrow("original project");
    const oversized = deviceFile();
    Object.defineProperty(oversized, "size", {value: MAX_EVIDENCE_BYTES + 1});
    await expect(uploadButlerEvidence(api, action, oversized, () => "project-1")).rejects.toThrow("25 MB");
    expect(methods.uploadEvidence).not.toHaveBeenCalled();
  });

  it("treats a failed readback after POST as uncertain and requires inspection", async () => {
    const file = deviceFile();
    const {api, methods} = client(evidence(file, "0".repeat(64)));
    await expect(uploadButlerEvidence(api, action, file, () => "project-1")).rejects.toThrow("Inspect Evidence before retrying");
    expect(methods.uploadEvidence).toHaveBeenCalledTimes(1);
    methods.getEvidence.mockRejectedValueOnce(new Error("Core disconnected"));
    await expect(uploadButlerEvidence(api, action, file, () => "project-1")).rejects.toThrow("Core disconnected");
    expect(methods.uploadEvidence).toHaveBeenCalledTimes(2);
  });
});
