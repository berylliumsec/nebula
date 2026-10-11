import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { useWorkspace } from "../state/WorkspaceContext";
import { ThemeProvider } from "../state/ThemeContext";
import { DialogProvider } from "./DialogSystem";
import { NebulaControlAssistant } from "./NebulaControlAssistant";

vi.mock("../state/WorkspaceContext", () => ({
  useWorkspace: vi.fn(),
}));

describe("NebulaControlAssistant", () => {
  beforeEach(() => {
    localStorage.clear();
    vi.mocked(useWorkspace).mockReturnValue({ api: undefined, engagement: undefined, providers: [], harnesses: [] } as unknown as ReturnType<typeof useWorkspace>);
  });

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

  it("opens a named setting from Enter through the settings lens callback", async () => {
    const user = userEvent.setup();
    const onOpenSetting = vi.fn();
    render(<DialogProvider><ThemeProvider><NebulaControlAssistant onOpenSetting={onOpenSetting} /></ThemeProvider></DialogProvider>);
    await user.click(screen.getByRole("button", { name: "Open Nebula assistant" }));
    await user.type(screen.getByRole("textbox", { name: "Ask Nebula to change a setting" }), "open MCP servers{enter}");
    expect(onOpenSetting).toHaveBeenCalledWith(expect.objectContaining({ id: "settings.mcp" }));
    expect(screen.getByRole("button", { name: "Show Nebula assistant" })).toBeInTheDocument();
  });

  it("sends once on Enter, keeps Shift+Enter as a newline, and ignores composition", async () => {
    const user = userEvent.setup();
    const streamChat = vi.fn().mockResolvedValue({
      sessionId: "control", backend: "harness", harnessProfileId: "local", model: "model",
      message: { role: "assistant", content: "Done" },
    });
    vi.mocked(useWorkspace).mockReturnValue({
      api: { getControlAssistantSession: vi.fn().mockResolvedValue(null), streamChat },
      engagement: { id: "project", name: "Project" }, providers: [],
      harnesses: [{ id: "local", name: "Local", enabled: true, healthy: true, localOnly: true,
        permitsSensitiveData: true, models: ["model"], modelOptions: [], defaultModel: "model" }],
    } as unknown as ReturnType<typeof useWorkspace>);
    render(<DialogProvider><ThemeProvider><NebulaControlAssistant onOpenSetting={vi.fn()} /></ThemeProvider></DialogProvider>);
    await user.click(screen.getByRole("button", { name: "Open Nebula assistant" }));
    const composer = screen.getByRole("textbox", { name: "Ask Nebula to change a setting" });
    await waitFor(() => expect(composer).toBeEnabled());
    await user.type(composer, "First{shift>}{enter}{/shift}second");
    expect(composer).toHaveValue("First\nsecond");
    fireEvent.keyDown(composer, { key: "Enter", isComposing: true });
    expect(streamChat).not.toHaveBeenCalled();
    await user.type(composer, "{enter}");
    await waitFor(() => expect(streamChat).toHaveBeenCalledTimes(1));
    expect(streamChat.mock.calls[0][0]).toMatchObject({ messages: [{ content: "First\nsecond" }] });
  });
});
