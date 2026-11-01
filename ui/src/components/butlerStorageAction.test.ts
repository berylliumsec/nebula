import { describe, expect, it, vi } from "vitest";
import type { ApiClient } from "../api/client";
import type { StorageSource } from "../api/types";
import { applyButlerStorageAction } from "./butlerStorageAction";

const sourceId = "b96e898d-207f-4943-846e-c00f4415fc26";
const source: StorageSource = {
  id: sourceId, name: "Fixture NAS", engagement_id: "project", enabled: true,
  backend: "provider", profile_id: "local", model: "model", revision: 1,
  reporting_interval_seconds: 900, callback_url: null, callback_credential_ref: null,
  last_report_at: null,
};
const rotate = {kind: "storage_rotate" as const, projectId: "project", sourceId, expectedRevision: 1, name: source.name};
const revoke = {kind: "storage_revoke" as const, projectId: "project", sourceId, expectedRevision: 1, name: source.name};
const dashboard = (item: StorageSource) => ({sources: [item]});

describe("Butler storage handoff", () => {
  it("opens enrollment on this device without creating a source", async () => {
    const api = {storageDashboard: vi.fn(), createStorageSource: vi.fn()} as unknown as ApiClient;
    const open = vi.fn();
    const result = await applyButlerStorageAction(api, {kind: "storage_enroll", projectId: "project"},
      () => "project", vi.fn(), open);
    expect(result.message).toContain("No source has been added yet");
    expect(open).toHaveBeenCalledOnce();
    expect(api.createStorageSource).not.toHaveBeenCalled();
    expect(result.issued).toBeUndefined();
  });

  it("rejects a switched project or stale source before changing the token", async () => {
    const api = {storageDashboard: vi.fn().mockResolvedValue(dashboard({...source, revision: 2})),
      rotateStorageSource: vi.fn()} as unknown as ApiClient;
    const confirm = vi.fn().mockResolvedValue(true);
    await expect(applyButlerStorageAction(api, rotate, () => "other", confirm, vi.fn()))
      .rejects.toThrow("original project");
    expect(api.storageDashboard).not.toHaveBeenCalled();
    await expect(applyButlerStorageAction(api, rotate, () => "project", confirm, vi.fn()))
      .rejects.toThrow("changed");
    expect(confirm).not.toHaveBeenCalled();
    expect(api.rotateStorageSource).not.toHaveBeenCalled();
  });

  it("keeps rotation cancelled and blocks a source change during confirmation", async () => {
    const api = {storageDashboard: vi.fn().mockResolvedValueOnce(dashboard(source))
      .mockResolvedValueOnce(dashboard(source)).mockResolvedValueOnce(dashboard({...source, revision: 2})),
      rotateStorageSource: vi.fn()} as unknown as ApiClient;
    const cancelled = await applyButlerStorageAction(api, rotate, () => "project", async () => false, vi.fn());
    expect(cancelled.message).toContain("not rotated");
    await expect(applyButlerStorageAction(api, rotate, () => "project", async () => true, vi.fn()))
      .rejects.toThrow("during confirmation");
    expect(api.rotateStorageSource).not.toHaveBeenCalled();
  });

  it("keeps a rotated one-time token in browser output and confirms Core readback", async () => {
    const changed = {...source, revision: 2};
    const api = {baseUrl: "/api/v1", storageDashboard: vi.fn().mockResolvedValueOnce(dashboard(source))
      .mockResolvedValueOnce(dashboard(source)).mockResolvedValueOnce(dashboard(changed)),
      rotateStorageSource: vi.fn().mockResolvedValue({source: changed, token: "one-time-secret"})} as unknown as ApiClient;
    const result = await applyButlerStorageAction(api, rotate, () => "project", async () => true, vi.fn());
    expect(api.rotateStorageSource).toHaveBeenCalledWith(source);
    expect(result.message).toContain("revision 2");
    expect(result.message).not.toContain("one-time-secret");
    expect(result.issued).toMatchObject({projectId: "project", token: "one-time-secret"});
    expect(api.storageDashboard).toHaveBeenCalledTimes(3);
  });

  it("reports an uncertain readback without replaying rotation and confirms revocation separately", async () => {
    const changed = {...source, revision: 2};
    const rotationApi = {baseUrl: "/api/v1", storageDashboard: vi.fn().mockResolvedValueOnce(dashboard(source))
      .mockResolvedValueOnce(dashboard(source)).mockRejectedValueOnce(new Error("offline")),
      rotateStorageSource: vi.fn().mockResolvedValue({source: changed, token: "one-time-secret"})} as unknown as ApiClient;
    const uncertain = await applyButlerStorageAction(rotationApi, rotate, () => "project", async () => true, vi.fn());
    expect(uncertain.message).toContain("readback failed");
    expect(uncertain.issued?.token).toBe("one-time-secret");
    expect(rotationApi.rotateStorageSource).toHaveBeenCalledTimes(1);

    const disabled = {...source, enabled: false, revision: 2};
    const revokeApi = {storageDashboard: vi.fn().mockResolvedValueOnce(dashboard(source))
      .mockResolvedValueOnce(dashboard(source)).mockResolvedValueOnce(dashboard(disabled)),
      revokeStorageSource: vi.fn().mockResolvedValue(disabled)} as unknown as ApiClient;
    const revoked = await applyButlerStorageAction(revokeApi, revoke, () => "project", async () => true, vi.fn());
    expect(revoked.message).toContain("confirmed that Core disabled");
    expect(revoked.issued).toBeUndefined();
    expect(revokeApi.revokeStorageSource).toHaveBeenCalledWith(source);
  });
});
