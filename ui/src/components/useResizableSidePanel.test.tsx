import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it } from "vitest";
import { useResizableSidePanel } from "./useResizableSidePanel";

function Harness({ enabled = true }: { enabled?: boolean }) {
  const size = useResizableSidePanel({
    defaultWidth: 560,
    enabled,
    label: "Resize test panel",
    maxWidth: 900,
    minPrimaryWidth: 320,
    minWidth: 360,
    storageKey: "nebula.test-panel.width",
  });
  return <main><aside ref={size.panelRef} style={size.panelStyle}>{size.resizeHandle}<span>Panel</span></aside></main>;
}

describe("useResizableSidePanel", () => {
  beforeEach(() => localStorage.clear());

  it("resizes from the keyboard, clamps to safe bounds, and persists the width", () => {
    render(<Harness />);
    const handle = screen.getByRole("separator", { name: "Resize test panel" });
    expect(handle).toHaveAttribute("aria-valuenow", "560");

    fireEvent.keyDown(handle, { key: "ArrowLeft" });
    expect(handle).toHaveAttribute("aria-valuenow", "584");
    expect(localStorage.getItem("nebula.test-panel.width")).toBe("584");

    fireEvent.keyDown(handle, { key: "End" });
    expect(handle).toHaveAttribute("aria-valuenow", "704");
    fireEvent.keyDown(handle, { key: "Home" });
    expect(handle).toHaveAttribute("aria-valuenow", "360");
  });

  it("restores a saved width and omits desktop sizing when disabled", () => {
    localStorage.setItem("nebula.test-panel.width", "640");
    const view = render(<Harness />);
    expect(screen.getByRole("separator")).toHaveAttribute("aria-valuenow", "640");
    view.rerender(<Harness enabled={false} />);
    expect(screen.queryByRole("separator")).not.toBeInTheDocument();
    expect((screen.getByText("Panel").parentElement as HTMLElement).style.width).toBe("");
  });
});
