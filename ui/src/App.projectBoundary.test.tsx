import { render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "./App";
import { DialogProvider } from "./components/DialogSystem";
import { ThemeProvider } from "./state/ThemeContext";
import { WorkspaceProvider } from "./state/WorkspaceContext";

const entity = { created_at: "2026-07-12T10:00:00Z", updated_at: "2026-07-12T11:00:00Z", revision: 1, description: "", client_name: "Client", tags: [], metadata: {} };

describe("canonical project links", () => {
  beforeEach(() => {
    localStorage.clear();
    sessionStorage.clear();
  });
  afterEach(() => vi.unstubAllGlobals());

  it("offers the remembered project when a link names an archived one", async () => {
    window.history.replaceState({}, "", "/projects/archived/findings");
    localStorage.setItem("nebula.engagement", "current");
    vi.stubGlobal("fetch", vi.fn<typeof fetch>().mockImplementation(async (input) => {
      const url = new URL(String(input));
      if (url.pathname.endsWith("/health")) return new Response(JSON.stringify({ status: "ok", version: "3.0.0", mode: "local", runner: "unavailable", human_pty: "unavailable" }), { status: 200 });
      if (url.pathname.endsWith("/engagements")) return new Response(JSON.stringify([
        { ...entity, id: "current", name: "Current project", status: "active" },
        { ...entity, id: "archived", name: "Archived project", status: "archived" },
      ]), { status: 200 });
      return new Response(JSON.stringify([]), { status: 200 });
    }));

    render(
      <MemoryRouter initialEntries={["/projects/archived/findings"]}>
        <ThemeProvider><WorkspaceProvider><DialogProvider><App /></DialogProvider></WorkspaceProvider></ThemeProvider>
      </MemoryRouter>,
    );

    const heading = await screen.findByRole("heading", { name: "Project unavailable" });
    const alert = heading.closest("[role='alert']") as HTMLElement;
    expect(within(alert).getByRole("link", { name: "Open Current project" })).toHaveAttribute("href", "/projects/current");
    expect(localStorage.getItem("nebula.engagement")).toBe("current");
  });
});
