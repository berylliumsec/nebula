import { expect, it, vi } from "vitest";

it("treats an unhandled abort rejection as a superseded operation", async () => {
  vi.resetModules();
  const logger = await import("./logger");
  logger.installGlobalDiagnosticHandlers();
  const event = new Event("unhandledrejection", {cancelable: true});
  Object.defineProperty(event, "reason", {value: new DOMException("Superseded", "AbortError")});

  window.dispatchEvent(event);

  expect(event.defaultPrevented).toBe(true);
});
