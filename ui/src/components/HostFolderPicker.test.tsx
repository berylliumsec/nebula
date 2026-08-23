import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { ApiClient } from "../api/client";
import { HostFolderPicker } from "./HostFolderPicker";

describe("HostFolderPicker", () => {
  it("keeps an unavailable path actionable and retries from the host home folder", async () => {
    const listHostWorkspaceFolders = vi.fn()
      .mockRejectedValueOnce(new Error("folder is unavailable"))
      .mockResolvedValueOnce({ path: "/home/agent", parent: "/home", directories: [], truncated: false });
    const api = { listHostWorkspaceFolders } as unknown as ApiClient;
    const user = userEvent.setup();
    render(<HostFolderPicker api={api} value="/missing/project" onSelect={vi.fn()} />);

    const browse = screen.getByRole("button", { name: "Browse folders" });
    await user.click(browse);
    const dialog = await screen.findByRole("dialog", { name: "Choose project folder" });
    expect(within(dialog).getByRole("alert")).toHaveTextContent("Folder unavailablefolder is unavailable");
    expect(listHostWorkspaceFolders).toHaveBeenNthCalledWith(1, "/missing/project");

    await user.click(within(dialog).getByRole("button", { name: "Try again" }));
    expect(await within(dialog).findByText("/home/agent", { exact: true })).toBeVisible();
    expect(listHostWorkspaceFolders).toHaveBeenNthCalledWith(2, undefined);

    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog", { name: "Choose project folder" })).not.toBeInTheDocument();
    expect(browse).toHaveFocus();
  });
});
