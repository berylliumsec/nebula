import "fake-indexeddb/auto";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState, type KeyboardEvent } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError, type ApiClient } from "../api/client";
import { WorkbenchEditorProvider } from "../state/WorkbenchEditorContext";
import { CodeEditorPanel } from "./CodeEditorPanel";
import { DialogProvider } from "./DialogSystem";
import { clearEditorSessions, loadEditorSessions } from "../state/editorSessionPersistence";

beforeEach(() => {
  localStorage.clear();
  return clearEditorSessions();
});

vi.mock("./CodeMirrorSurface", () => ({
  CodeMirrorSurface: ({ ariaLabel = "Code editor", value, findRequest, fontSize, tabSize, wordWrap, onChange, onFocus, onSave }: { ariaLabel?: string; value: string; findRequest?: number; fontSize?: number; tabSize?: number; wordWrap?: boolean; onChange(value: string): void; onFocus?(): void; onSave(): void }) => <textarea
    aria-label={ariaLabel}
    data-find-request={findRequest}
    data-font-size={fontSize}
    data-tab-size={tabSize}
    data-word-wrap={wordWrap}
    value={value}
    onFocus={onFocus}
    onChange={(event) => onChange(event.target.value)}
    onKeyDown={(event: KeyboardEvent<HTMLTextAreaElement>) => {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "s") {
        event.preventDefault();
        onSave();
      }
    }}
  />,
  languageLabelForPath: (path: string) => path.endsWith(".py") ? "Python" : "Plain text",
}));

const pythonEntry = {
  path: "tool.py",
  name: "tool.py",
  kind: "file" as const,
  size: 15,
  modifiedAt: "2026-07-16T12:00:00Z",
};

function listing(entries = [pythonEntry]) {
  return { engagementId: "project-1", path: "", entries, offset: 0, total: entries.length };
}

function panel(api: Partial<ApiClient>, active = true) {
  return <DialogProvider><WorkbenchEditorProvider><CodeEditorPanel
    active={active}
    api={api as ApiClient}
    engagementId="project-1"
  /></WorkbenchEditorProvider></DialogProvider>;
}

function renderPanel(api: Partial<ApiClient>) {
  return render(panel(api));
}

