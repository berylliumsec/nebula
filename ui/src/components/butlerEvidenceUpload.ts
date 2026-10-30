import type { ApiClient } from "../api/client";
import type { EvidenceSummary } from "../api/types";
import { sha256BlobHex } from "../sha256";
import type { SettingClientAction } from "./nebulaAssistantActions";

export const MAX_EVIDENCE_BYTES = 25 * 1024 * 1024;

type EvidenceApi = Pick<ApiClient, "uploadEvidence" | "getEvidence" | "listEvidence" | "listFindings">;
type UploadAction = Extract<SettingClientAction, {kind: "evidence_upload"}>;

function encodeFile(buffer: ArrayBuffer): string {
  const bytes = new Uint8Array(buffer);
  let binary = "";
  for (let offset = 0; offset < bytes.length; offset += 0x8000) {
    binary += String.fromCharCode(...bytes.subarray(offset, offset + 0x8000));
  }
  return btoa(binary);
}

export async function uploadButlerEvidence(
  api: EvidenceApi,
  action: UploadAction,
  file: File,
  selectedProject: () => string | undefined,
  capturedBy?: string,
  signal?: AbortSignal,
): Promise<EvidenceSummary> {
  if (selectedProject() !== action.projectId) throw new Error("Select the original project before uploading evidence.");
  if (file.size > MAX_EVIDENCE_BYTES) throw new Error(`${file.name} is larger than the 25 MB evidence limit.`);
  if (!file.name || file.name.length > 255) throw new Error("Choose a file with a name of at most 255 characters.");
  const title = action.title ?? file.name;
  if (!title.trim() || title.length > 500) throw new Error("Choose an evidence title of at most 500 characters.");
  const sha256 = await sha256BlobHex(file, signal);
  if (selectedProject() !== action.projectId || signal?.aborted) throw new Error("The project changed before the upload. Ask Butler to prepare it again.");
  const contentBase64 = encodeFile(await file.arrayBuffer());
  if (selectedProject() !== action.projectId || signal?.aborted) throw new Error("The project changed before the upload. Ask Butler to prepare it again.");
  const created = await api.uploadEvidence({
    engagementId: action.projectId,
    filename: file.name,
    title,
    evidenceType: action.evidenceType,
    contentBase64,
    mediaType: file.type || undefined,
    description: action.description,
    source: "operator_upload",
    findingId: action.findingId,
    assetIds: action.assetIds,
    capturedBy,
  }, signal);
  const observed = await api.getEvidence(created.id);
  const listed = await api.listEvidence(action.projectId);
  if (selectedProject() !== action.projectId || observed.engagementId !== action.projectId
      || observed.id !== created.id || !listed.items.some((item) => item.id === observed.id)
      || observed.sha256 !== sha256 || observed.metadata.size !== file.size
      || observed.metadata.filename !== file.name || !observed.artifactId
      || observed.title !== title || observed.evidenceType !== action.evidenceType
      || observed.description !== action.description || observed.findingId !== action.findingId
      || observed.assetIds.length !== action.assetIds.length
      || !action.assetIds.every((id) => observed.assetIds.includes(id))) {
    throw new Error("Core did not confirm the evidence bytes and links. Inspect Evidence before retrying.");
  }
  if (action.findingId) {
    const findings = await api.listFindings(action.projectId);
    if (!findings.items.some((item) => item.id === action.findingId && item.evidenceIds.includes(observed.id))) {
      throw new Error("Core did not confirm the finding link. Inspect Evidence and Findings before retrying.");
    }
  }
  return observed;
}
