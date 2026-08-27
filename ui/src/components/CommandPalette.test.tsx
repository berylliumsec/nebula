import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import type { ApiClient } from "../api/client";
import type { SearchResponse } from "../api/types";
import { CommandPalette } from "./CommandPalette";

const response: SearchResponse = {
  items: [{
    ref: { projectId: "project-1", kind: "asset", id: "asset-1", revision: 2 },
    project: "Alpha",
    label: "Gateway",
    description: "edge.alpha.test",
    snippet: "critical external host",
    breadcrumb: "Assets",
    updatedAt: new Date().toISOString(),
    score: 390,
    actions: [{
      id: "open", acceptedResourceKinds: ["asset"], authority: "ui",
      requiredCapabilities: [], risk: "safe", confirmationPolicy: "none", available: true,
    }],
  }],
  partialIndex: false,
};

function renderPalette(api: ApiClient) {
  return render(<MemoryRouter><CommandPalette
    api={api}
    activeProjectId="project-1"
    open
    onClose={vi.fn()}
    onToggleActivity={vi.fn()}
    onToggleSidebar={vi.fn()}
    onOpenSetting={vi.fn()}
  /></MemoryRouter>);
}

describe("CommandPalette federated search", () => {
  it("merges Core resources into the existing page, setting, and action search", async () => {
    const searchResources = vi.fn().mockResolvedValue(response);
    renderPalette({ searchResources } as unknown as ApiClient);
    fireEvent.change(screen.getByLabelText("Search pages, actions, and settings"), { target: { value: "gateway" } });
    expect(await screen.findByRole("option", { name: /Gateway/ })).toBeVisible();
    expect(screen.getByRole("option", { name: /Search project files for/ })).toBeVisible();
    expect(searchResources).toHaveBeenCalledWith(
      expect.objectContaining({ activeProject: "project-1", scope: "active" }),
      expect.any(AbortSignal),
    );
    fireEvent.keyDown(screen.getByLabelText("Search pages, actions, and settings"), { key: "ArrowRight" });
    expect(screen.getByRole("menu", { name: "Actions for Gateway" })).toBeVisible();
    expect(screen.getByRole("menuitem", { name: /open/i })).toBeEnabled();
  });

  it("keeps local commands available when Core search is offline", async () => {
    renderPalette({ searchResources: vi.fn().mockRejectedValue(new Error("offline")) } as unknown as ApiClient);
    fireEvent.change(screen.getByLabelText("Search pages, actions, and settings"), { target: { value: "settings" } });
    await waitFor(() => expect(screen.getByText(/Core search is offline/)).toBeVisible());
    expect(screen.getByRole("option", { name: /Go to Settings/i })).toBeVisible();
  });
});