describe("CodeEditorPanel", () => {
  it("opens, edits, and conditionally saves the shared workspace file", async () => {
    const user = userEvent.setup();
    const uploadWorkspaceFile = vi.fn().mockResolvedValue({
      engagementId: "project-1",
      path: "tool.py",
      size: 16,
      sha256: "b".repeat(64),
      overwritten: true,
    });
    renderPanel({
      listWorkspace: vi.fn().mockResolvedValue(listing()),
      downloadWorkspaceFile: vi.fn().mockResolvedValue(new Blob(["print('first')\n"])),
      uploadWorkspaceFile,
    });

    await user.click(await screen.findByRole("button", { name: /tool\.py/ }));
    const editor = await screen.findByRole("textbox", { name: "Code editor" });
    await user.clear(editor);
    await user.type(editor, "print('saved')");
    await user.keyboard("{Control>}s{/Control}");

    await waitFor(() => expect(uploadWorkspaceFile).toHaveBeenCalledTimes(1));
    const args = uploadWorkspaceFile.mock.calls[0];
    expect(args.slice(0, 2)).toEqual(["project-1", "tool.py"]);
    expect(args[3]).toBe(true);
    expect(args[4]).toBeUndefined();
    expect(args[5]).toMatch(/^[a-f0-9]{64}$/);
    expect(await (args[2] as Blob).text()).toBe("print('saved')");
    expect(await screen.findByText("Saved /workspace/tool.py. Use it from Terminal when you're ready.")).toBeVisible();
  });

  it("retains a stale draft and requires explicit confirmation to force overwrite", async () => {
    const user = userEvent.setup();
    const uploadWorkspaceFile = vi.fn()
      .mockRejectedValueOnce(new ApiError("workspace file changed", 412))
      .mockResolvedValueOnce({ engagementId: "project-1", path: "tool.py", size: 14, sha256: "c".repeat(64), overwritten: true });
    renderPanel({
      listWorkspace: vi.fn().mockResolvedValue(listing()),
      downloadWorkspaceFile: vi.fn().mockResolvedValue(new Blob(["print('first')\n"])),
      uploadWorkspaceFile,
    });

    await user.click(await screen.findByRole("button", { name: /tool\.py/ }));
    const editor = await screen.findByRole("textbox", { name: "Code editor" });
    await user.clear(editor);
    await user.type(editor, "print('draft')");
    await user.click(screen.getByRole("button", { name: "Save" }));

    expect(await screen.findByText("Newer workspace version detected")).toBeVisible();
    expect(editor).toHaveValue("print('draft')");
    await user.click(screen.getByRole("button", { name: "Force overwrite" }));
    const dialog = await screen.findByRole("dialog", { name: "Overwrite the newer workspace file?" });
    await user.click(within(dialog).getByRole("button", { name: "Overwrite file" }));

    await waitFor(() => expect(uploadWorkspaceFile).toHaveBeenCalledTimes(2));
    expect(uploadWorkspaceFile.mock.calls[1][5]).toBeUndefined();
    expect(await (uploadWorkspaceFile.mock.calls[1][2] as Blob).text()).toBe("print('draft')");
  });

  it("keeps one unsaved engagement buffer when the editor route unmounts", async () => {
    const user = userEvent.setup();
    const api = { listWorkspace: vi.fn().mockResolvedValue(listing([])) } as unknown as ApiClient;
    function Harness() {
      const [visible, setVisible] = useState(true);
      return <WorkbenchEditorProvider><button type="button" onClick={() => setVisible((value) => !value)}>Toggle editor</button>{visible && <CodeEditorPanel active api={api} engagementId="project-1" />}</WorkbenchEditorProvider>;
    }
    render(<DialogProvider><Harness /></DialogProvider>);

    await user.click((await screen.findAllByRole("button", { name: "New file" }))[0]);
    await user.type(screen.getByRole("textbox", { name: "Code editor" }), "print('persisted draft')");
    await user.click(screen.getByRole("button", { name: "Toggle editor" }));
    expect(screen.queryByRole("textbox", { name: "Code editor" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Toggle editor" }));
    expect(await screen.findByRole("textbox", { name: "Code editor" })).toHaveValue("print('persisted draft')");
  });

  it("restores an exact dirty draft after the complete editor provider remounts", async () => {
    const api = { listWorkspace: vi.fn().mockResolvedValue(listing([])) } as unknown as ApiClient;
    const first = render(panel(api));
    const user = userEvent.setup();

    await user.click((await screen.findAllByRole("button", { name: "New file" }))[0]);
    fireEvent.change(screen.getByRole("textbox", { name: "File path" }), { target: { value: "research/notes.txt" } });
    fireEvent.change(screen.getByRole("textbox", { name: "Code editor" }), { target: { value: "exact λ draft\nsecond line" } });
    await waitFor(async () => expect((await loadEditorSessions())["project-1"]?.buffers[0]?.content).toBe("exact λ draft\nsecond line"), { timeout: 2_000 });
    first.unmount();

    render(panel(api));
    expect(await screen.findByRole("textbox", { name: "File path" })).toHaveValue("research/notes.txt");
    expect(screen.getByRole("textbox", { name: "Code editor" })).toHaveValue("exact λ draft\nsecond line");
    expect(screen.getByText(/^1 open · 1 unsaved · recovery on$/)).toBeVisible();
  });

  it("keeps two pane drafts independent and follows the focused pane", async () => {
    const user = userEvent.setup();
    renderPanel({
      listWorkspace: vi.fn().mockResolvedValue(listing()),
      downloadWorkspaceFile: vi.fn().mockResolvedValue(new Blob(["print('first')\n"])),
    });

    await user.click(await screen.findByRole("button", { name: /tool\.py/ }));
    await user.click(screen.getByRole("button", { name: "New editor file" }));
    await user.click(screen.getByRole("button", { name: "Split" }));
    const primary = screen.getByRole("textbox", { name: "Primary code editor: untitled.txt" });
    const secondary = screen.getByRole("textbox", { name: "Secondary code editor: tool.py" });
    fireEvent.change(primary, { target: { value: "primary draft" } });
    fireEvent.change(secondary, { target: { value: "secondary draft" } });
    fireEvent.focus(secondary);
    expect(screen.getByRole("textbox", { name: "File path" })).toHaveValue("tool.py");
    fireEvent.focus(primary);
    expect(screen.getByRole("textbox", { name: "File path" })).toHaveValue("untitled.txt");
    await user.click(screen.getByRole("button", { name: "Close split editor" }));
    expect(screen.getAllByRole("textbox", { name: "Code editor" })).toHaveLength(1);
    expect(screen.getByRole("textbox", { name: "Code editor" })).toHaveValue("primary draft");
    expect(within(screen.getByRole("tablist", { name: "Open editor files" })).getAllByRole("tab")).toHaveLength(2);
  });

  it("applies and retains device-local editor settings", async () => {
    const user = userEvent.setup();
    renderPanel({ listWorkspace: vi.fn().mockResolvedValue(listing([])) });
    await user.click((await screen.findAllByRole("button", { name: "New file" }))[0]);
    await user.click(screen.getByRole("button", { name: "Editor settings" }));
    const dialog = screen.getByRole("dialog", { name: "Editor settings and keybindings" });
    await user.selectOptions(within(dialog).getByLabelText("Font size"), "16");
    await user.selectOptions(within(dialog).getByLabelText("Tab size"), "4");
    await user.click(within(dialog).getByRole("checkbox", { name: /Word wrap/ }));
    await user.click(within(dialog).getByRole("button", { name: "Apply settings" }));
    const editor = screen.getByRole("textbox", { name: "Code editor" });
    expect(editor).toHaveAttribute("data-font-size", "16");
    expect(editor).toHaveAttribute("data-tab-size", "4");
    expect(editor).toHaveAttribute("data-word-wrap", "true");
    expect(localStorage.getItem("nebula.editor.preferences.v1")).toContain('"fontSize":16');
  });

  it("refreshes workspace files when the persistent editor becomes active again", async () => {
    const terminalEntry = {
      path: "from-terminal.py",
      name: "from-terminal.py",
      kind: "file" as const,
      size: 10,
      modifiedAt: "2026-07-16T12:01:00Z",
    };
    const listWorkspace = vi.fn()
      .mockResolvedValueOnce(listing())
      .mockResolvedValueOnce(listing([pythonEntry, terminalEntry]));
    const api = { listWorkspace };
    const view = render(panel(api));

    expect(await screen.findByRole("button", { name: /tool\.py/ })).toBeVisible();
    view.rerender(panel(api, false));
    view.rerender(panel(api, true));

    expect(await screen.findByRole("button", { name: /from-terminal\.py/ })).toBeVisible();
    expect(listWorkspace).toHaveBeenCalledTimes(2);
  });

  it("progressively discloses mobile files and secondary editor actions", async () => {
    const user = userEvent.setup();
    renderPanel({ listWorkspace: vi.fn().mockResolvedValue(listing([])) });

    await user.click((await screen.findAllByRole("button", { name: "New file" }))[0]);
    const panelElement = screen.getByRole("textbox", { name: "Code editor" }).closest(".code-editor-panel");
    expect(panelElement).toHaveClass("has-buffer");
    expect(panelElement).not.toHaveClass("mobile-files-open");

    const more = screen.getByRole("button", { name: "More editor actions" });
    expect(more).toHaveAttribute("aria-expanded", "false");
    await user.click(more);
    expect(more).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByLabelText("Editor options")).toBeInTheDocument();
    await user.click(within(screen.getByLabelText("Editor options")).getByRole("button", { name: "Find" }));
    expect(screen.getByRole("textbox", { name: "Code editor" })).toHaveAttribute("data-find-request", "1");
    expect(screen.queryByLabelText("Editor options")).not.toBeInTheDocument();

    await user.click(more);
    await user.click(screen.getByRole("button", { name: "Show editor files" }));
    expect(panelElement).toHaveClass("mobile-files-open");
    expect(screen.queryByLabelText("Editor options")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Hide editor files" }));
    expect(panelElement).not.toHaveClass("mobile-files-open");
  });

  it("refuses binary workspace content", async () => {
    const user = userEvent.setup();
    renderPanel({
      listWorkspace: vi.fn().mockResolvedValue(listing()),
      downloadWorkspaceFile: vi.fn().mockResolvedValue(new Blob([new Uint8Array([65, 0, 66])])),
    });
    await user.click(await screen.findByRole("button", { name: /tool\.py/ }));
    expect(await screen.findByText("This file appears to be binary and cannot be edited as text.")).toBeVisible();
    expect(screen.queryByRole("textbox", { name: "Code editor" })).not.toBeInTheDocument();
  });

  it("keeps independent dirty drafts while switching between editor tabs", async () => {
    const user = userEvent.setup();
    renderPanel({
      listWorkspace: vi.fn().mockResolvedValue(listing()),
      downloadWorkspaceFile: vi.fn().mockResolvedValue(new Blob(["print('first')\n"])),
    });

    await user.click(await screen.findByRole("button", { name: /tool\.py/ }));
    const editor = await screen.findByRole("textbox", { name: "Code editor" });
    await user.clear(editor);
    await user.type(editor, "print('dirty first')");
    expect(within(screen.getByRole("tablist", { name: "Open editor files" })).getAllByRole("tab").map((tab) => tab.textContent)).toEqual(["tool.py"]);
    await user.click(screen.getByRole("button", { name: "New editor file" }));
    expect(within(screen.getByRole("tablist", { name: "Open editor files" })).getAllByRole("tab").map((tab) => tab.textContent)).toEqual(["tool.py", "untitled.txt"]);
    const secondEditor = screen.getByRole("textbox", { name: "Code editor" });
    fireEvent.change(secondEditor, { target: { value: "second draft" } });

    expect(within(screen.getByRole("tablist", { name: "Open editor files" })).getAllByRole("tab").map((tab) => tab.textContent)).toEqual(["tool.py", "untitled.txt"]);
    await user.click(screen.getByRole("tab", { name: /tool\.py/ }));
    expect(screen.getByRole("textbox", { name: "Code editor" })).toHaveValue("print('dirty first')");
    await user.click(screen.getByRole("tab", { name: /untitled\.txt/i }));
    expect(screen.getByRole("textbox", { name: "Code editor" })).toHaveValue("second draft");
    expect(screen.getByText(/^2 open · 2 unsaved ·/)).toBeVisible();
  });

  it("quick-opens a recursive workspace result without discarding the current tab", async () => {
    const user = userEvent.setup();
    const searchWorkspace = vi.fn().mockResolvedValue({
      engagementId: "project-1",
      query: "scanner",
      mode: "files",
      matches: [{ path: "src/scanner.py", kind: "path", preview: "src/scanner.py" }],
      scannedFiles: 8,
      truncated: false,
    });
    const downloadWorkspaceFile = vi.fn()
      .mockResolvedValueOnce(new Blob(["print('first')\n"]))
      .mockResolvedValueOnce(new Blob(["def scan():\n    pass\n"]));
    renderPanel({ listWorkspace: vi.fn().mockResolvedValue(listing()), downloadWorkspaceFile, searchWorkspace });

    await user.click(await screen.findByRole("button", { name: /tool\.py/ }));
    await user.click(screen.getByRole("button", { name: "Open" }));
    const dialog = await screen.findByRole("dialog", { name: "Quick open" });
    const searchInput = within(dialog).getByRole("textbox", { name: "Find a workspace file" });
    await user.click(searchInput);
    await user.type(searchInput, "scanner");
    await user.click(await within(dialog).findByRole("option", { name: /src\/scanner\.py/ }));

    expect(await screen.findByRole("tab", { name: /scanner\.py/ })).toHaveAttribute("aria-selected", "true");
    expect(within(screen.getByRole("tablist", { name: "Open editor files" })).getAllByRole("tab")).toHaveLength(2);
    expect(screen.getByRole("textbox", { name: "Code editor" })).toHaveValue("def scan():\n    pass\n");
    expect(searchWorkspace).toHaveBeenCalledWith("project-1", "scanner", "files", "", expect.any(AbortSignal));
  });

  it("shows repository changes and a hardened diff without duplicating Git mutations", async () => {
    const user = userEvent.setup();
    const sourceControlStatus = vi.fn().mockResolvedValue({
      engagementId: "project-1",
      state: "ready",
      branch: "research/fix",
      head: "abcdef123456",
      files: [{
        path: "tool.py",
        indexStatus: "unmodified",
        worktreeStatus: "modified",
      }],
      truncated: false,
      detail: "1 changed path.",
    });
    const sourceControlDiff = vi.fn().mockResolvedValue({
      engagementId: "project-1",
      path: "tool.py",
      staged: false,
      text: "@@ -1 +1 @@\n-print('old')\n+print('new')",
      truncated: false,
      head: "abcdef123456",
    });
    renderPanel({
      listWorkspace: vi.fn().mockResolvedValue(listing()),
      sourceControlStatus,
      sourceControlDiff,
    });

    await user.click(screen.getByRole("tab", { name: "Changes" }));
    expect(await screen.findByText("research/fix")).toBeVisible();
    expect(screen.getByText("modified")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Working diff" }));

    const dialog = await screen.findByRole("dialog", { name: "tool.py" });
    expect(within(dialog).getByLabelText("Diff for tool.py")).toHaveTextContent("+print('new')");
    expect(within(dialog).queryByRole("button", { name: /stage|commit|push/i })).not.toBeInTheDocument();
    expect(sourceControlStatus).toHaveBeenCalledWith("project-1", expect.any(AbortSignal));
    expect(sourceControlDiff).toHaveBeenCalledWith("project-1", "tool.py", false);
  });

  it("discovers project tasks and hands exact commands to reviewed execution", async () => {
    const user = userEvent.setup();
    const onRun = vi.fn();
    const api = {
      listWorkspace: vi.fn().mockResolvedValue(listing()),
      workspaceTasks: vi.fn().mockResolvedValue({
        engagementId: "project-1",
        scannedEntries: 12,
        truncated: false,
        tasks: [{ id: "a".repeat(64), label: "pytest: all discovered tests", command: "python -m pytest", kind: "test", source: "pytest", detail: "3 test files discovered" }],
      }),
    } as unknown as ApiClient;
    render(<DialogProvider><WorkbenchEditorProvider><CodeEditorPanel active api={api} engagementId="project-1" onRun={onRun} /></WorkbenchEditorProvider></DialogProvider>);

    await user.click(await screen.findByRole("button", { name: "Project tasks" }));
    const dialog = await screen.findByRole("dialog", { name: "Project tasks and tests" });
    await user.click(within(dialog).getByRole("option", { name: /pytest: all discovered tests/ }));

    expect(onRun).toHaveBeenCalledWith(expect.objectContaining({
      source: "set -eu\npython -m pytest\n",
      language: "bash",
      origin: expect.objectContaining({ sourceKind: "workspace_task", sourceLabel: "pytest: all discovered tests" }),
    }));
  });
});
