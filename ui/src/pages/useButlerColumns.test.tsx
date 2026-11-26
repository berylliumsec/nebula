import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it } from "vitest";
import { MIDDLE_MIN, MIDDLE_WIDTH_KEY, RAIL_MAX, RAIL_MIN, RAIL_WIDTH_KEY, useButlerColumns } from "./useButlerColumns";

function Harness({ enabled = true }: { enabled?: boolean }) {
  const columns = useButlerColumns(enabled, "handle");
  return <div data-testid="body" style={columns.style}>
    <nav>Rail</nav>{columns.railHandle}<section>Thread</section>{columns.middleHandle}<section>Board</section>
  </div>;
}

describe("useButlerColumns", () => {
  beforeEach(() => localStorage.clear());

  it("keeps CSS defaults until the operator resizes a column", () => {
    render(<Harness />);
    const body = screen.getByTestId("body");
    expect(body.style.getPropertyValue("--butler-rail-track")).toBe("");
    expect(body.style.getPropertyValue("--butler-middle-track")).toBe("");
    expect(screen.getAllByRole("separator").map((item) => item.getAttribute("aria-label"))).toEqual(["Resize threads list", "Resize open thread"]);
  });

  it("resizes each column from the keyboard within bounds and persists the width", () => {
    render(<Harness />);
    const rail = screen.getByRole("separator", { name: "Resize threads list" });
    fireEvent.keyDown(rail, { key: "End" });
    expect(rail).toHaveAttribute("aria-valuenow", String(RAIL_MAX));
    fireEvent.keyDown(rail, { key: "ArrowLeft", shiftKey: true });
    expect(rail).toHaveAttribute("aria-valuenow", String(RAIL_MAX - 80));
    expect(localStorage.getItem(RAIL_WIDTH_KEY)).toBe(String(RAIL_MAX - 80));
    fireEvent.keyDown(rail, { key: "Home" });
    expect(rail).toHaveAttribute("aria-valuenow", String(RAIL_MIN));
    expect(screen.getByTestId("body").style.getPropertyValue("--butler-rail-track")).toBe(`clamp(${RAIL_MIN}px, ${RAIL_MIN}px, ${RAIL_MAX}px)`);

    const middle = screen.getByRole("separator", { name: "Resize open thread" });
    fireEvent.keyDown(middle, { key: "Home" });
    fireEvent.keyDown(middle, { key: "ArrowRight" });
    expect(middle).toHaveAttribute("aria-valuenow", String(MIDDLE_MIN + 24));
    expect(localStorage.getItem(MIDDLE_WIDTH_KEY)).toBe(String(MIDDLE_MIN + 24));
    expect(screen.getByTestId("body").style.getPropertyValue("--butler-middle-track")).toContain(`min(${MIDDLE_MIN + 24}px`);
  });

  it("restores saved widths, resets on double click, and has no dividers when compact", () => {
    localStorage.setItem(RAIL_WIDTH_KEY, "360");
    localStorage.setItem(MIDDLE_WIDTH_KEY, "700");
    const view = render(<Harness />);
    const body = screen.getByTestId("body");
    expect(body.style.getPropertyValue("--butler-rail-track")).toContain("360px");
    expect(body.style.getPropertyValue("--butler-middle-track")).toContain("700px");

    fireEvent.doubleClick(screen.getByRole("separator", { name: "Resize threads list" }));
    expect(localStorage.getItem(RAIL_WIDTH_KEY)).toBeNull();
    expect(body.style.getPropertyValue("--butler-rail-track")).toBe("");

    view.rerender(<Harness enabled={false} />);
    expect(screen.queryByRole("separator")).not.toBeInTheDocument();
    expect(body.style.getPropertyValue("--butler-middle-track")).toBe("");
  });
});
