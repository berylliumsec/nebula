import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { ApplicationModelPage, buildCondition } from "./ApplicationModelPage";

const request = vi.hoisted(() => vi.fn());
const engagement = { id: "project", name: "Fixture" };
const api = { request };
vi.mock("../state/WorkspaceContext", () => ({ useWorkspace: () => ({ api, engagement }) }));
vi.mock("../components/PageHeader", () => ({ PageHeader: ({ title }: { title: string }) => <h1>{title}</h1> }));

describe("Application Model inspector", () => {
  it("rejects inexact integers and preserves Boolean types", () => {
    expect(() => buildCondition("count", "eq", "integer", "9007199254740993")).toThrow();
    expect(() => buildCondition("count", "eq", "integer", "")).toThrow();
    expect(buildCondition("ready", "eq", "boolean", "false").args[1].value).toBe(false);
  });
  it("explains disabled capability without presenting executable controls", async () => {
    request.mockImplementation(async (url: string) => url.endsWith("/status") ? { enabled: false, solver_available: true, pending_count: 0 } : url.endsWith("browser-workspace") ? { sessions: [] } : []);
    render(<MemoryRouter><ApplicationModelPage /></MemoryRouter>);
    expect(await screen.findByText("Application model is disabled")).toBeVisible();
    expect(screen.queryByRole("button", { name: "Check consistency" })).toBeNull();
  });
  it("offers collection creation from an enumerated browser session", async () => {
    request.mockImplementation(async (url: string) => url.endsWith("/status") ? { enabled: true, solver_available: true, pending_count: 0 } : url.endsWith("browser-workspace") ? { sessions: [{ id: "browser", name: "Reader" }] } : []);
    render(<MemoryRouter><ApplicationModelPage /></MemoryRouter>);
    expect(await screen.findByRole("button", { name: "Create collection" })).toBeEnabled();
    expect(screen.getByLabelText("Browser session")).toHaveValue("browser");
    expect(screen.getByLabelText("Include recorded history")).toBeChecked();
  });
});
