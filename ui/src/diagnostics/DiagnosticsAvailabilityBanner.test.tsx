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
});
