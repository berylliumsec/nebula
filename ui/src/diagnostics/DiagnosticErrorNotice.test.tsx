import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { DiagnosticErrorNotice } from "./DiagnosticErrorNotice";
import { rememberDiagnosticErrorPresentation } from "./logger";

describe("DiagnosticErrorNotice", () => {
  it("shows a structured safe reason, retryability, code, and correlated diagnostics link", () => {
    const error = Object.assign(new Error("The report renderer was unavailable."), {
      errorId: "err_report_123",
      requestId: "req_report_123",
      retryable: true,
      code: "reports.render.failed",
    });

    render(<DiagnosticErrorNotice error={error} />);

    expect(screen.getByRole("alert")).toHaveAttribute("data-error-reference", "err_report_123");
    expect(screen.getByText("The report renderer was unavailable.")).toBeVisible();
    expect(screen.getByText("This operation can be retried.")).toBeVisible();
    expect(screen.getByText("Reference: err_report_123 · reports.render.failed")).toBeVisible();
    expect(screen.getByRole("link", { name: "View diagnostics" })).toHaveAttribute(
      "href",
      "/settings?diagnostic=err_report_123#diagnostics-settings",
    );
  });

  it("supports legacy string errors without duplicating their reference or redacting text", () => {
    rememberDiagnosticErrorPresentation("err_provider_456", {
      retryable: false,
      code: "providers.request.failed",
    });
    render(
      <DiagnosticErrorNotice
        error="Provider failed with Bearer top-secret-token-value. Reference: err_provider_456."
        compact
      />,
    );

    const alert = screen.getByRole("alert");
    expect(alert.tagName).toBe("SPAN");
    expect(alert).toHaveTextContent("Provider failed with Bearer top-secret-token-value");
    expect(alert).not.toHaveTextContent("[REDACTED]");
    expect(screen.getByText("No verified retry procedure is available.")).toBeVisible();
    expect(screen.getByText("Reference: err_provider_456 · providers.request.failed")).toBeVisible();
  });

  it("fails safely when no verified recovery or reference is available", () => {
    render(<DiagnosticErrorNotice error={{}} fallback="A safe local failure occurred." />);

    expect(screen.getByText("A safe local failure occurred.")).toBeVisible();
    expect(screen.getByText("Review Diagnostics for the recorded cause and recovery guidance.")).toBeVisible();
    expect(screen.getByText("Reference: pending local diagnostic")).toBeVisible();
  });

  it("shows the correlated exact cause and impact inline", () => {
    const error = Object.assign(new Error("Harness transport failed."), {
      errorId: "err_harness_shared",
      reasonCode: "transport_closed",
      operatorDetail: "Codex app-server closed stdout before turn completion.",
      impact: "The harness turn did not complete.",
      retryable: true,
    });

    render(<DiagnosticErrorNotice error={error} />);

    expect(screen.getByText("Cause:").closest("small")).toHaveTextContent(error.operatorDetail);
    expect(screen.getByText("Impact:").closest("small")).toHaveTextContent(error.impact);
    expect(screen.getByText("Reference: err_harness_shared · transport closed")).toBeVisible();
  });
});
