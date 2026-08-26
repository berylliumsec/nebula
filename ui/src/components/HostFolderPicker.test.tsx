import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { FormEvent } from "react";
import type { ApiClient } from "../api/client";
import { DialogProvider } from "./DialogSystem";
import { HostFolderPicker } from "./HostFolderPicker";

describe("HostFolderPicker", () => {
  it("creates a child folder, enters it, and returns the selected host path", async () => {
    const api = {
      listHostWorkspaceFolders: vi.fn(async () => ({
        path: "/srv/projects",
        parent: "/srv",
        directories: [],
        truncated: false,
      })),
      createHostWorkspaceFolder: vi.fn(async () => ({
        path: "/srv/projects/acme-review",
        parent: "/srv/projects",
        directories: [],
        truncated: false,
      })),
    } as unknown as ApiClient;
    const onSelect = vi.fn();
    const onParentSubmit = vi.fn((event: FormEvent) => event.preventDefault());
    const user = userEvent.setup();

    render(<DialogProvider><form onSubmit={onParentSubmit}><HostFolderPicker api={api} onSelect={onSelect} /></form></DialogProvider>);
    const browse = screen.getByRole("button", { name: "Browse folders" });
    await user.click(browse);
    const dialog = await screen.findByRole("dialog", { name: "Choose project folder" });
    await user.click(screen.getByRole("button", { name: "New folder" }));
    await user.type(screen.getByRole("textbox", { name: "New folder name" }), "acme-review");
    await user.click(screen.getByRole("button", { name: "Create folder" }));

    await waitFor(() => expect(api.createHostWorkspaceFolder).toHaveBeenCalledWith("/srv/projects", "acme-review"));
    expect(onParentSubmit).not.toHaveBeenCalled();
    expect(dialog).toHaveTextContent("/srv/projects/acme-review");
    await waitFor(() => expect(screen.getByRole("button", { name: "Select folder" })).toHaveFocus());
    await user.click(screen.getByRole("button", { name: "Select folder" }));
    expect(onSelect).toHaveBeenCalledWith("/srv/projects/acme-review");
    expect(screen.queryByRole("dialog", { name: "Choose project folder" })).not.toBeInTheDocument();
    expect(browse).toHaveFocus();
  });

  it("keeps the folder form usable when creation fails and permits a corrected retry", async () => {
    const createHostWorkspaceFolder = vi.fn()
      .mockRejectedValueOnce(new Error("folder already exists"))
      .mockResolvedValueOnce({ path: "/srv/projects/acme-review-2", parent: "/srv/projects", directories: [], truncated: false });
    const api = {
      listHostWorkspaceFolders: vi.fn(async () => ({ path: "/srv/projects", parent: "/srv", directories: [], truncated: false })),
      createHostWorkspaceFolder,
    } as unknown as ApiClient;
    const user = userEvent.setup();

    render(<DialogProvider><HostFolderPicker api={api} onSelect={vi.fn()} /></DialogProvider>);
    await user.click(screen.getByRole("button", { name: "Browse folders" }));
    await user.click(await screen.findByRole("button", { name: "New folder" }));
    const name = screen.getByRole("textbox", { name: "New folder name" });
    await user.type(name, "acme-review");
    await user.click(screen.getByRole("button", { name: "Create folder" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("folder already exists");
    expect(name).toHaveValue("acme-review");
    await waitFor(() => expect(name).toHaveFocus());
    await user.clear(name);
    await user.type(name, "acme-review-2");
    await user.click(screen.getByRole("button", { name: "Create folder" }));
    await waitFor(() => expect(createHostWorkspaceFolder).toHaveBeenLastCalledWith("/srv/projects", "acme-review-2"));
    expect(screen.getByRole("dialog", { name: "Choose project folder" })).toHaveTextContent("/srv/projects/acme-review-2");
  });

  it("loads every page without duplicates and keeps a failed continuation retryable", async () => {
    const firstPage = Array.from({ length: 500 }, (_, index) => ({
      name: `project-${index.toString().padStart(3, "0")}`,
      path: `/projects/project-${index.toString().padStart(3, "0")}`,
    }));
    const listHostWorkspaceFolders = vi.fn()
      .mockResolvedValueOnce({ path: "/projects", parent: "/", directories: firstPage, truncated: true, nextOffset: 500 })
      .mockRejectedValueOnce(new Error("folder page could not be listed"))
      .mockResolvedValueOnce({
        path: "/projects",
        parent: "/",
        directories: [firstPage[499], { name: "project-500", path: "/projects/project-500" }],
        truncated: false,
      });
    const api = { listHostWorkspaceFolders } as unknown as ApiClient;
    const user = userEvent.setup();
    render(<HostFolderPicker api={api} value="/projects" onSelect={vi.fn()} />);

    await user.click(screen.getByRole("button", { name: "Browse folders" }));
    const dialog = await screen.findByRole("dialog", { name: "Choose project folder" });
    const loadMore = within(dialog).getByRole("button", { name: "Load more folders" });
    expect(within(dialog).getByText("500 folders shown.")).toBeVisible();

    await user.click(loadMore);
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("folder page could not be listed");
    expect(within(dialog).getByRole("button", { name: "Try loading more again" })).toHaveFocus();

    await user.click(within(dialog).getByRole("button", { name: "Try loading more again" }));
    expect(await within(dialog).findByRole("button", { name: "project-500" })).toBeVisible();
    expect(within(dialog).getAllByRole("button", { name: "project-499" })).toHaveLength(1);
    expect(within(dialog).queryByRole("button", { name: "Load more folders" })).not.toBeInTheDocument();
    expect(listHostWorkspaceFolders).toHaveBeenNthCalledWith(1, "/projects");
    expect(listHostWorkspaceFolders).toHaveBeenNthCalledWith(2, "/projects", 500);
    expect(listHostWorkspaceFolders).toHaveBeenNthCalledWith(3, "/projects", 500);
  });
});
