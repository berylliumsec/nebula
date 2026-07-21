import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { DiagnosticsAvailabilityBanner } from "./DiagnosticsPanel";

vi.mock("./logger", async (importOriginal) => ({
  ...await importOriginal<typeof import("./logger")>(),
  isDiagnosticsAvailable: () => false,
}));

describe("DiagnosticsAvailabilityBanner", () => {
  it("links to diagnostics and can be dismissed", async () => {
    const user = userEvent.setup();
    render(<DiagnosticsAvailabilityBanner />);

    expect(screen.getByRole("link", { name: "Diagnostics" })).toHaveAttribute(
      "href",
      "/settings#diagnostics-settings",
    );

    await user.click(screen.getByRole("button", { name: "Dismiss diagnostics notice" }));

    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  it("stays dismissed across health refreshes and returns for a new occurrence", async () => {
    const user = userEvent.setup();
    render(<DiagnosticsAvailabilityBanner />);
    await user.click(screen.getByRole("button", { name: "Dismiss diagnostics notice" }));

    window.dispatchEvent(new CustomEvent("nebula-diagnostics-health", {
      detail: { available: false, reason: "The same failure." },
    }));
    expect(screen.queryByRole("status")).not.toBeInTheDocument();

    window.dispatchEvent(new CustomEvent("nebula-diagnostics-health", {
      detail: { available: false, reason: "The same failure.", occurrence: true },
    }));
    expect(await screen.findByRole("status")).toBeVisible();
  });
});
