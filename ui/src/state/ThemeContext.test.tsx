import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ThemeProvider, useTheme } from "./ThemeContext";

function Probe() {
  const { preference } = useTheme();
  return <output data-testid="preference">{preference}</output>;
}

describe("ThemeProvider", () => {
  afterEach(() => vi.restoreAllMocks());

  it("falls back to the default appearance when browser storage is blocked", () => {
    const blocked = () => { throw new DOMException("Storage is blocked.", "SecurityError"); };
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(blocked);
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(blocked);

    render(<ThemeProvider><Probe /></ThemeProvider>);

    expect(screen.getByTestId("preference")).toHaveTextContent("zero-dark");
    expect(document.documentElement.dataset.theme).toBe("zero-dark");
  });
});
