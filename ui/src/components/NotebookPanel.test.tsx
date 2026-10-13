import {fireEvent, render, screen, waitFor} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import {beforeEach, expect, it, vi} from "vitest";
import type {ApiClient} from "../api/client";
import type {NotebookFile} from "../api/types";
import {NotebookPanel} from "./NotebookPanel";

vi.mock("./CodeMirrorSurface", () => ({CodeMirrorSurface: ({value, onChange, onSave, ariaLabel}: {value: string; onChange: (value: string) => void; onSave: () => void; ariaLabel: string}) => <textarea aria-label={ariaLabel} value={value} onChange={event => onChange(event.target.value)} onKeyDown={event => {if ((event.ctrlKey || event.metaKey) && event.key === "s") onSave();}} />}));

const initial = (source = "print(1)", hash = "hash-a"): NotebookFile => ({path: "quick.ipynb", revision: "file-a", cell_revisions: {cell1: hash}, document: {nbformat: 4, nbformat_minor: 5, metadata: {}, cells: [{id: "cell1", cell_type: "code", source, execution_count: null, outputs: []}]}});

let api: Record<string, ReturnType<typeof vi.fn>>;
beforeEach(() => {
  api = {
    notebookStatus: vi.fn(async () => ({projectId: "p", executionMode: "host", hostAccessAcknowledged: true, consentRequired: false, runtimeReady: true, mcpProfileId: "nebula-notebook-p", mcpEnabled: true})),
    listNotebooks: vi.fn(async () => [{path: "quick.ipynb", size: 100, modifiedAt: "2026-10-13T00:00:00Z"}]),
    readNotebook: vi.fn(async () => initial()),
    listNotebookRuns: vi.fn(async () => []),
    saveNotebookCell: vi.fn(async () => initial("print(2)", "hash-b")),
    runNotebookCell: vi.fn(async () => ({id: "run1", engagement_id: "p", notebook_path: "quick.ipynb", cell_id: "cell1", status: "queued", detail: "", revision: 1})),
    readNotebookRun: vi.fn(async () => ({run: {id: "run1", status: "running"}, events: []})),
    watchNotebookRun: vi.fn(async () => {}),
    restartNotebookKernel: vi.fn(async () => ({restarted: true})),
    searchNotebookPackage: vi.fn(async () => ({name: "numpy", version: "2.3.0", summary: "Array computing", url: "https://pypi.org/project/numpy/"})),
  };
});

it("looks up a package name in the project's runtime before installation", async () => {
  const user = userEvent.setup();
  render(<NotebookPanel api={api as unknown as ApiClient} projectId="p" active path="quick.ipynb" onPathChange={vi.fn()} onProjectSettings={vi.fn()} />);
  await screen.findByRole("textbox", {name: "code cell editor"});
  await user.type(screen.getByRole("textbox", {name: /Project Python packages/}), "numpy");
  await user.click(screen.getByRole("button", {name: "Find"}));
  await waitFor(() => expect(api.searchNotebookPackage).toHaveBeenCalledWith("p", "quick.ipynb", "numpy"));
  expect(await screen.findByText(/Array computing/)).toBeVisible();
});

it("saves a changed cell before queueing execution", async () => {
  const user = userEvent.setup();
  render(<NotebookPanel api={api as unknown as ApiClient} projectId="p" active path="quick.ipynb" onPathChange={vi.fn()} onProjectSettings={vi.fn()} />);
  const editor = await screen.findByRole("textbox", {name: "code cell editor"});
  fireEvent.change(editor, {target: {value: "print(2)"}});
  await user.click(screen.getByRole("button", {name: "Run cell"}));
  await waitFor(() => expect(api.saveNotebookCell).toHaveBeenCalledWith("p", expect.objectContaining({cellId: "cell1", source: "print(2)", expectedSourceHash: "hash-a"})));
  expect(api.runNotebookCell).toHaveBeenCalledWith("p", "quick.ipynb", "cell1");
});

