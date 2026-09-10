import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { DiagnosticsAvailabilityBanner } from "./DiagnosticsPanel";
import { setCoreDiagnosticsHealth } from "./logger";

vi.mock("./logger", async (importOriginal) => ({
  ...await importOriginal<typeof import("./logger")>(),
  isDiagnosticsAvailable: () => false,
}));

describe("DiagnosticsAvailabilityBanner", () => {
  beforeEach(() => window.localStorage.clear());

  it("retains an open or dismissed notice across unchanged Core health samples", async () => {
    const user = userEvent.setup();
    render(<DiagnosticsAvailabilityBanner />);
    const unavailable = {diagnosticsDegraded: false, browserDiagnosticIngress: "disabled"};
    act(() => setCoreDiagnosticsHealth(unavailable));
    await user.click(screen.getByRole("button", {name: "Diagnostics notice details"}));
    act(() => setCoreDiagnosticsHealth(unavailable));
    expect(screen.getByRole("dialog", {name: "Browser event capture is unavailable."})).toBeVisible();
    await user.keyboard("{Escape}");
    await user.click(screen.getByRole("button", {name: "Dismiss diagnostics notice"}));
    act(() => setCoreDiagnosticsHealth(unavailable));
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
    act(() => setCoreDiagnosticsHealth({diagnosticsDegraded: false, browserDiagnosticIngress: "enabled"}));
    act(() => setCoreDiagnosticsHealth(unavailable));
    expect(screen.getByRole("status")).toBeVisible();
  });

  it("keeps verbose detail behind an in-place, focus-restoring disclosure", async () => {
    const user = userEvent.setup();
    render(<DiagnosticsAvailabilityBanner />);
    const reason = "Diagnostic storage is unavailable. ".repeat(20);
    act(() => window.dispatchEvent(new CustomEvent("nebula-diagnostics-health", {
      detail: {available: false, reason},
    })));
    const details = screen.getByRole("button", {name: "Diagnostics notice details"});
    expect(screen.getByRole("status")).not.toHaveTextContent(reason);
    await user.click(details);
    expect(screen.getByRole("dialog", {name: "Local diagnostics are unavailable."})).toHaveTextContent(reason);
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(details).toHaveFocus();
  });

  it("links to diagnostics and can be dismissed", async () => {
    const user = userEvent.setup();
    render(<DiagnosticsAvailabilityBanner />);

    await user.click(screen.getByRole("button", {name: "Diagnostics notice details"}));
    expect(screen.getByRole("link", { name: "Diagnostics" })).toHaveAttribute(
      "href",
      "/settings#diagnostics-settings",
    );
    await user.keyboard("{Escape}");

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

  it("closes resolved details without reopening them on a later failure", async () => {
    const user = userEvent.setup();
    render(<DiagnosticsAvailabilityBanner />);
    await user.click(screen.getByRole("button", {name: "Diagnostics notice details"}));
    act(() => window.dispatchEvent(new CustomEvent("nebula-diagnostics-health", {detail: {available: true}})));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    act(() => window.dispatchEvent(new CustomEvent("nebula-diagnostics-health", {detail: {available: false, reason: "New failure"}})));
    expect(screen.getByRole("status")).toBeVisible();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
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
    const user = userEvent.setup();
    render(<DiagnosticsAvailabilityBanner />);
    act(() => window.dispatchEvent(new CustomEvent("nebula-diagnostics-health", {
      detail: { available: false, reason: "Browser event capture is disabled for this binding." },
    })));

    const notice = await screen.findByRole("status");
    expect(notice).toHaveClass("tone-informational");
    await user.click(screen.getByRole("button", {name: "Diagnostics notice details"}));
    expect(screen.getByRole("dialog")).toHaveTextContent("Core and the rest of the workspace remain usable");
  });
});
