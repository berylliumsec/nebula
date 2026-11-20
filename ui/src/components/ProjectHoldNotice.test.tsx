import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { ApiClient } from "../api/client";
import { ProjectHoldWatch, type ProjectHoldView } from "../api/projectHold";
import { DialogProvider } from "./DialogSystem";
import { ProjectHoldNotice } from "./ProjectHoldNotice";

function client(views: ProjectHoldView[]) {
  const request = vi.fn(async () => views.length > 1 ? views.shift()! : views[0]);
  return { api: { projectHolds: new ProjectHoldWatch(request as never) } as unknown as ApiClient, request };
}

function hold(status: "holding" | "held" | "released", graceSeconds = 160): ProjectHoldView {
  return {
    project_id: "project-1",
    held: status !== "released",
    hold: {
      id: "hold-1", status, reason: "Core supervisor restart", grace_seconds: 180,
      grace_until: new Date(Date.now() + graceSeconds * 1000).toISOString(),
      created_at: new Date().toISOString(),
      paused_goals: Array.from({ length: 12 }, (_, index) => ({ goal_id: `g${index}`, session_id: `s${index}` })),
    },
    active_turns: status === "holding" ? 2 : 0,
    running_goals: 0,
  };
}

function show(api: ApiClient) {
  return render(<DialogProvider><ProjectHoldNotice api={api} projectId="project-1" /></DialogProvider>);
}

describe("ProjectHoldNotice", () => {
  it("renders nothing for an open project", async () => {
    const { api, request } = client([{ project_id: "project-1", held: false, hold: null, active_turns: 0, running_goals: 1 }]);
    const { container } = show(api);
    await waitFor(() => expect(request).toHaveBeenCalled());
    expect(container).toBeEmptyDOMElement();
  });

  it("says running responses may finish while the hold is placed", async () => {
    const { api } = client([hold("holding")]);
    show(api);
    expect(await screen.findByText("Pausing work in this project")).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent(/Running responses can finish for 2m \d\ds, then they stop\. 12 goals resume on their own afterwards\./);
    expect(screen.queryByRole("button", { name: "Release hold" })).not.toBeInTheDocument();
  });

  it("confirms before releasing a held project and hides once released", async () => {
    const { api, request } = client([hold("held"), { ...hold("released"), held: false }]);
    show(api);
    expect(await screen.findByText("Work in this project is paused")).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("12 goals and queued messages resume on their own when it finishes. Saved work is safe.");
    await userEvent.click(screen.getByRole("button", { name: "Release hold" }));
    const dialog = await screen.findByRole("dialog");
    expect(dialog).toHaveTextContent("The 12 goals it paused resume now. Release only if the update that placed it is no longer running. Placed for: Core supervisor restart.");
    await userEvent.click(screen.getByRole("button", { name: "Release" }));
    await waitFor(() => expect(screen.queryByText("Work in this project is paused")).not.toBeInTheDocument());
    expect(request).toHaveBeenLastCalledWith("engagements/project-1/hold/hold-1/release", expect.objectContaining({ method: "POST" }));
  });

  it("keeps the hold when the operator keeps it paused", async () => {
    const { api, request } = client([hold("held")]);
    show(api);
    await userEvent.click(await screen.findByRole("button", { name: "Release hold" }));
    await userEvent.click(await screen.findByRole("button", { name: "Keep paused" }));
    expect(screen.getByText("Work in this project is paused")).toBeInTheDocument();
    expect(request).toHaveBeenCalledTimes(1);
  });
});
