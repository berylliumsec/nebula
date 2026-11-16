import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, expect, it, vi } from "vitest";
import { App } from "./App";
import type { HealthResponse } from "./api/types";

const workspace = vi.hoisted(() => ({health: {apiReady: false} as Partial<HealthResponse>, reconnect: () => {}}));
vi.mock("./state/WorkspaceContext", () => ({useWorkspace: () => workspace}));
vi.mock("./components/AppShell", () => ({AppShell: () => { throw new Error("Catalog-dependent shell must not mount during host recovery"); }}));
vi.mock("./components/MemoryBudgetSettings", () => ({MemoryBudgetSettings: () => <section aria-label="Memory budgets">Memory budget controls</section>}));
vi.mock("./components/RuntimeContinuityNotice", () => ({RuntimeContinuityNotice: () => null}));

beforeEach(() => {
  workspace.health = {apiReady: false};
  workspace.reconnect = vi.fn();
});

it("exposes memory settings from host-only health without mounting catalog-dependent pages", async () => {
  render(<MemoryRouter initialEntries={["/projects/missing/workbench?view=chat"]}><App /></MemoryRouter>);
  expect(screen.getByRole("heading", {name: "Core recovery"})).toBeVisible();
  await userEvent.click(screen.getByRole("link", {name: "Memory Settings"}));
  expect(await screen.findByRole("region", {name: "Memory budgets"})).toBeVisible();
  expect(screen.getByRole("button", {name: "Check workspace availability"})).toBeVisible();
});

it("keeps memory recovery for an API admission Core refused", () => {
  workspace.health = {apiReady: false, apiState: "unavailable", apiAdmission: "held"};
  render(<MemoryRouter><App /></MemoryRouter>);
  expect(screen.getByRole("heading", {name: "Core recovery"})).toBeVisible();
  expect(screen.getByRole("link", {name: "Memory Settings"})).toBeVisible();
});

it.each([["starting", "Core is starting"], ["stopping", "Core is stopping"]] as const)(
  "does not blame the memory budget while Core is %s",
  async (apiAdmission, heading) => {
    workspace.health = {apiReady: false, apiState: "unavailable", apiAdmission};
    render(<MemoryRouter initialEntries={["/projects/p/workbench?view=chat"]}><App /></MemoryRouter>);
    expect(screen.getByRole("heading", {name: heading})).toBeVisible();
    expect(screen.getByRole("status")).toHaveTextContent("Saved work and unsent drafts are kept.");
    expect(screen.queryByRole("link", {name: "Memory Settings"})).toBeNull();
    expect(screen.queryByText(/memory budget/i)).toBeNull();
    await userEvent.click(screen.getByRole("button", {name: "Check workspace availability"}));
    expect(workspace.reconnect).toHaveBeenCalledTimes(1);
  },
);
