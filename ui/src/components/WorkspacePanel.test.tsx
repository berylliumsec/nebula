import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { ApiError, type ApiClient } from "../api/client";
import { DialogProvider } from "./DialogSystem";
import { WorkspacePanel } from "./WorkspacePanel";

function renderPanel(api: Partial<ApiClient>) {
  return render(<DialogProvider><WorkspacePanel
    api={api as ApiClient}
    engagementId="project-1"
    engagementName="Scratch Project"
  /></DialogProvider>);
}

function listing() {
  return {
    engagementId: "project-1",
    path: "",
    entries: [],
    offset: 0,
    total: 0,
  };
}

describe("WorkspacePanel uploads", () => {
  it("offers right-click file actions and renames without overwriting", async () => {
    const user = userEvent.setup();
    const listWorkspace = vi.fn().mockResolvedValue({
      ...listing(), total: 1, entries: [{
        path: "proof.txt", name: "proof.txt", kind: "file", size: 5,
        modifiedAt: "2026-07-13T12:00:00Z",
      }],
    });
    const renameWorkspaceEntry = vi.fn().mockResolvedValue({ path: "finding.txt", previousPath: "proof.txt" });
    renderPanel({ listWorkspace, renameWorkspaceEntry });

    const entry = await screen.findByRole("button", { name: /proof\.txt/ });
    fireEvent.contextMenu(entry, { clientX: 40, clientY: 50 });
    const menu = await screen.findByRole("menu", { name: "Actions for proof.txt" });
    await user.click(within(menu).getByRole("menuitem", { name: "Rename" }));
    await user.clear(within(menu).getByRole("textbox", { name: "New name" }));
    await user.type(within(menu).getByRole("textbox", { name: "New name" }), "finding.txt");
    await user.click(within(menu).getByRole("button", { name: "Rename" }));

    await waitFor(() => expect(renameWorkspaceEntry).toHaveBeenCalledWith("project-1", "proof.txt", "finding.txt"));
    expect(await screen.findByText("Renamed proof.txt to finding.txt.")).toBeVisible();
  });

  it("uploads a selected file into the open workspace folder", async () => {
    const user = userEvent.setup();
    const uploadWorkspaceFile = vi.fn().mockResolvedValue({
      engagementId: "project-1",
      path: "proof.txt",
      size: 5,
      sha256: "a".repeat(64),
      overwritten: false,
    });
    renderPanel({
      listWorkspace: vi.fn().mockResolvedValue(listing()),
      uploadWorkspaceFile,
    });

    const file = new File(["proof"], "proof.txt", { type: "text/plain" });
    await user.upload(screen.getByLabelText("Choose workspace file"), file);

    await waitFor(() => expect(uploadWorkspaceFile).toHaveBeenCalledWith(
      "project-1",
      "proof.txt",
      file,
      false,
      expect.any(AbortSignal),
    ));
    expect(await screen.findByText(/Uploaded proof\.txt/)).toBeVisible();
  });

  it("requires confirmation before atomically overwriting a file", async () => {
    const user = userEvent.setup();
    const uploadWorkspaceFile = vi.fn()
      .mockRejectedValueOnce(new ApiError("workspace file already exists", 409))
      .mockResolvedValueOnce({
        engagementId: "project-1",
        path: "proof.txt",
        size: 7,
        sha256: "b".repeat(64),
        overwritten: true,
      });
    renderPanel({
      listWorkspace: vi.fn().mockResolvedValue(listing()),
      uploadWorkspaceFile,
    });

    const file = new File(["replace"], "proof.txt", { type: "text/plain" });
    await user.upload(screen.getByLabelText("Choose workspace file"), file);
    const dialog = await screen.findByRole("dialog", { name: "Replace proof.txt?" });
    expect(uploadWorkspaceFile).toHaveBeenCalledTimes(1);
    await user.click(within(dialog).getByRole("button", { name: "Replace file" }));

    await waitFor(() => expect(uploadWorkspaceFile).toHaveBeenLastCalledWith(
      "project-1",
      "proof.txt",
      file,
      true,
      expect.any(AbortSignal),
    ));
    expect(await screen.findByText(/Replaced proof\.txt/)).toBeVisible();
  });

  it("exposes cancellation and aborts an in-flight upload", async () => {
    const user = userEvent.setup();
    const uploadWorkspaceFile = vi.fn((
      _engagementId: string,
      _path: string,
      _file: Blob,
      _overwrite: boolean,
      signal?: AbortSignal,
    ) => new Promise((_resolve, reject) => {
      signal?.addEventListener("abort", () => reject(new DOMException("Aborted", "AbortError")), { once: true });
    }));
    renderPanel({
      listWorkspace: vi.fn().mockResolvedValue(listing()),
      uploadWorkspaceFile: uploadWorkspaceFile as ApiClient["uploadWorkspaceFile"],
    });

    await user.upload(screen.getByLabelText("Choose workspace file"), new File(["large"], "large.bin"));
    await user.click(await screen.findByRole("button", { name: "Cancel upload" }));

    expect(await screen.findByText("Upload of large.bin was cancelled.")).toBeVisible();
    expect(screen.getByRole("button", { name: "Upload file" })).toBeEnabled();
  });

  it("drafts a previewable file for the Assistant without sending it", async () => {
    const user = userEvent.setup();
    const onUseWithAssistant = vi.fn();
    render(<DialogProvider><WorkspacePanel
      api={{
        listWorkspace: vi.fn().mockResolvedValue({
          ...listing(),
          total: 1,
          entries: [{
            path: "proof.txt",
            name: "proof.txt",
            kind: "file",
            size: 5,
            modifiedAt: "2026-07-13T12:00:00Z",
          }],
        }),
        previewWorkspaceFile: vi.fn().mockResolvedValue({
          engagementId: "project-1",
          path: "proof.txt",
          text: "proof",
          bytesReturned: 5,
          truncated: false,
          previewSha256: "a".repeat(64),
        }),
      } as unknown as ApiClient}
      engagementId="project-1"
      engagementName="Scratch Project"
      onUseWithAssistant={onUseWithAssistant}
    /></DialogProvider>);

    await user.click(await screen.findByRole("button", { name: /proof\.txt/ }));
    await user.click(await screen.findByRole("button", { name: "Use with Assistant" }));

    expect(onUseWithAssistant).toHaveBeenCalledWith({
      text: "proof",
      sourceKind: "workspace_file",
      sourceId: "proof.txt",
      sourceLabel: "proof.txt",
      truncated: false,
    });
  });
});
