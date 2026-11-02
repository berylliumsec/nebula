import {fireEvent, render, screen, waitFor} from "@testing-library/react";
import {beforeEach, expect, it, vi} from "vitest";
import {ButlerSourceAdd} from "./ButlerSourceAdd";

const state = vi.hoisted(() => ({workspace: {} as Record<string, unknown>, hash: vi.fn()}));
vi.mock("../state/WorkspaceContext", () => ({useWorkspace: () => state.workspace}));
vi.mock("../sha256", () => ({sha256BlobHex: state.hash}));

const source = {id: "source", engagementId: "project", name: "document.txt", revision: 1, status: "ready",
  metadata: {sourceUrl: "https://private.invalid/?key=PRIVATE_URL", sha256: "a".repeat(64), size: 5}};

function setup(inputKind: "file" | "url" = "url", project = () => "project") {
  const api = {ingestKnowledgeSource: vi.fn().mockResolvedValue(source), ingestKnowledgeUrlSource: vi.fn().mockResolvedValue(source),
    getKnowledgeSource: vi.fn().mockResolvedValue(source), listKnowledgeSources: vi.fn().mockResolvedValue({items: [source]})};
  const retryResource = vi.fn().mockResolvedValue(undefined);
  state.workspace = {api, coreState: "online", retryResource};
  const callbacks = {onSaved: vi.fn(), onCancel: vi.fn(), onUncertain: vi.fn()};
  render(<ButlerSourceAdd projectId="project" inputKind={inputKind} currentProjectId={project} {...callbacks} />);
  return {api, retryResource, ...callbacks};
}

beforeEach(() => state.hash.mockResolvedValue("a".repeat(64)));

it("cancels private URL input without dispatching ingestion", () => {
  const {api, onCancel} = setup();
  fireEvent.change(screen.getByRole("textbox", {name: "Source URL"}), {target: {value: source.metadata.sourceUrl}});
  fireEvent.click(screen.getByRole("button", {name: "Cancel"}));
  expect(onCancel).toHaveBeenCalledOnce();
  expect(api.ingestKnowledgeUrlSource).not.toHaveBeenCalled();
});

it("verifies exact URL ingestion and list membership without returning private input", async () => {
  const {api, onSaved, retryResource} = setup();
  fireEvent.change(screen.getByRole("textbox", {name: "Source URL"}), {target: {value: source.metadata.sourceUrl}});
  fireEvent.submit(screen.getByRole("button", {name: "Add source"}).closest("form")!);
  await waitFor(() => expect(onSaved).toHaveBeenCalledWith({id: "source", engagementId: "project", name: "document.txt", revision: 1, status: "ready"}, false));
  expect(api.ingestKnowledgeUrlSource).toHaveBeenCalledWith({engagementId: "project", url: source.metadata.sourceUrl});
  expect(JSON.stringify(onSaved.mock.calls)).not.toContain("PRIVATE_URL");
  expect(retryResource).toHaveBeenCalledWith("sources");
});

it("reports uncertain readback without automatically resubmitting", async () => {
  const {api, onSaved, onUncertain} = setup();
  api.getKnowledgeSource.mockRejectedValue(new Error("offline"));
  fireEvent.change(screen.getByRole("textbox", {name: "Source URL"}), {target: {value: source.metadata.sourceUrl}});
  fireEvent.submit(screen.getByRole("button", {name: "Add source"}).closest("form")!);
  await waitFor(() => expect(onUncertain).toHaveBeenCalledOnce());
  expect(api.ingestKnowledgeUrlSource).toHaveBeenCalledTimes(1);
  expect(onSaved).not.toHaveBeenCalled();
  expect(JSON.stringify(onUncertain.mock.calls)).not.toContain("PRIVATE_URL");
});

it("refuses a switched project before sending private input", async () => {
  const {api, onSaved} = setup("url", () => "other");
  fireEvent.change(screen.getByRole("textbox", {name: "Source URL"}), {target: {value: source.metadata.sourceUrl}});
  fireEvent.submit(screen.getByRole("button", {name: "Add source"}).closest("form")!);
  expect(api.ingestKnowledgeUrlSource).not.toHaveBeenCalled();
  expect(onSaved).not.toHaveBeenCalled();
  expect(await screen.findByText(/Reconnect and select the original project/)).toBeVisible();
});
