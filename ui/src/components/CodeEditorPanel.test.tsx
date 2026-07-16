import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState, type KeyboardEvent } from "react";
import { describe, expect, it, vi } from "vitest";
import { ApiError, type ApiClient } from "../api/client";
import { WorkbenchEditorProvider } from "../state/WorkbenchEditorContext";
import { CodeEditorPanel } from "./CodeEditorPanel";
import { DialogProvider } from "./DialogSystem";

vi.mock("./CodeMirrorSurface", () => ({
  CodeMirrorSurface: ({ value, onChange, onSave }: { value: string; onChange(value: string): void; onSave(): void }) => <textarea
    aria-label="Code editor"
    value={value}
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

function renderPanel(api: Partial<ApiClient>) {
  return render(<DialogProvider><WorkbenchEditorProvider><CodeEditorPanel
    active
    api={api as ApiClient}
    engagementId="project-1"
  /></WorkbenchEditorProvider></DialogProvider>);
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
    expect(await screen.findByText("Saved /workspace/tool.py. Run it from Terminal with its interpreter.")).toBeVisible();
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
});
