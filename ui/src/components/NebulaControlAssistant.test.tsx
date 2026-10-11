import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ThemeProvider } from "../state/ThemeContext";
import { DialogProvider } from "./DialogSystem";
import { NebulaControlAssistant } from "./NebulaControlAssistant";

vi.mock("../state/WorkspaceContext", () => ({
  useWorkspace: () => ({ api: undefined, engagement: undefined, providers: [], harnesses: [] }),
}));

describe("NebulaControlAssistant", () => {
  beforeEach(() => localStorage.clear());

  it("opens from the global icon, applies a device theme, and keeps the setting catalog reachable", async () => {
    const user = userEvent.setup();
    const onOpenSetting = vi.fn();
    render(<DialogProvider><ThemeProvider><NebulaControlAssistant onOpenSetting={onOpenSetting} /></ThemeProvider></DialogProvider>);
    await user.click(screen.getByRole("button", { name: "Open Nebula assistant" }));
    await user.click(screen.getByText("Settings I can help with"));
    await user.click(screen.getByRole("button", { name: "Light" }));
    expect(document.documentElement.dataset.theme).toBe("light");
    expect(localStorage.getItem("nebula.theme")).toBe("light");
    await user.type(screen.getByRole("textbox", { name: "Find a setting" }), "provider");
    await user.click(screen.getByRole("button", { name: /Model providers.*Direct actions available/ }));
    expect(onOpenSetting).toHaveBeenCalledWith(expect.objectContaining({ id: "settings.providers" }));
    expect(screen.getByRole("button", { name: "Show Nebula assistant" })).toBeInTheDocument();
  });

  it("hides, restores, and closes without discarding the draft", async () => {
    const user = userEvent.setup();
    render(<DialogProvider><ThemeProvider><NebulaControlAssistant onOpenSetting={vi.fn()} /></ThemeProvider></DialogProvider>);
    await user.click(screen.getByRole("button", { name: "Open Nebula assistant" }));
    await user.type(screen.getByRole("textbox", { name: "Ask Nebula to change a setting" }), "Help me choose a theme");
    await user.click(screen.getByRole("button", { name: "Hide Nebula assistant" }));
    await user.click(screen.getByRole("button", { name: "Show Nebula assistant" }));
    expect(screen.getByRole("textbox", { name: "Ask Nebula to change a setting" })).toHaveValue("Help me choose a theme");
    await user.click(screen.getByRole("button", { name: "Close Nebula assistant" }));
    await user.click(screen.getByRole("button", { name: "Open Nebula assistant" }));
    expect(screen.getByRole("textbox", { name: "Ask Nebula to change a setting" })).toHaveValue("Help me choose a theme");
  });
});
