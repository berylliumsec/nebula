import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { ChromeProvider, type ChromeContextValue } from "../state/ChromeContext";
import { DialogProvider } from "./DialogSystem";
import { WorkbenchBrowser } from "./WorkbenchBrowser";

const chrome: ChromeContextValue = {
  activityOpen: false,
  paletteOpen: false,
  sidebarCollapsed: true,
  toolbarHost: null,
  openPalette: () => undefined,
  setActivityOpen: () => undefined,
  setPaletteOpen: () => undefined,
  setToolbarHost: () => undefined,
  toggleActivity: () => undefined,
  toggleSidebar: () => undefined,
};

describe("WorkbenchBrowser", () => {
  it("explains that native browsing is desktop-only in the web workspace", () => {
    render(<DialogProvider><ChromeProvider value={chrome}><WorkbenchBrowser active projectId="project-1" onOpenFiles={() => undefined} /></ChromeProvider></DialogProvider>);
    expect(screen.getByRole("strong")).toHaveTextContent("Browser is available in the Nebula desktop app");
    expect(screen.getByText(/Native child webviews are intentionally unavailable/)).toBeVisible();
  });
});