it("keeps an unsaved operator draft when an agent changes the same cell", async () => {
  const user = userEvent.setup();
  render(<NotebookPanel api={api as unknown as ApiClient} projectId="p" active path="quick.ipynb" onPathChange={vi.fn()} onProjectSettings={vi.fn()} />);
  const editor = await screen.findByRole("textbox", {name: "code cell editor"});
  fireEvent.change(editor, {target: {value: "print(2)"}});
  api.readNotebook.mockResolvedValue(initial("print(3)", "hash-agent"));
  await user.click(screen.getByRole("button", {name: "Refresh notebook"}));
  expect(await screen.findByRole("alert")).toHaveTextContent(/changed elsewhere/);
  expect(editor).toHaveValue("print(2)");
  expect(screen.getByRole("button", {name: "Save cell"})).toBeDisabled();
});

it("shows authenticated notebook events as they arrive", async () => {
  api.listNotebookRuns.mockResolvedValue([{id: "run1", engagement_id: "p", notebook_path: "quick.ipynb", cell_id: "cell1", status: "running", detail: "", revision: 1}]);
  api.watchNotebookRun.mockImplementation(async (_projectId: string, _runId: string, _cursor: number, onEvent: (event: unknown) => void) => {
    onEvent({sequence: 1, event_type: "notebook.output", payload: {kind: "stream", content: {name: "stdout", text: "live output"}}});
  });
  render(<NotebookPanel api={api as unknown as ApiClient} projectId="p" active path="quick.ipynb" onPathChange={vi.fn()} onProjectSettings={vi.fn()} />);
  expect(await screen.findByText("live output")).toBeVisible();
  expect(api.watchNotebookRun).toHaveBeenCalledWith("p", "run1", 0, expect.any(Function), expect.any(AbortSignal));
});

it("opens a selected notebook while a large workspace inventory is still running", async () => {
  api.listNotebooks.mockImplementation(() => new Promise(() => undefined));
  render(<NotebookPanel api={api as unknown as ApiClient} projectId="p" active path="quick.ipynb" onPathChange={vi.fn()} onProjectSettings={vi.fn()} />);
  expect(await screen.findByRole("textbox", {name: "code cell editor"})).toHaveValue("print(1)");
  expect(screen.getByRole("combobox", {name: "Notebook"})).toHaveValue("quick.ipynb");
  expect(screen.getByRole("combobox", {name: "Notebook"})).toHaveTextContent("Finding notebooks…");
  await new Promise(resolve => setTimeout(resolve, 1600));
  expect(api.listNotebooks).toHaveBeenCalledTimes(1);
  expect(api.readNotebook.mock.calls.length).toBeGreaterThan(1);
});

it("explicit refresh reloads the inventory even when Core has a cached list", async () => {
  const user = userEvent.setup();
  render(<NotebookPanel api={api as unknown as ApiClient} projectId="p" active path="quick.ipynb" onPathChange={vi.fn()} onProjectSettings={vi.fn()} />);
  await screen.findByRole("textbox", {name: "code cell editor"});
  await waitFor(() => expect(api.listNotebooks).toHaveBeenCalledWith("p", false));
  await user.click(screen.getByRole("button", {name: "Refresh notebook"}));
  await waitFor(() => expect(api.listNotebooks).toHaveBeenCalledWith("p", true));
});

it("keeps the selected file usable and retries a failed inventory", async () => {
  api.listNotebooks.mockRejectedValueOnce(new Error("Inventory unavailable"));
  const user = userEvent.setup();
  render(<NotebookPanel api={api as unknown as ApiClient} projectId="p" active path="quick.ipynb" onPathChange={vi.fn()} onProjectSettings={vi.fn()} />);
  expect(await screen.findByRole("textbox", {name: "code cell editor"})).toHaveValue("print(1)");
  expect(await screen.findByRole("alert")).toHaveTextContent("Inventory unavailable");
  await user.click(screen.getByRole("button", {name: "Retry"}));
  await waitFor(() => expect(api.listNotebooks).toHaveBeenCalledWith("p", true));
  await waitFor(() => expect(screen.queryByRole("alert")).not.toBeInTheDocument());
});
