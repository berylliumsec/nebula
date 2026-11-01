import {describe, expect, it, vi} from "vitest";
import type {ApiClient} from "../api/client";
import {applyButlerDiagnosticsExport} from "./butlerDiagnosticsAction";

const action = {kind: "diagnostics_export" as const, projectId: "project"};
const zip = new Blob([new Uint8Array([0x50, 0x4b, 0x03, 0x04, 1, 2, 3])], {type: "application/zip"});

describe("Butler diagnostics export", () => {
  it("requires the selected project and browser confirmation", async () => {
    const api = {exportDiagnostics: vi.fn()} as unknown as ApiClient;
    const download = vi.fn();
    await expect(applyButlerDiagnosticsExport(api, action, () => "other", vi.fn(), download))
      .rejects.toThrow("original project");
    expect(await applyButlerDiagnosticsExport(api, action, () => "project", async () => false, download))
      .toContain("No diagnostics ZIP");
    expect(api.exportDiagnostics).not.toHaveBeenCalled();
    expect(download).not.toHaveBeenCalled();
  });

  it("rejects a non-ZIP response without starting a download", async () => {
    const api = {exportDiagnostics: vi.fn().mockResolvedValue(new Blob(["not a zip"]))} as unknown as ApiClient;
    const download = vi.fn();
    await expect(applyButlerDiagnosticsExport(api, action, () => "project", async () => true, download))
      .rejects.toThrow("valid diagnostics ZIP");
    expect(download).not.toHaveBeenCalled();
  });

  it("starts a verified ZIP download only on the requesting browser", async () => {
    const api = {exportDiagnostics: vi.fn().mockResolvedValue(zip)} as unknown as ApiClient;
    const download = vi.fn();
    const result = await applyButlerDiagnosticsExport(api, action, () => "project", async () => true, download);
    expect(download).toHaveBeenCalledWith(zip);
    expect(result).toContain("Butler received no bundle bytes");
  });
});
