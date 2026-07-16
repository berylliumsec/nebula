import { beforeEach, describe, expect, it, vi } from "vitest";
import contract from "../../../tests/v3/fixtures/diagnostics_contract.json";
import { diagnosticFeatures } from "./types";

async function freshLogger() {
  vi.resetModules();
  return import("./logger");
}

describe("interface diagnostics", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
    delete (window as Window & { __TAURI_INTERNALS__?: unknown }).__TAURI_INTERNALS__;
  });

  it("matches the shared cross-language schema, settings, features, and sanitizer contract", async () => {
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(JSON.stringify({ accepted: 1, error_ids: ["err_contract"] }), { status: 202 }),
    );
    vi.stubGlobal("fetch", fetchMock);
    const logger = await freshLogger();
    logger.configureBrowserDiagnostics("/api/v1");

    await logger.logDiagnostic({
      level: "error",
      eventCode: "interface.contract.failed",
      message: "A contract fault was injected.",
      metadata: contract.metadata_input,
    });

    const body = JSON.parse(String(fetchMock.mock.calls[0][1]?.body));
    expect(contract.record_schema).toBe("nebula.diagnostic/v1");
    expect(contract.settings).toEqual({
      schema: "nebula.diagnostics-settings/v1",
      global_level: "error",
      feature_levels: {},
      sensitive_detail_capture: false,
    });
    expect(contract.features).toEqual(diagnosticFeatures);
    expect(body.events[0].metadata).toEqual(contract.metadata_expected);
  });

  it("filters below Error by default and sends a bounded sanitized record", async () => {
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(JSON.stringify({ accepted: 1, error_ids: ["err_server"] }), {
        status: 202,
        headers: { "content-type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);
    const logger = await freshLogger();
    logger.configureBrowserDiagnostics("http://127.0.0.1:8765/api/v1", "test-token");

    await logger.logDiagnostic({
      level: "info",
      eventCode: "interface.test.started",
      message: "A test started.",
    });
    const errorId = await logger.logDiagnostic({
      level: "error",
      eventCode: "interface.test.failed",
      message: "Authentication failed for Bearer top-secret-token-value.",
      exception: new Error("canary exception payload"),
      metadata: {
        component: "test",
        authorization: "canary-authorization",
        command: "canary-command",
        count: 2,
      },
    });

    expect(fetchMock).toHaveBeenCalledOnce();
    expect(errorId).toBe("err_server");
    const [, init] = fetchMock.mock.calls[0];
    expect(new Headers(init?.headers).get("Authorization")).toBe("Bearer test-token");
    const encoded = String(init?.body);
    expect(encoded).not.toContain("top-secret-token-value");
    expect(encoded).not.toContain("canary exception payload");
    expect(encoded).not.toContain("canary-authorization");
    expect(encoded).not.toContain("canary-command");
    expect(JSON.parse(encoded)).toEqual({
      events: [expect.objectContaining({
        schema: "nebula.diagnostic/v1",
        level: "error",
        feature: "interface",
        event_code: "interface.test.failed",
        error_id: expect.stringMatching(/^err_[a-f0-9]{32}$/),
        exception_type: "Error",
        stack_frames: expect.arrayContaining([
          expect.objectContaining({ module: "interface", line: expect.any(Number) }),
        ]),
        metadata: { component: "test", count: 2 },
      })],
    });
  });

  it("applies the interface feature override without a restart", async () => {
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(JSON.stringify({ accepted: 1, error_ids: [] }), { status: 202 }),
    );
    vi.stubGlobal("fetch", fetchMock);
    const logger = await freshLogger();
    logger.configureBrowserDiagnostics("/api/v1");
    logger.setDiagnosticSettings({
      schema: "nebula.diagnostics-settings/v1",
      global_level: "error",
      feature_levels: { interface: "debug" },
      sensitive_detail_capture: false,
    });

    await logger.logDiagnostic({
      level: "debug",
      eventCode: "interface.test.decision",
      message: "A bounded interface decision was made.",
    });

    expect(fetchMock).toHaveBeenCalledOnce();
  });

  it("retains errors and reports degraded health while the sink is unavailable", async () => {
    vi.stubGlobal("fetch", vi.fn<typeof fetch>().mockRejectedValue(new Error("offline")));
    const logger = await freshLogger();
    logger.configureBrowserDiagnostics("/api/v1");

    const result = await logger.logDiagnostic({
      level: "critical",
      eventCode: "interface.bootstrap.failed",
      message: "The interface could not start.",
    });

    expect(result).toBeUndefined();
    expect(logger.isDiagnosticsAvailable()).toBe(false);
    expect(logger.diagnosticsFallbackErrors()).toEqual([
      expect.objectContaining({
        level: "critical",
        event_code: "interface.bootstrap.failed",
        error_id: expect.stringMatching(/^err_[a-f0-9]{32}$/),
      }),
    ]);
  });

  it("preserves a Core error reference when the UI handles an API failure", async () => {
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(JSON.stringify({ accepted: 1, error_ids: [] }), { status: 202 }),
    );
    vi.stubGlobal("fetch", fetchMock);
    const logger = await freshLogger();
    logger.configureBrowserDiagnostics("/api/v1");
    logger.setDiagnosticSettings({
      schema: "nebula.diagnostics-settings/v1",
      global_level: "debug",
      feature_levels: {},
      sensitive_detail_capture: false,
    });
    const failure = Object.assign(new Error("safe API detail"), {
      status: 503,
      errorId: "err_core_123",
      requestId: "req_core_123",
    });

    logger.logCaughtDiagnostic(
      "interface.api.handled_failure",
      "An API operation failed.",
      failure,
      "api-response",
    );
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledOnce());

    const body = JSON.parse(String(fetchMock.mock.calls[0][1]?.body));
    expect(body.events[0]).toMatchObject({
      level: "debug",
      error_id: "err_core_123",
      request_id: "req_core_123",
      metadata: { kind: "core-error-handled", http_status: 503 },
    });
  });

  it("classifies an uncorrelated expected API denial as Warning", async () => {
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(JSON.stringify({ accepted: 1, error_ids: [] }), { status: 202 }),
    );
    vi.stubGlobal("fetch", fetchMock);
    const logger = await freshLogger();
    logger.configureBrowserDiagnostics("/api/v1");
    logger.setDiagnosticSettings({
      schema: "nebula.diagnostics-settings/v1",
      global_level: "warning",
      feature_levels: {},
      sensitive_detail_capture: false,
    });

    logger.logCaughtDiagnostic(
      "interface.api.expected_denial",
      "An API operation was rejected safely.",
      Object.assign(new Error("safe denial"), { status: 409, requestId: "req_denied_123" }),
      "api-response",
    );
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledOnce());

    const body = JSON.parse(String(fetchMock.mock.calls[0][1]?.body));
    expect(body.events[0]).toMatchObject({
      level: "warning",
      request_id: "req_denied_123",
      retryable: true,
      safe_failure_cause: "The request was rejected safely.",
      metadata: { kind: "interface-error", http_status: 409 },
    });
    expect(body.events[0]).not.toHaveProperty("error_id");
  });

  it("assigns a frontend failure reference before its catch handler updates the UI", async () => {
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(JSON.stringify({ accepted: 1, error_ids: [] }), { status: 202 }),
    );
    vi.stubGlobal("fetch", fetchMock);
    const logger = await freshLogger();
    logger.configureBrowserDiagnostics("/api/v1");
    const failure = new Error("A local renderer failed.");

    logger.logCaughtDiagnostic(
      "interface.renderer.failed",
      "The interface renderer failed.",
      failure,
      "render",
    );

    expect(failure).toMatchObject({
      errorId: expect.stringMatching(/^err_[a-f0-9]{32}$/),
      message: expect.stringMatching(/Reference: err_[a-f0-9]{32}\.$/),
    });
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledOnce());
    const body = JSON.parse(String(fetchMock.mock.calls[0][1]?.body));
    expect(body.events[0].error_id).toBe((failure as Error & { errorId: string }).errorId);
    expect(body.events[0].safe_failure_cause).toBe("The interface operation raised Error.");
  });
});
