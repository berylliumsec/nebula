import { fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { ActionDescriptor, ResourceRef } from "../api/types";
import { ResourceActionMenu } from "./ResourceActionMenu";

const api = vi.hoisted(() => ({ resolveResourceActions: vi.fn() }));
vi.mock("../state/WorkspaceContext", () => ({
  useWorkspace: () => ({ api }),
}));

const evidence: ResourceRef = { projectId: "project-1", kind: "evidence", id: "evidence-1" };
const available = (id: string): ActionDescriptor => ({
  id, acceptedResourceKinds: ["evidence"], authority: "core", requiredCapabilities: [], risk: "safe", confirmationPolicy: "none", available: true,
});

describe("ResourceActionMenu", () => {
  beforeEach(() => api.resolveResourceActions.mockReset());

  it("resolves actions once even when the parent re-renders with a new adapters object", async () => {
    api.resolveResourceActions.mockResolvedValue([available("open"), available("download")]);
    const view = render(<ResourceActionMenu resource={evidence} adapters={{ download: vi.fn() }} />);
    await screen.findByRole("button", { name: "Actions" });
    view.rerender(<ResourceActionMenu resource={evidence} adapters={{ download: vi.fn() }} />);
    view.rerender(<ResourceActionMenu resource={evidence} adapters={{ download: vi.fn() }} />);
    expect(api.resolveResourceActions).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("button", { name: "Actions" })).toBeVisible();
  });

  it("closes the open menu on Escape and on a pointer press outside it", async () => {
    api.resolveResourceActions.mockResolvedValue([available("open"), available("download")]);
    const user = userEvent.setup();
    render(<ResourceActionMenu resource={evidence} adapters={{ download: vi.fn() }} />);
    const trigger = await screen.findByRole("button", { name: "Actions" });

    await user.click(trigger);
    expect(screen.getByRole("menu", { name: "Resource actions" })).toBeVisible();
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("menu")).toBeNull();
    expect(trigger).toHaveAttribute("aria-expanded", "false");

    await user.click(trigger);
    expect(screen.getByRole("menu", { name: "Resource actions" })).toBeVisible();
    fireEvent.pointerDown(document.body);
    expect(screen.queryByRole("menu")).toBeNull();
  });

  it("uses Core availability and invokes only registered surface adapters", async () => {
    const download = vi.fn();
    api.resolveResourceActions.mockResolvedValue([
      {
        id: "open",
        acceptedResourceKinds: ["evidence"],
        authority: "ui",
        requiredCapabilities: [],
        risk: "safe",
        confirmationPolicy: "none",
        available: true,
      },
      {
        id: "download",
        acceptedResourceKinds: ["evidence"],
        authority: "core",
        requiredCapabilities: [],
        risk: "safe",
        confirmationPolicy: "none",
        available: true,
      },
      {
        id: "copy",
        acceptedResourceKinds: ["evidence"],
        authority: "device",
        requiredCapabilities: ["clipboard.write"],
        risk: "safe",
        confirmationPolicy: "none",
        available: false,
        disabledReason: "No connected device currently provides clipboard.write.",
      },
    ]);
    const user = userEvent.setup();
    render(
      <ResourceActionMenu
        resource={{ projectId: "project-1", kind: "evidence", id: "evidence-1" }}
        adapters={{ download, copy: vi.fn() }}
      />,
    );

    await user.click(await screen.findByRole("button", { name: "Actions" }));
    await user.click(screen.getByRole("menuitem", { name: /Download/ }));
    expect(download).toHaveBeenCalledOnce();

    await user.click(screen.getByRole("button", { name: "Actions" }));
    expect(screen.getByRole("menuitem", { name: /Copy/ })).toBeDisabled();
    expect(screen.queryByRole("menuitem", { name: /Ask Nebula/ })).toBeNull();
  });
});
