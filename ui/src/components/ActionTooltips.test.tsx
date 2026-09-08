import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ActionTooltips } from "./ActionTooltips";

afterEach(() => vi.useRealTimers());

describe("ActionTooltips", () => {
  it("explains an icon on hover, avoids duplicate native titles, and dismisses on Escape", () => {
    vi.useFakeTimers();
    render(<><button aria-label="Attach files" title="Attach files"><svg /></button><ActionTooltips /></>);
    const button = screen.getByRole("button");
    fireEvent.pointerOver(button, { pointerType: "mouse" });
    act(() => vi.advanceTimersByTime(200));
    expect(screen.getByRole("tooltip")).toHaveTextContent("Attach files");
    expect(button).toHaveAttribute("aria-describedby", screen.getByRole("tooltip").id);
    expect(button).not.toHaveAttribute("title");
    fireEvent.keyDown(button, { key: "Escape" });
    expect(screen.queryByRole("tooltip")).not.toBeInTheDocument();
    expect(button).toHaveAttribute("title", "Attach files");
    expect(button).not.toHaveAttribute("aria-describedby");
  });
  it("supports keyboard focus and restores existing descriptions", () => {
    render(<><p id="help">Saved conversation</p><button aria-label="Results" aria-describedby="help"><svg /></button><ActionTooltips /></>);
    const button = screen.getByRole("button");
    fireEvent.focusIn(button);
    expect(screen.getByRole("tooltip")).toHaveTextContent("Results");
    fireEvent.focusOut(button);
    expect(button).toHaveAttribute("aria-describedby", "help");
    expect(screen.queryByRole("tooltip")).not.toBeInTheDocument();
  });
  it("covers disabled controls added by lazy screens and does not intercept input", () => {
    vi.useFakeTimers();
    const view = render(<ActionTooltips />);
    view.rerender(<><button disabled aria-label="New conversation"><svg /></button><ActionTooltips /></>);
    fireEvent.pointerOver(screen.getByRole("button"), { pointerType: "mouse" });
    act(() => vi.advanceTimersByTime(200));
    expect(screen.getByRole("tooltip")).toHaveTextContent("New conversation");
    fireEvent.pointerOut(screen.getByRole("button"));
    expect(screen.queryByRole("tooltip")).not.toBeInTheDocument();
  });
});
