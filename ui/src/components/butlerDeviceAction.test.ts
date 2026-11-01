import { describe, expect, it, vi } from "vitest";
import type { ApiClient } from "../api/client";
import type { PairedDevice } from "../api/types";
import { applyButlerDeviceAction } from "./butlerDeviceAction";

const deviceId = "b96e898d-207f-4943-846e-c00f4415fc26";
const device: PairedDevice = {
  id: deviceId, name: "Fixture phone", createdAt: "2026-11-01T12:00:00Z",
  lastUsedAt: "2026-11-01T12:01:00Z", idleExpiresAt: "2026-12-01T12:00:00Z",
  absoluteExpiresAt: "2027-01-30T12:00:00Z", current: false,
};
const revoke = {kind: "device_revoke" as const, projectId: "project", deviceId,
  name: device.name, expectedCreatedAt: device.createdAt};

describe("Butler device handoff", () => {
  it("opens pairing without asking Core for a one-time offer", async () => {
    const api = {createDevicePairing: vi.fn(), listPairedDevices: vi.fn()} as unknown as ApiClient;
    const open = vi.fn();
    const message = await applyButlerDeviceAction(api, {kind: "device_pair", projectId: "project"},
      () => "project", vi.fn(), open);
    expect(open).toHaveBeenCalledOnce();
    expect(api.createDevicePairing).not.toHaveBeenCalled();
    expect(message).toContain("has not created an offer");
  });

  it("rejects another project and a changed device identity", async () => {
    const api = {listPairedDevices: vi.fn().mockResolvedValue([{...device, name: "Renamed"}]),
      revokePairedDevice: vi.fn()} as unknown as ApiClient;
    await expect(applyButlerDeviceAction(api, revoke, () => "other", vi.fn(), vi.fn()))
      .rejects.toThrow("original project");
    expect(api.listPairedDevices).not.toHaveBeenCalled();
    await expect(applyButlerDeviceAction(api, revoke, () => "project", vi.fn(), vi.fn()))
      .rejects.toThrow("changed");
    expect(api.revokePairedDevice).not.toHaveBeenCalled();
  });

  it("leaves a cancelled or newly current device paired", async () => {
    const api = {listPairedDevices: vi.fn().mockResolvedValueOnce([device])
      .mockResolvedValueOnce([device]).mockResolvedValueOnce([{...device, current: true}]),
      revokePairedDevice: vi.fn()} as unknown as ApiClient;
    expect(await applyButlerDeviceAction(api, revoke, () => "project", async () => false, vi.fn()))
      .toContain("not revoked");
    await expect(applyButlerDeviceAction(api, revoke, () => "project", async () => true, vi.fn()))
      .rejects.toThrow("current browser");
    expect(api.revokePairedDevice).not.toHaveBeenCalled();
  });

  it("opens the existing Unpair control for the current browser", async () => {
    const api = {listPairedDevices: vi.fn().mockResolvedValue([{...device, current: true}]),
      revokePairedDevice: vi.fn()} as unknown as ApiClient;
    const open = vi.fn();
    const message = await applyButlerDeviceAction(api, revoke, () => "project", vi.fn(), open);
    expect(open).toHaveBeenCalledOnce();
    expect(message).toContain("loses Core access immediately");
    expect(api.revokePairedDevice).not.toHaveBeenCalled();
  });

  it("confirms a noncurrent revocation and observes list absence", async () => {
    const api = {listPairedDevices: vi.fn().mockResolvedValueOnce([device])
      .mockResolvedValueOnce([device]).mockResolvedValueOnce([]),
      revokePairedDevice: vi.fn().mockResolvedValue(undefined)} as unknown as ApiClient;
    const message = await applyButlerDeviceAction(api, revoke, () => "project", async () => true, vi.fn());
    expect(api.revokePairedDevice).toHaveBeenCalledWith(deviceId);
    expect(message).toContain("confirmed that Core no longer lists");
  });
});
