import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { DiagnosticsAvailabilityBanner } from "./DiagnosticsPanel";

vi.mock("./logger", async (importOriginal) => ({
  ...await importOriginal<typeof import("./logger")>(),
  isDiagnosticsAvailable: () => false,
}));

describe("DiagnosticsAvailabilityBanner", () => {
  beforeEach(() => window.localStorage.clear());

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

    act(() => window.dispatchEvent(new CustomEvent("nebula-diagnostics-health", {
      detail: { available: false, reason: "The same failure." },
    })));
    expect(screen.queryByRole("status")).not.toBeInTheDocument();

    act(() => window.dispatchEvent(new CustomEvent("nebula-diagnostics-health", {
      detail: { available: false, reason: "The same failure.", occurrence: true },
    })));
    expect(await screen.findByRole("status")).toBeVisible();
  });

  it("persists dismissal for the current Core binding across remounts", async () => {
    const user = userEvent.setup();
    const first = render(<DiagnosticsAvailabilityBanner />);
    await user.click(screen.getByRole("button", { name: "Dismiss diagnostics notice" }));
    first.unmount();

    render(<DiagnosticsAvailabilityBanner />);
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  it("presents browser capture as a neutral capability limitation", async () => {
    render(<DiagnosticsAvailabilityBanner />);
    act(() => window.dispatchEvent(new CustomEvent("nebula-diagnostics-health", {
      detail: { available: false, reason: "Browser event capture is disabled for this binding." },
    })));

    const notice = await screen.findByRole("status");
    expect(notice).toHaveClass("tone-informational");
    expect(notice).toHaveTextContent("Core and the rest of the workspace remain usable");
  });
});
