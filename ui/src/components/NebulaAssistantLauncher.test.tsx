import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { NebulaAssistantLauncherButton, NebulaAssistantLauncherContext } from "./NebulaAssistantLauncher";

describe("Butler launch mark", () => {
  it("uses the Quiet Lens mark in the desktop launcher while keeping its accessible action", () => {
    const toggle = vi.fn();
    render(<NebulaAssistantLauncherContext.Provider value={{ opened: false, visible: false, toggle }}>
      <NebulaAssistantLauncherButton />
    </NebulaAssistantLauncherContext.Provider>);
    const button = screen.getByRole("button", { name: "Open Butler" });
    expect(button.querySelector("img")).toHaveAttribute("src", "/nebula-logo.svg");
    expect(button.querySelector("img")).toHaveAttribute("alt", "");
    fireEvent.click(button);
    expect(toggle).toHaveBeenCalledOnce();
  });

  it("keeps the mobile label and uses the same mark", () => {
    render(<NebulaAssistantLauncherContext.Provider value={{ opened: true, visible: false, toggle: vi.fn() }}>
      <NebulaAssistantLauncherButton mobile />
    </NebulaAssistantLauncherContext.Provider>);
    const button = screen.getByRole("button", { name: "Show Butler" });
    expect(button).toHaveTextContent("Butler");
    expect(button.querySelector("img")).toHaveAttribute("src", "/nebula-logo.svg");
  });
});
