import { describe, expect, it } from "vitest";
import {
  diagnosticFailurePresentation,
  diagnosticRecordMatchesReference,
  diagnosticTechnicalDetails,
  humanizeDiagnosticValue,
} from "./presentation";
import type { DiagnosticRecord } from "./types";

const record: DiagnosticRecord = {
  schema: "nebula.diagnostic/v1",
  level: "ERROR",
  feature: "terminal-audit",
  event_code: "terminal.audit.capture_failed",
  message: "Terminal audit capture could not complete.",
  safe_failure_cause: "The audit spool could not be persisted.",
  stage: "persist_spool",
  retryable: false,
  error_id: "err_capture_123",
  request_id: "req_capture_123",
  metadata: { component: "audit_writer" },
};

describe("diagnostic presentation", () => {
  it("humanizes the failing component and exposes only a known destination", () => {
    expect(diagnosticFailurePresentation(record)).toMatchObject({
      featureLabel: "Terminal audit",
      operationLabel: "Terminal audit · Audit writer",
      cause: "The audit spool could not be persisted.",
      recovery: "Review the technical evidence and correlation identifiers in this incident.",
      destination: "/?view=terminal",
      actionLabel: "Open Terminal audit",
    });
    expect(humanizeDiagnosticValue("image-preparation_retry")).toBe("Image preparation retry");
  });

  it("uses the honest unclassified fallback without inventing a fix", () => {
    expect(diagnosticFailurePresentation({
      ...record,
      feature: "diagnostics",
      safe_failure_cause: undefined,
      retryable: undefined,
      metadata: undefined,
    })).toMatchObject({
      operationLabel: "Local diagnostics · Persist spool",
      cause: "Nebula recorded an internal failure but the available sanitized evidence does not identify a verified root cause.",
      recovery: "Review the technical evidence and correlation identifiers in this incident.",
    });
  });

  it("matches either supported correlation reference and copies only the sanitized record", () => {
    expect(diagnosticRecordMatchesReference(record, "err_capture_123")).toBe(true);
    expect(diagnosticRecordMatchesReference(record, "req_capture_123")).toBe(true);
    expect(diagnosticRecordMatchesReference(record, "op_capture_123")).toBe(false);
    expect(JSON.parse(diagnosticTechnicalDetails(record))).toEqual(record);
  });
});
