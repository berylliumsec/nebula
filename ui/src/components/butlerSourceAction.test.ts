import {describe, expect, it, vi} from "vitest";
import {ApiError, type ApiClient} from "../api/client";
import type {KnowledgeSource} from "../api/types";
import {applyButlerSourceAction} from "./butlerSourceAction";

const source = {id: "source-id", engagementId: "project", name: "source.txt", revision: 2,
  sourceType: "text", status: "ready", documentCount: 1, artifactId: "artifact", metadata: {}, createdAt: "", updatedAt: ""} satisfies KnowledgeSource & {revision: number};
const action = {kind: "source_reindex" as const, projectId: "project", sourceId: source.id, name: source.name, expectedRevision: 2};

describe("Butler source handoff", () => {
  it("refuses foreign or stale records before confirmation or mutation", async () => {
    const api = {getKnowledgeSource: vi.fn().mockResolvedValue({...source, engagementId: "foreign"}), reindexKnowledgeSourceAtRevision: vi.fn()} as unknown as ApiClient;
    const confirm = vi.fn();
    await expect(applyButlerSourceAction(api, action, () => "project", confirm)).rejects.toThrow("source changed");
    expect(confirm).not.toHaveBeenCalled();
    expect(api.reindexKnowledgeSourceAtRevision).not.toHaveBeenCalled();
  });
  it("cancels without dispatching a write", async () => {
    const api = {getKnowledgeSource: vi.fn().mockResolvedValue(source), reindexKnowledgeSourceAtRevision: vi.fn()} as unknown as ApiClient;
    expect((await applyButlerSourceAction(api, action, () => "project", async () => false)).refresh).toBe(false);
    expect(api.reindexKnowledgeSourceAtRevision).not.toHaveBeenCalled();
  });
  it("rechecks the revision after confirmation and rejects a concurrent change", async () => {
    const api = {getKnowledgeSource: vi.fn().mockResolvedValueOnce(source).mockResolvedValue({...source, revision: 3}), reindexKnowledgeSourceAtRevision: vi.fn()} as unknown as ApiClient;
    await expect(applyButlerSourceAction(api, action, () => "project", async () => true)).rejects.toThrow("source changed");
    expect(api.reindexKnowledgeSourceAtRevision).not.toHaveBeenCalled();
  });
  it("sends exact project and revision and checks ready readback", async () => {
    const api = {getKnowledgeSource: vi.fn().mockResolvedValueOnce(source).mockResolvedValueOnce(source).mockResolvedValue({...source, revision: 3}),
      reindexKnowledgeSourceAtRevision: vi.fn()} as unknown as ApiClient;
    expect((await applyButlerSourceAction(api, action, () => "project", async () => true)).refresh).toBe(true);
    expect(api.reindexKnowledgeSourceAtRevision).toHaveBeenCalledWith(source.id, "project", 2);
  });
  it("requires exact Core absence for removal", async () => {
    const api = {getKnowledgeSource: vi.fn().mockResolvedValueOnce(source).mockResolvedValueOnce(source).mockRejectedValue(new ApiError("missing", 404)),
      deleteKnowledgeSourceAtRevision: vi.fn()} as unknown as ApiClient;
    expect((await applyButlerSourceAction(api, {...action, kind: "source_remove"}, () => "project", async () => true)).message).toContain("original artifact was retained");
    expect(api.deleteKnowledgeSourceAtRevision).toHaveBeenCalledWith(source.id, "project", 2);
  });
});
