import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { expect, it, vi } from "vitest";
import { App } from "./App";

vi.mock("./state/WorkspaceContext", () => ({useWorkspace: () => ({health: {apiReady: false}, reconnect: vi.fn()})}));
vi.mock("./components/AppShell", () => ({AppShell: () => { throw new Error("Catalog-dependent shell must not mount during host recovery"); }}));
vi.mock("./components/MemoryBudgetSettings", () => ({MemoryBudgetSettings: () => <section aria-label="Memory budgets">Memory budget controls</section>}));
vi.mock("./components/RuntimeContinuityNotice", () => ({RuntimeContinuityNotice: () => null}));

it("exposes memory settings from host-only health without mounting catalog-dependent pages", async () => {
  render(<MemoryRouter initialEntries={["/projects/missing/workbench?view=chat"]}><App /></MemoryRouter>);
  expect(screen.getByRole("heading", {name: "Core recovery"})).toBeVisible();
  await userEvent.click(screen.getByRole("link", {name: "Memory Settings"}));
  expect(await screen.findByRole("region", {name: "Memory budgets"})).toBeVisible();
  expect(screen.getByRole("button", {name: "Check workspace availability"})).toBeVisible();
});
