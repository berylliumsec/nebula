import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import type { HarnessSessionActivity } from "../api/types";
import { HarnessStatusRail } from "./HarnessStatusRail";

const activity: HarnessSessionActivity = {
  sessionId: "harness-1",
  sessionStatus: "running",
  busy: true,
  live: true,
  lastActivityAt: "2026-08-26T12:00:00Z",
  detail: "Working through the plan",
  plan: [
    { id: "inspect", title: "Inspect the current workflow", status: "completed" },
    { id: "implement", title: "Implement the expandable plan summary", status: "in_progress" },
    { id: "verify", title: "Verify keyboard and mobile behavior", status: "pending" },
  ],
};

describe("HarnessStatusRail", () => {
  it("expands all plan steps accessibly and remains expanded across live updates", async () => {
    const user = userEvent.setup();
    const { rerender } = render(<HarnessStatusRail activity={activity} pendingRequests={0} />);
    const toggle = screen.getByRole("button", { name: "Expand plan steps, 1 of 3 completed" });
    expect(screen.queryByRole("list", { name: "Plan steps" })).not.toBeInTheDocument();

    toggle.focus();
    await user.keyboard("{Enter}");
    expect(screen.getByRole("button", { name: "Collapse plan steps, 1 of 3 completed" })).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByRole("list", { name: "Plan steps" })).toBeVisible();
    expect(screen.getByText("Implement the expandable plan summary")).toBeVisible();
    expect(screen.getByText("In progress")).toBeVisible();

    rerender(<HarnessStatusRail activity={{ ...activity, plan: activity.plan?.map((entry) => entry.id === "implement" ? { ...entry, status: "completed" } : entry) }} pendingRequests={0} />);
    expect(screen.getByRole("button", { name: "Collapse plan steps, 2 of 3 completed" })).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByRole("list", { name: "Plan steps" })).toBeVisible();
  });

  it("does not present an empty expansion control when no plan exists", () => {
    render(<HarnessStatusRail activity={{ ...activity, plan: undefined }} pendingRequests={1} />);
    expect(screen.queryByRole("button", { name: /plan steps/i })).not.toBeInTheDocument();
    expect(screen.getByText("Action required")).toBeVisible();
  });
});
