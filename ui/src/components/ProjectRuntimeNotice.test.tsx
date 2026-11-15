import { act, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { ApiClient } from "../api/client";
import { ProjectRuntimeRecovery } from "../api/projectRuntime";
import { ProjectRuntimeNotice } from "./ProjectRuntimeNotice";

function notice() {
  const projectRuntime = new ProjectRuntimeRecovery(vi.fn().mockResolvedValue({ answered: false }));
  render(<ProjectRuntimeNotice api={{ projectRuntime } as unknown as ApiClient} />);
  return projectRuntime;
}

describe("ProjectRuntimeNotice", () => {
  it("says a project held open by a view is starting, not idle, and is never an alert", () => {
    const recovery = notice();
    act(() => {
      recovery.holdingView("project-a");
      recovery.unavailable("engagements/project-a/automation-policy", undefined, "idle");
      void recovery.waitForRuntime().catch(() => undefined);
    });
    expect(screen.getByRole("status")).toHaveTextContent("Starting this project");
    expect(screen.getByRole("status")).toHaveTextContent("This view loads when the project is ready.");
    expect(screen.queryByText("This project is idle")).not.toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("keeps the idle wording for a project nothing holds open", () => {
    const recovery = notice();
    act(() => {
      recovery.unavailable("engagements/project-a/automation-policy", undefined, "idle");
      void recovery.waitForRuntime().catch(() => undefined);
    });
    expect(screen.getByRole("status")).toHaveTextContent("This project is idle");
  });

  it("explains a write waiting for a starting project and clears when Core answers it", () => {
    const recovery = notice();
    act(() => {
      recovery.holdingView("project-a");
      recovery.viewRuntime("project-a", "starting");
    });
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
    act(() => recovery.writeStarted());
    expect(screen.getByRole("status")).toHaveTextContent("Starting this project");
    expect(screen.getByRole("status")).toHaveTextContent("Your change finishes when the project is ready.");
    act(() => recovery.writeSettled());
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });
});
