import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { ChromeProvider, type ChromeContextValue } from "../state/ChromeContext";
import { settingCatalog } from "../settingsCatalog";
import { CompactSettingsList, groupSettings } from "./CompactSettingsList";

function chrome(openSetting = vi.fn()): ChromeContextValue {
  return {
    activityOpen: false, paletteOpen: false, settingLensOpen: false, sidebarCollapsed: true, toolbarHost: null,
    openPalette: vi.fn(), openSetting, setActivityOpen: vi.fn(), setPaletteOpen: vi.fn(), setToolbarHost: vi.fn(),
    toggleActivity: vi.fn(), toggleSidebar: vi.fn(),
  };
}

describe("CompactSettingsList", () => {
  it("orders every catalog entry into setup-first groups", () => {
    const groups = groupSettings(settingCatalog);
    expect(groups[0].category).toBe("Setup");
    expect(groups.map((group) => group.category).slice(1, 3)).toEqual(["Models", "Automation"]);
    expect(groups.flatMap((group) => group.entries)).toHaveLength(settingCatalog.length);
  });

  it("filters by keyword and opens the focused setting", () => {
    const openSetting = vi.fn();
    render(<ChromeProvider value={chrome(openSetting)}><CompactSettingsList /></ChromeProvider>);
    fireEvent.change(screen.getByRole("searchbox", { name: "Search settings" }), { target: { value: "ssh" } });
    const group = screen.getByRole("region", { name: "Environments" });
    fireEvent.click(within(group).getByRole("button", { name: /Environments/ }));
    expect(openSetting).toHaveBeenCalledWith(expect.objectContaining({ id: "settings.ssh-environments" }), expect.any(HTMLElement));
    expect(screen.queryByRole("region", { name: "Models" })).not.toBeInTheDocument();
  });

  it("offers the native connection sheet only inside the iPhone shell", () => {
    const userAgent = vi.spyOn(navigator, "userAgent", "get");
    userAgent.mockReturnValue("Mozilla/5.0 (iPhone) AppleWebKit/605.1.15 NebulaShell/1.0");
    try {
      render(<ChromeProvider value={chrome()}><CompactSettingsList /></ChromeProvider>);
      expect(screen.getByRole("link", { name: /App connection/ })).toHaveAttribute("href", "nebula://settings");
    } finally {
      userAgent.mockRestore();
    }
  });
});
