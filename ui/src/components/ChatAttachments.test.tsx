import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { ApiClient } from "../api/client";
import type { WorkspaceEntry } from "../api/types";
import { ChatAttachments } from "./ChatAttachments";

function entry(name: string): WorkspaceEntry {
  return { path: name, name, kind: "file", size: 5, modifiedAt: "2026-07-13T12:00:00Z" };
}

describe("ChatAttachments", () => {
  it("appends the next page of project files instead of replacing the first page", async () => {
    const user = userEvent.setup();
    const listWorkspace = vi.fn()
      .mockResolvedValueOnce({ engagementId: "project-1", path: "", entries: [entry("alpha.txt"), entry("beta.txt")], offset: 0, nextOffset: 2, total: 3 })
      .mockResolvedValueOnce({ engagementId: "project-1", path: "", entries: [entry("gamma.txt")], offset: 2, total: 3 });
    render(<ChatAttachments api={{ listWorkspace } as unknown as ApiClient} projectId="project-1" onAttach={vi.fn()} onImages={vi.fn()} imagesEnabled={false} />);

    await user.click(screen.getByRole("button", { name: "Attach files" }));
    await user.click(screen.getByRole("button", { name: "Browse project files" }));
    expect(await screen.findByRole("button", { name: "beta.txt" })).toBeVisible();

    await user.click(screen.getByRole("button", { name: "More files" }));
    expect(await screen.findByRole("button", { name: "gamma.txt" })).toBeVisible();
    expect(listWorkspace).toHaveBeenLastCalledWith("project-1", "", 2);
    expect(screen.getByRole("button", { name: "alpha.txt" })).toBeVisible();
    expect(screen.getByRole("button", { name: "beta.txt" })).toBeVisible();
    expect(screen.queryByRole("button", { name: "More files" })).toBeNull();
  });
});
