import {ApiError, type ApiClient} from "../api/client";
import {startLibraryArtifactDownload} from "../api/libraryFiles";
import {sha256BlobHex} from "../sha256";
import type {SettingClientAction} from "./nebulaAssistantActions";

type SourceAction = Extract<SettingClientAction, {kind: "source_download" | "source_reindex" | "source_remove"}>;
type SourceApi = Pick<ApiClient, "getKnowledgeSource" | "getArtifactContent" | "reindexKnowledgeSourceAtRevision" | "deleteKnowledgeSourceAtRevision">;

export async function applyButlerSourceAction(
  api: SourceApi, action: SourceAction, project: () => string | undefined,
  confirm: (request: {title: string; message: string; confirmLabel: string; tone?: "danger"}) => Promise<boolean>,
): Promise<{message: string; refresh: boolean}> {
  const current = async () => {
    if (project() !== action.projectId) throw new Error("Select the original project before using its source.");
    const source = await api.getKnowledgeSource(action.sourceId);
    if (project() !== action.projectId || source.id !== action.sourceId || source.engagementId !== action.projectId
        || source.revision !== action.expectedRevision || source.name !== action.name) {
      throw new Error("The source changed. Ask Butler to read its current identity and revision.");
    }
    return source;
  };
  await current();
  const remove = action.kind === "source_remove";
  const download = action.kind === "source_download";
  const verb = remove ? "Remove" : download ? "Download" : "Reindex";
  if (!await confirm({title: `${verb} project source?`,
    message: remove ? `Remove “${action.name}” from this project's retrieval? The original artifact is retained.`
      : download ? `Download “${action.name}” to this device? The file may contain sensitive information; Butler receives no bytes.`
      : `Rebuild “${action.name}” from its stored original for this project's retrieval?`,
    confirmLabel: `${verb} source`, ...(remove ? {tone: "danger" as const} : {})})) {
    return {message: `The source was not ${remove ? "removed" : download ? "downloaded" : "reindexed"}.`, refresh: false};
  }
  const before = await current();
  if (download) {
    if (!before.artifactId || typeof before.metadata.sha256 !== "string") throw new Error("The source has no verifiable original artifact.");
    const blob = await api.getArtifactContent(before.artifactId);
    const after = await current();
    if (after.artifactId !== before.artifactId || blob.size !== before.metadata.size || await sha256BlobHex(blob) !== before.metadata.sha256) {
      throw new Error("The source download did not match Core's original. Inspect the source before retrying.");
    }
    startLibraryArtifactDownload(blob, before.metadata.filename || before.name);
    return {message: `Started a verified original download of ${before.name} on this device.`, refresh: false};
  }
  if (remove) {
    await api.deleteKnowledgeSourceAtRevision(action.sourceId, action.projectId, action.expectedRevision);
    try { await api.getKnowledgeSource(action.sourceId); }
    catch (error) {
      // diagnostic-expected: exact Core absence is required before reporting removal.
      if (error instanceof ApiError && error.status === 404) return {message: `Removed ${action.name} from this project's Sources. Its original artifact was retained.`, refresh: true};
      throw error;
    }
    throw new Error("Core still lists this source. Inspect Sources before retrying removal.");
  }
  await api.reindexKnowledgeSourceAtRevision(action.sourceId, action.projectId, action.expectedRevision);
  const saved = await api.getKnowledgeSource(action.sourceId);
  if (project() !== action.projectId || saved.id !== before.id || saved.engagementId !== action.projectId
      || saved.revision <= before.revision || saved.artifactId !== before.artifactId || saved.status !== "ready") {
    throw new Error("Core did not confirm a ready reindexed source. Inspect Sources before retrying.");
  }
  return {message: `Reindexed ${saved.name} in Core's project Sources at revision ${saved.revision}.`, refresh: true};
}
