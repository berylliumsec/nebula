import { render, screen } from "@testing-library/react";
import type { ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";
import { DiagnosticErrorBoundary, isStaleBundleImportError } from "./ErrorBoundary";

vi.mock("./logger", () => ({
  logDiagnostic: vi.fn().mockResolvedValue("err_render_test"),
}));

function ThrowRenderError({ error }: { error: Error }): ReactNode {
  throw error;
}

describe("DiagnosticErrorBoundary", () => {
  it("identifies browser errors caused by a replaced lazy bundle", () => {
    expect(isStaleBundleImportError(new TypeError(
      "Failed to fetch dynamically imported module: http://nebula.test/assets/SettingsPage-old.js",
    ))).toBe(true);
    expect(isStaleBundleImportError(new Error("Importing a module script failed."))).toBe(true);
    expect(isStaleBundleImportError(new Error("The settings component failed."))).toBe(false);
  });

  it("offers direct reload recovery without presenting a deploy transition as data loss", () => {
    vi.spyOn(console, "error").mockImplementation(() => undefined);
    render(
      <DiagnosticErrorBoundary>
        <ThrowRenderError error={new TypeError("Failed to fetch dynamically imported module: /assets/SettingsPage-old.js")} />
      </DiagnosticErrorBoundary>,
    );

    expect(screen.getByRole("heading", { name: "Nebula was updated" })).toBeVisible();
    expect(screen.getByText(/Saved projects, chats, and evidence are unaffected/)).toBeVisible();
    expect(screen.getByRole("button", { name: "Reload Nebula" })).toBeVisible();
    expect(screen.queryByText("The workspace could not be displayed")).not.toBeInTheDocument();
  });
});
