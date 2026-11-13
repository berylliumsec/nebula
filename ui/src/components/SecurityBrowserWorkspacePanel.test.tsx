import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, useLocation } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import { SecurityBrowserWorkspacePanel } from "./SecurityBrowserWorkspacePanel";
function Location() { return <output data-testid="location">{useLocation().search}</output>; }
describe("browser tools workspace", () => {
  it("preserves tool content and retires assessment and Intruder deep links", async () => {
    render(<MemoryRouter initialEntries={["/?view=browser&tool=intruder&assessment=old&candidate=old"]}>
      <Location /><SecurityBrowserWorkspacePanel compact desktop={false} onClose={vi.fn()} toolNavigation={<nav>Repeater</nav>}><p>Saved request</p></SecurityBrowserWorkspacePanel>
    </MemoryRouter>);
    expect(screen.getByText("Saved request")).toBeVisible();
    expect(screen.queryByRole("button", { name: /guided|Intruder/i })).not.toBeInTheDocument();
    await waitFor(() => expect(screen.getByTestId("location")).toHaveTextContent("tool=traffic"));
    expect(screen.getByTestId("location")).not.toHaveTextContent("assessment");
    expect(screen.getByText(/This browser feature is no longer available/)).toHaveTextContent("no longer available");
  });
});
