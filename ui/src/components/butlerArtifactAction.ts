import type {ApiClient} from "../api/client";
import {startLibraryArtifactDownload} from "../api/libraryFiles";
import {sha256BlobHex} from "../sha256";
import type {SettingClientAction} from "./nebulaAssistantActions";

type Action = Extract<SettingClientAction, {kind: "artifact_download"}>;
type ArtifactApi = Pick<ApiClient, "request" | "downloadToolArtifact">;
type Artifact = {id: string; engagement_id: string; revision: number; sha256: string; size: number; metadata: Record<string, unknown>};
type Owner = {id: string; engagement_id: string; chat_session_id?: string; [key: string]: unknown};
const executionFields = ["source_artifact_id", "stdout_artifact_id", "stderr_artifact_id", "redacted_stdout_artifact_id", "redacted_stderr_artifact_id", "manifest_artifact_id"];

/** Original bytes remain on the requesting browser after explicit acknowledgment. */
export async function applyButlerArtifactDownload(
  api: ArtifactApi, action: Action, selectedProject: () => string | undefined,
  confirm: (request: {title: string; message: string; confirmLabel: string}) => Promise<boolean>,
): Promise<string> {
  const current = async () => {
    if (selectedProject() !== action.projectId) throw new Error("Select the original project before downloading its artifact.");
    const ownerPath = `${action.ownerKind === "tool_call" ? "tool-calls" : "executions"}/${encodeURIComponent(action.ownerId)}`;
    const [artifact, owner] = await Promise.all([
      api.request<Artifact>(`artifacts/${encodeURIComponent(action.artifactId)}`),
      api.request<Owner>(ownerPath),
    ]);
    if (selectedProject() !== action.projectId || owner.id !== action.ownerId || owner.engagement_id !== action.projectId
        || artifact.id !== action.artifactId || artifact.engagement_id !== action.projectId
        || artifact.revision !== action.expectedRevision || artifact.sha256 !== action.sha256 || artifact.size !== action.size
        || artifact.size > 20 * 1024 * 1024
        || (action.ownerKind === "tool_call" ? artifact.metadata.tool_call_id !== owner.id
          : artifact.metadata.execution_id !== owner.id || !executionFields.some(field => owner[field] === artifact.id))) {
      throw new Error("The artifact or its owner changed. Ask Butler to read its current identity and revision.");
    }
    if (action.ownerKind === "tool_call" && typeof owner.chat_session_id === "string") {
      const chat = await api.request<{id: string; engagement_id: string; metadata: Record<string, unknown>}>(`chat-sessions/${encodeURIComponent(owner.chat_session_id)}`);
      if (chat.id !== owner.chat_session_id || chat.engagement_id !== action.projectId
          || ["control_assistant", "side_chat", "temporary_assistant"].some(key => chat.metadata[key] === true)) {
        throw new Error("Choose an artifact from a visible project conversation.");
      }
    }
    if (selectedProject() !== action.projectId) throw new Error("The selected project changed. Read the artifact again.");
  };
  await current();
  if (!await confirm({title: "Download original artifact?", message: `Download “${action.filename}” to this device? Original source and output may contain unredacted credentials or private data. Butler receives no original bytes.`, confirmLabel: "Download original"})) {
    return `Original artifact “${action.filename}” was not downloaded.`;
  }
  await current();
  const {blob} = await api.downloadToolArtifact(action.artifactId);
  await current();
  if (blob.size !== action.size || await sha256BlobHex(blob) !== action.sha256) throw new Error("The original download failed its Core hash or size check. Inspect the artifact before retrying.");
  if (selectedProject() !== action.projectId) throw new Error("The selected project changed before the download started.");
  startLibraryArtifactDownload(blob, action.filename);
  return `Started a verified original download of “${action.filename}” on this device.`;
}
