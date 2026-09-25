import { expect, it, vi } from "vitest";

function reject(reason: unknown): Event {
  const event = new Event("unhandledrejection", {cancelable: true});
  Object.defineProperty(event, "reason", {value: reason});
  window.dispatchEvent(event);
  return event;
}

it("treats an unhandled abort rejection as a superseded operation", async () => {
  vi.resetModules();
  const logger = await import("./logger");
  logger.installGlobalDiagnosticHandlers();
  const event = reject(new DOMException("Superseded", "AbortError"));
  expect(event.defaultPrevented).toBe(true);
  // A detached chat viewer rejects with the same kind of cancellation.
  expect(logger.classifyUnhandledRejection(new DOMException("Viewer detached", "AbortError"))).toBe("cancelled");
});

it("does not report a failure Nebula already handled when a fetch wrapper drops its own copy", async () => {
  vi.resetModules();
  const logger = await import("./logger");
  const failure = new TypeError("Failed to fetch");
  // ApiClient and the diagnostics sink hand the failure to the logger where it happened.
  await logger.logDiagnostic({ level: "error", eventCode: "interface.api.transport_failed", message: "Core unreachable.", exception: failure });
  expect(logger.classifyUnhandledRejection(failure)).toBe("handled");
  logger.markRejectionHandled(undefined);
  expect(logger.classifyUnhandledRejection(new TypeError("Failed to fetch"))).toBe("failure");
});

it("records a rejection from a browser extension's own code below error level", async () => {
  vi.resetModules();
  const logger = await import("./logger");
  for (const scheme of ["chrome-extension", "moz-extension", "safari-web-extension"]) {
    const foreign = new TypeError("Failed to fetch");
    foreign.stack = `TypeError: Failed to fetch\n    at t.class.sayHiToJil.window.fetch (${scheme}://abcdef/inject.js:1:4242)\n    at hi (http://192.168.1.155:8000/assets/diagnostics.js:2:100)`;
    expect(logger.classifyUnhandledRejection(foreign)).toBe("extension");
  }
  const ours = new Error("Unexpected state");
  ours.stack = "Error: Unexpected state\n    at submit (http://192.168.1.155:8000/assets/SessionsPage.js:9:1200)";
  expect(logger.classifyUnhandledRejection(ours)).toBe("failure");
  expect(logger.classifyUnhandledRejection("plain string")).toBe("failure");
});

it("keeps genuine Nebula rejections at error level and sends the others at debug", async () => {
  vi.resetModules();
  const sent: { level: string; event_code: string }[] = [];
  const fetchMock = vi.fn(async (_url: string, init?: RequestInit) => {
    for (const record of JSON.parse(String(init?.body)).events) sent.push(record);
    return new Response(JSON.stringify({ accepted: 1, error_ids: [] }), { status: 202 });
  });
  vi.stubGlobal("fetch", fetchMock);
  try {
    const logger = await import("./logger");
    logger.configureBrowserDiagnostics("/api/v1");
    logger.setCoreDiagnosticsHealth({ diagnosticsDegraded: false, browserDiagnosticIngress: "enabled" });
    logger.setDiagnosticSettings({ schema: "nebula.diagnostics-settings/v1", global_level: "debug", feature_levels: {}, sensitive_detail_capture: false });
    logger.installGlobalDiagnosticHandlers();
    const foreign = new TypeError("Failed to fetch");
    foreign.stack = "TypeError: Failed to fetch\n    at wrapper (chrome-extension://abcdef/inject.js:1:10)";
    reject(foreign);
    reject(new Error("A genuine Nebula failure"));
    await vi.waitFor(() => expect(sent).toHaveLength(2));
    expect(sent.map(record => [record.event_code, record.level])).toEqual([
      ["interface.promise.extension_rejection", "debug"],
      ["interface.promise.unhandled_rejection", "error"],
    ]);
  } finally {
    vi.unstubAllGlobals();
  }
});
