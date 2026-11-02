import {describe, expect, it, vi} from "vitest";
import {ApiError, type ApiClient} from "../api/client";
import type {StructuredResultRecord} from "../api/types";
import {applyButlerResultDelete} from "./butlerResultAction";

const result = {id: "result", projectId: "selected", title: "Saved result", revision: 2} as StructuredResultRecord;
const action = {kind: "result_delete" as const, projectId: "selected", resultId: result.id, title: result.title, expectedRevision: 2};
const missing = new ApiError("Absent", 404);

describe("Butler saved-result deletion", () => {
  it("refuses a foreign record before asking or deleting", async () => {
    const api = {getStructuredResult: vi.fn().mockResolvedValue({...result, projectId: "foreign"}), deleteStructuredResultAtRevision: vi.fn()} as unknown as ApiClient;
    const confirm = vi.fn();
    await expect(applyButlerResultDelete(api, action, () => "selected", confirm)).rejects.toThrow("result changed");
    expect(confirm).not.toHaveBeenCalled();
    expect(api.deleteStructuredResultAtRevision).not.toHaveBeenCalled();
  });
  it("cancels without dispatching a write", async () => {
    const api = {getStructuredResult: vi.fn().mockResolvedValue(result), deleteStructuredResultAtRevision: vi.fn()} as unknown as ApiClient;
    expect((await applyButlerResultDelete(api, action, () => "selected", async () => false)).deleted).toBe(false);
    expect(api.deleteStructuredResultAtRevision).not.toHaveBeenCalled();
  });
  it("rechecks a concurrent revision change after confirmation", async () => {
    const api = {getStructuredResult: vi.fn().mockResolvedValueOnce(result).mockResolvedValue({...result, revision: 3}), deleteStructuredResultAtRevision: vi.fn()} as unknown as ApiClient;
    await expect(applyButlerResultDelete(api, action, () => "selected", async () => true)).rejects.toThrow("result changed");
    expect(api.deleteStructuredResultAtRevision).not.toHaveBeenCalled();
  });
  it("refuses a project switch while confirmation is open", async () => {
    const api = {getStructuredResult: vi.fn().mockResolvedValue(result), deleteStructuredResultAtRevision: vi.fn()} as unknown as ApiClient;
    let selected = "selected";
    await expect(applyButlerResultDelete(api, action, () => selected, async () => {selected = "other"; return true;})).rejects.toThrow("original project");
    expect(api.deleteStructuredResultAtRevision).not.toHaveBeenCalled();
  });
  it("deletes with the exact revision and requires Core absence", async () => {
    const api = {getStructuredResult: vi.fn().mockResolvedValueOnce(result).mockResolvedValueOnce(result).mockRejectedValue(missing), deleteStructuredResultAtRevision: vi.fn()} as unknown as ApiClient;
    expect((await applyButlerResultDelete(api, action, () => "selected", async () => true)).deleted).toBe(true);
    expect(api.deleteStructuredResultAtRevision).toHaveBeenCalledExactlyOnceWith("selected", "result", 2);
  });
  it("reconciles a lost write response without replaying deletion", async () => {
    const api = {getStructuredResult: vi.fn().mockResolvedValueOnce(result).mockResolvedValueOnce(result).mockRejectedValue(missing), deleteStructuredResultAtRevision: vi.fn().mockRejectedValue(new Error("lost"))} as unknown as ApiClient;
    expect((await applyButlerResultDelete(api, action, () => "selected", async () => true)).deleted).toBe(true);
    expect(api.deleteStructuredResultAtRevision).toHaveBeenCalledTimes(1);
  });
  it("keeps failed readback uncertain instead of reporting deletion", async () => {
    const api = {getStructuredResult: vi.fn().mockResolvedValueOnce(result).mockResolvedValueOnce(result).mockRejectedValue(new ApiError("Unavailable", 503)), deleteStructuredResultAtRevision: vi.fn()} as unknown as ApiClient;
    const observed = await applyButlerResultDelete(api, action, () => "selected", async () => true);
    expect(observed.deleted).toBe(false);
    expect(observed.message).toContain("did not confirm");
  });
});
