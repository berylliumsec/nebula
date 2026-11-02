import {beforeEach, describe, expect, it, vi} from "vitest";
import type {ApiClient} from "../api/client";
import {startLibraryArtifactDownload} from "../api/libraryFiles";
import {sha256BlobHex} from "../sha256";
import {applyButlerArtifactDownload} from "./butlerArtifactAction";

vi.mock("../api/libraryFiles", () => ({startLibraryArtifactDownload: vi.fn()}));
vi.mock("../sha256", () => ({sha256BlobHex: vi.fn()}));
const artifact = {id: "artifact", engagement_id: "selected", revision: 2, sha256: "a".repeat(64), size: 4, metadata: {tool_call_id: "call"}};
const owner = {id: "call", engagement_id: "selected", chat_session_id: "chat"};
const chat = {id: "chat", engagement_id: "selected", metadata: {}};
const action = {kind: "artifact_download" as const, projectId: "selected", artifactId: "artifact", expectedRevision: 2, ownerKind: "tool_call" as const, ownerId: "call", sha256: artifact.sha256, size: 4, filename: "out.txt"};
function fixture(readArtifact = () => artifact, readOwner = () => owner, readChat = () => chat) {
  return {request: vi.fn(async (path: string) => {
    if (path === "artifacts/artifact") return readArtifact();
    if (path === "tool-calls/call") return readOwner();
    if (path === "chat-sessions/chat") return readChat();
    throw new Error(`Unexpected Core route: ${path}`);
  }), downloadToolArtifact: vi.fn().mockResolvedValue({blob: {size: 4} as Blob})} as unknown as ApiClient;
}
beforeEach(() => {vi.clearAllMocks(); vi.mocked(sha256BlobHex).mockResolvedValue(artifact.sha256);});

describe("Butler original artifact download", () => {
  it.each([true, false])("validates execution artifact links before transferring original bytes: %s", async linked => {
    const api = {request: vi.fn(async (path: string) => {
      if (path === "artifacts/artifact") return {...artifact, metadata: {execution_id: "execution"}};
      if (path === "executions/execution") return {id: "execution", engagement_id: "selected", stdout_artifact_id: linked ? "artifact" : "other"};
      throw new Error(`Unexpected Core route: ${path}`);
    }),
      downloadToolArtifact: vi.fn().mockResolvedValue({blob: {size: 4} as Blob})} as unknown as ApiClient;
    const target = {...action, ownerKind: "execution" as const, ownerId: "execution"};
    if (linked) {
      expect(await applyButlerArtifactDownload(api, target, () => "selected", async () => true)).toContain("verified original download");
      expect(api.downloadToolArtifact).toHaveBeenCalledOnce();
    } else {
      await expect(applyButlerArtifactDownload(api, target, () => "selected", async () => true)).rejects.toThrow("owner changed");
      expect(api.downloadToolArtifact).not.toHaveBeenCalled();
    }
  });
  it("refuses foreign artifacts before asking or fetching original bytes", async () => {
    const api = fixture(() => ({...artifact, engagement_id: "foreign"}));
    const confirm = vi.fn();
    await expect(applyButlerArtifactDownload(api, action, () => "selected", confirm)).rejects.toThrow("owner changed");
    expect(confirm).not.toHaveBeenCalled(); expect(api.downloadToolArtifact).not.toHaveBeenCalled();
  });
  it("refuses an artifact that belongs to another tool call", async () => {
    const api = fixture(() => ({...artifact, metadata: {tool_call_id: "other-call"}}));
    await expect(applyButlerArtifactDownload(api, action, () => "selected", async () => true)).rejects.toThrow("owner changed");
    expect(api.downloadToolArtifact).not.toHaveBeenCalled();
  });
  it("keeps private conversations outside the download handoff", async () => {
    const api = fixture(undefined, undefined, () => ({...chat, metadata: {control_assistant: true}}));
    await expect(applyButlerArtifactDownload(api, action, () => "selected", async () => true)).rejects.toThrow("visible project conversation");
    expect(api.downloadToolArtifact).not.toHaveBeenCalled();
  });
  it("cancels without downloading", async () => {
    const api = fixture();
    expect(await applyButlerArtifactDownload(api, action, () => "selected", async () => false)).toContain("was not downloaded");
    expect(api.downloadToolArtifact).not.toHaveBeenCalled();
  });
  it("rechecks a changed revision after confirmation", async () => {
    let current = artifact;
    const api = fixture(() => current);
    await expect(applyButlerArtifactDownload(api, action, () => "selected", async () => {current = {...artifact, revision: 3}; return true;})).rejects.toThrow("owner changed");
    expect(api.downloadToolArtifact).not.toHaveBeenCalled();
  });
  it("refuses a project switch while confirming", async () => {
    let project = "selected";
    const api = fixture();
    await expect(applyButlerArtifactDownload(api, action, () => project, async () => {project = "other"; return true;})).rejects.toThrow("original project");
    expect(api.downloadToolArtifact).not.toHaveBeenCalled();
  });
  it("starts a local download only after exact Core ownership, hash and size checks", async () => {
    const api = fixture();
    expect(await applyButlerArtifactDownload(api, action, () => "selected", async () => true)).toContain("verified original download");
    expect(api.downloadToolArtifact).toHaveBeenCalledExactlyOnceWith("artifact");
    expect(startLibraryArtifactDownload).toHaveBeenCalledExactlyOnceWith({size: 4}, "out.txt");
  });
  it("keeps an integrity failure from reaching the save action", async () => {
    const api = fixture();
    vi.mocked(sha256BlobHex).mockResolvedValue("b".repeat(64));
    await expect(applyButlerArtifactDownload(api, action, () => "selected", async () => true)).rejects.toThrow("hash or size");
    expect(startLibraryArtifactDownload).not.toHaveBeenCalled();
  });
});
