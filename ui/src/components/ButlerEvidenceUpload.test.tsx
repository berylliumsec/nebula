import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, it, vi } from "vitest";
import type { EvidenceSummary } from "../api/types";
import { ButlerEvidenceUpload } from "./ButlerEvidenceUpload";

const state = vi.hoisted(() => ({workspace: {} as Record<string, unknown>, hash: vi.fn()}));
vi.mock("../state/WorkspaceContext", () => ({useWorkspace: () => state.workspace}));
vi.mock("../sha256", () => ({sha256BlobHex: state.hash}));

const projectId = "project-1";
const evidenceId = "12345678-1234-1234-1234-123456789abc";

function evidence(): EvidenceSummary {
  return {
    id: evidenceId, revision: 1, engagementId: projectId, evidenceType: "operator_upload",
    title: "Operator proof", description: "From the device", artifactId: "artifact-1",
    findingId: "finding-1", assetIds: ["asset-1"], sha256: "a".repeat(64),
    capturedAt: "2026-10-30T10:00:00Z", createdAt: "2026-10-30T10:00:00Z",
    updatedAt: "2026-10-30T10:00:00Z", metadata: {filename: "proof.txt", size: 5},
  };
}

function setup() {
  const saved = evidence();
  const api = {
    uploadEvidence: vi.fn().mockResolvedValue(saved),
    getEvidence: vi.fn().mockResolvedValue(saved),
    listEvidence: vi.fn().mockResolvedValue({items: [saved], total: 1}),
  };
  const retryResource = vi.fn().mockResolvedValue(undefined);
  state.workspace = {
    api, coreState: "online", activeOperator: {id: "operator-1", displayName: "Avery"},
    assets: [{id: "asset-1", displayName: "Example server"}],
    findings: [{id: "finding-1", title: "Finding one"}], retryResource,
  };
  const onSaved = vi.fn();
  const onCancel = vi.fn();
  const onUncertain = vi.fn();
  const user = userEvent.setup();
  render(<ButlerEvidenceUpload projectId={projectId} currentProjectId={() => projectId}
    onSaved={onSaved} onCancel={onCancel} onUncertain={onUncertain} />);
  return {api, retryResource, onSaved, onCancel, onUncertain, user};
}

beforeEach(() => state.hash.mockReset().mockResolvedValue("a".repeat(64)));

it("stores a browser-selected file with reviewed links and confirms Core detail and list", async () => {
  const {api, onSaved, onUncertain, user} = setup();
  const file = new File(["proof"], "proof.txt", {type: "text/plain"});
  Object.defineProperty(file, "arrayBuffer", {value: async () => new TextEncoder().encode("proof").buffer});
  await user.upload(screen.getByLabelText("Choose project evidence file"), file);
  await user.clear(screen.getByRole("textbox", {name: "Title"}));
  await user.type(screen.getByRole("textbox", {name: "Title"}), "Operator proof");
  await user.type(screen.getByRole("textbox", {name: "Description"}), "From the device");
  await user.selectOptions(screen.getByRole("combobox", {name: "Finding"}), "finding-1");
  await user.click(screen.getByRole("checkbox", {name: "Example server"}));
  fireEvent.submit(screen.getByRole("button", {name: "Store evidence"}).closest("form")!);
  await waitFor(() => expect(onSaved).toHaveBeenCalledWith(evidence(), false));
  expect(onUncertain).not.toHaveBeenCalled();
  expect(api.uploadEvidence).toHaveBeenCalledWith(expect.objectContaining({
    engagementId: projectId, filename: "proof.txt", title: "Operator proof",
    description: "From the device", findingId: "finding-1", assetIds: ["asset-1"],
    capturedBy: "operator-1", contentBase64: "cHJvb2Y=",
  }));
  expect(api.getEvidence).toHaveBeenCalledWith(evidenceId);
  expect(api.listEvidence).toHaveBeenCalledWith(projectId);
});

it("cancels before transferring a file", async () => {
  const {api, onCancel, user} = setup();
  await user.click(screen.getByRole("button", {name: "Cancel"}));
  expect(onCancel).toHaveBeenCalledOnce();
  expect(api.uploadEvidence).not.toHaveBeenCalled();
});

it("reports an uncertain upload without claiming a saved result or retrying", async () => {
  const {api, onSaved, onUncertain, user} = setup();
  api.uploadEvidence.mockRejectedValueOnce(new Error("connection lost"));
  const file = new File(["proof"], "proof.txt", {type: "text/plain"});
  Object.defineProperty(file, "arrayBuffer", {value: async () => new TextEncoder().encode("proof").buffer});
  await user.upload(screen.getByLabelText("Choose project evidence file"), file);
  fireEvent.submit(screen.getByRole("button", {name: "Store evidence"}).closest("form")!);
  await waitFor(() => expect(onUncertain).toHaveBeenCalledWith(expect.stringContaining("Inspect this project's Evidence list")));
  expect(onSaved).not.toHaveBeenCalled();
  expect(api.uploadEvidence).toHaveBeenCalledTimes(1);
});
