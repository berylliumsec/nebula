import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { BrowserPageSurface } from "./BrowserPageSurface";

describe("BrowserPageSurface", () => {
  it("is a non-interactive image with no page input or keyboard trap", () => {
    render(<BrowserPageSurface frame="data:image/jpeg;base64,AA==" connected />);
    const image = screen.getByRole("img", { name: /Assistant browser page — read-only/ });
    expect(image).not.toHaveAttribute("tabindex");
    expect(image).toHaveAttribute("draggable", "false");
    fireEvent.pointerDown(image, { clientX: 10, clientY: 10 });
    fireEvent.keyDown(image, { key: "a" });
    fireEvent.wheel(image, { deltaY: 100 });
    expect(image).not.toHaveFocus();
    expect(screen.getByRole("region", {name: "Read-only browser viewport"})).toHaveAttribute("tabindex", "0");
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
  });
  it("keeps the last frame visibly busy while disconnected", () => {
    const { container, rerender } = render(<BrowserPageSurface frame="" connected={false} />);
    expect(screen.getByText(/will appear when/)).toBeVisible();
    rerender(<BrowserPageSurface frame="data:image/jpeg;base64,AA==" connected={false} />);
    expect(screen.getByRole("img")).toBeVisible();
    expect(container.firstChild).toHaveAttribute("aria-busy", "true");
  });
});
