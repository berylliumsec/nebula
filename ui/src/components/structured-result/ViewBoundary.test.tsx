import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ViewBoundary } from "./ViewBoundary";

const { logCaughtDiagnostic } = vi.hoisted(() => ({ logCaughtDiagnostic: vi.fn() }));
vi.mock("../../diagnostics", () => ({ logCaughtDiagnostic }));

function Explodes({ failing }: { failing: boolean }): React.ReactElement {
  if (failing) throw new Error("the table renderer gave up");
  return <p>rendered</p>;
}

describe("a renderer that fails inside the dashboard", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    // React logs the caught error; the boundary is what this test is about.
    vi.spyOn(console, "error").mockImplementation(() => {});
  });
  afterEach(() => vi.restoreAllMocks());

  it("replaces only that view, keeps the reason and offers the tree", async () => {
    const user = userEvent.setup();
    const fallback = vi.fn();
    render(<ViewBoundary view="table" onFallback={fallback}><Explodes failing /></ViewBoundary>);

    expect(screen.getByRole("alert")).toHaveTextContent("The table view could not render");
    expect(screen.getByText("the table renderer gave up")).toBeInTheDocument();
    expect(screen.getByText(/The result itself is unchanged/)).toBeInTheDocument();
    expect(logCaughtDiagnostic).toHaveBeenCalledWith(
      "interface.structured_result.view_failed",
      expect.stringContaining("table view"),
      expect.any(Error),
      "structured_result",
    );

    await user.click(screen.getByRole("button", { name: "Show the tree" }));
    expect(fallback).toHaveBeenCalledOnce();
  });

  it("tries again on request and renders when the renderer recovers", async () => {
    const user = userEvent.setup();
    const { rerender } = render(<ViewBoundary view="table"><Explodes failing /></ViewBoundary>);
    expect(screen.getByRole("alert")).toBeInTheDocument();

    rerender(<ViewBoundary view="table"><Explodes failing={false} /></ViewBoundary>);
    await user.click(screen.getByRole("button", { name: "Try again" }));
    expect(screen.getByText("rendered")).toBeInTheDocument();
  });

  it("gives a different view its own fresh attempt", () => {
    const { rerender } = render(<ViewBoundary view="table"><Explodes failing /></ViewBoundary>);
    expect(screen.getByRole("alert")).toBeInTheDocument();
    rerender(<ViewBoundary view="tree"><Explodes failing={false} /></ViewBoundary>);
    expect(screen.getByText("rendered")).toBeInTheDocument();
  });
});
