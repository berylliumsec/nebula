import { beforeEach, describe, expect, it, vi } from "vitest";
import { ApiClient } from "./client";

const diagnostics = vi.hoisted(() => ({ logDiagnostic: vi.fn() }));
vi.mock("../diagnostics", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../diagnostics")>()),
  logDiagnostic: diagnostics.logDiagnostic,
}));

describe("ApiClient cancellation diagnostics", () => {
  beforeEach(() => diagnostics.logDiagnostic.mockClear());

  it("records an abort while reading a response body as a cancellation", async () => {
    const controller = new AbortController();
    const response = {
      ok: true,
      status: 200,
      json: vi.fn(async () => {
        controller.abort();
        throw new DOMException("The operation was aborted.", "AbortError");
      }),
    } as unknown as Response;
    const client = new ApiClient({
      baseUrl: "http://127.0.0.1:8765",
      fetch: vi.fn<typeof fetch>().mockResolvedValue(response),
    });

    await expect(client.health(controller.signal)).rejects.toMatchObject({ name: "AbortError" });
    expect(diagnostics.logDiagnostic.mock.calls.filter(([record]) => record.level === "error")).toEqual([]);
    expect(diagnostics.logDiagnostic).toHaveBeenCalledWith(expect.objectContaining({
      level: "debug",
      eventCode: "interface.api.request_cancelled",
      stage: "response-parse",
    }));
  });
});
