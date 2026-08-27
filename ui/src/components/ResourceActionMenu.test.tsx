import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { ResourceActionMenu } from "./ResourceActionMenu";

const api = vi.hoisted(() => ({ resolveResourceActions: vi.fn() }));
vi.mock("../state/WorkspaceContext", () => ({
  useWorkspace: () => ({ api }),
}));

describe("ResourceActionMenu", () => {
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
