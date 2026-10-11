import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ThemeProvider, useTheme } from "./ThemeContext";

function Probe() {
  const { preference } = useTheme();
  return <output data-testid="preference">{preference}</output>;
}

describe("ThemeProvider", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    localStorage.clear();
  });

  it("falls back to the default appearance when browser storage is blocked", () => {
    const blocked = () => { throw new DOMException("Storage is blocked.", "SecurityError"); };
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(blocked);
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(blocked);

    render(<ThemeProvider><Probe /></ThemeProvider>);

    expect(screen.getByTestId("preference")).toHaveTextContent("dark");
    expect(document.documentElement.dataset.theme).toBe("dark");

  });

  it("keeps an existing Zero Dark choice on the original palette during upgrade", () => {
    localStorage.setItem("nebula.theme", "zero-dark");

    render(<ThemeProvider><Probe /></ThemeProvider>);

    expect(screen.getByTestId("preference")).toHaveTextContent("zero-classic");
    expect(document.documentElement.dataset.theme).toBe("zero-classic");
    expect(localStorage.getItem("nebula.theme")).toBe("zero-classic");
    expect(localStorage.getItem("nebula.theme.paletteVersion")).toBe("2");
  });

  it("restores a newly selected Zero Dark without repeating the migration", () => {
    localStorage.setItem("nebula.theme", "zero-dark");
    localStorage.setItem("nebula.theme.paletteVersion", "2");

    render(<ThemeProvider><Probe /></ThemeProvider>);

    expect(screen.getByTestId("preference")).toHaveTextContent("zero-dark");
    expect(document.documentElement.dataset.theme).toBe("zero-dark");
  });

  it("restores the Quiet Lens device preference", () => {
    localStorage.setItem("nebula.theme", "quiet-lens");

    render(<ThemeProvider><Probe /></ThemeProvider>);

    expect(screen.getByTestId("preference")).toHaveTextContent("quiet-lens");
    expect(document.documentElement.dataset.theme).toBe("quiet-lens");
  });
});
