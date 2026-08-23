import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { ApiClient } from "../api/client";
import type { PairedDevice } from "../api/types";
import { DevicePairingSettings } from "./DevicePairingSettings";
import { DialogProvider } from "./DialogSystem";

const currentDevice: PairedDevice = {
  id: "device-current",
  name: "My phone",
  createdAt: "2026-08-23T14:00:00Z",
  lastUsedAt: "2026-08-23T15:05:51Z",
  idleExpiresAt: "2026-09-22T14:00:00Z",
  absoluteExpiresAt: "2026-11-21T14:00:00Z",
  current: true,
};

const otherDevice: PairedDevice = {
  ...currentDevice,
  id: "device-other",
  name: "Tablet",
  current: false,
};

function renderSettings(api: Partial<ApiClient>, onCurrentDeviceRevoked = vi.fn()) {
  return {
    onCurrentDeviceRevoked,
    ...render(
      <DialogProvider>
        <DevicePairingSettings
          api={api as ApiClient}
          disabled={false}
          onCurrentDeviceRevoked={onCurrentDeviceRevoked}
        />
      </DialogProvider>,
    ),
  };
}

describe("DevicePairingSettings", () => {
  it("replaces the dead LAN pairing control with host-only guidance", async () => {
    renderSettings({
      baseUrl: "http://192.168.1.155:8000/api/v1",
      getToken: vi.fn().mockReturnValue("host-token"),
      listPairedDevices: vi.fn().mockResolvedValue([]),
    });

    expect(await screen.findByText("Pair new devices from the Nebula host")).toBeVisible();
    expect(screen.getByText(/nebula-core ui/)).toBeVisible();
    expect(screen.queryByRole("button", { name: "Pair device" })).not.toBeInTheDocument();
  });

  it("creates pairing links from a compact loopback-only dialog", async () => {
    const user = userEvent.setup();
    const createDevicePairing = vi.fn().mockResolvedValue({
      secret: "single-use-secret",
      confirmationCode: "123456",
      expiresAt: "2026-08-23T16:00:00Z",
    });
    renderSettings({
      baseUrl: "http://127.0.0.1:8000/api/v1",
      getToken: vi.fn().mockReturnValue("host-token"),
      listPairedDevices: vi.fn().mockResolvedValue([]),
      createDevicePairing,
    });

    await user.click(await screen.findByRole("button", { name: "Pair device" }));
    const dialog = screen.getByRole("dialog", { name: "Pair a device" });
    expect(dialog).toBeVisible();
    await user.clear(screen.getByLabelText("Device name"));
    await user.type(screen.getByLabelText("Device name"), "Field phone");
    await user.click(screen.getByRole("button", { name: "Create pairing link" }));

    await waitFor(() => expect(createDevicePairing).toHaveBeenCalledWith("Field phone"));
    expect(screen.getByRole("status")).toHaveTextContent("Confirm code 123456");
    expect(screen.getByRole("button", { name: "Done" })).toBeVisible();
  });

  it("confirms current-browser revocation and does not refresh with the revoked cookie", async () => {
    const user = userEvent.setup();
    const listPairedDevices = vi.fn().mockResolvedValue([currentDevice]);
    const revokePairedDevice = vi.fn().mockResolvedValue(undefined);
    const { onCurrentDeviceRevoked } = renderSettings({ listPairedDevices, revokePairedDevice });

    await user.click(await screen.findByRole("button", { name: "Unpair My phone" }));
    expect(screen.getByRole("heading", { name: "Unpair this browser?" })).toBeVisible();
    expect(revokePairedDevice).not.toHaveBeenCalled();

    await user.click(screen.getByRole("button", { name: "Unpair browser" }));
    await waitFor(() => expect(revokePairedDevice).toHaveBeenCalledWith("device-current"));
    expect(onCurrentDeviceRevoked).toHaveBeenCalledOnce();
    expect(listPairedDevices).toHaveBeenCalledOnce();
    expect(screen.queryByText("valid bearer token required")).not.toBeInTheDocument();
  });

  it("refreshes the authoritative list after revoking a different browser", async () => {
    const user = userEvent.setup();
    const listPairedDevices = vi.fn()
      .mockResolvedValueOnce([currentDevice, otherDevice])
      .mockResolvedValueOnce([currentDevice]);
    const revokePairedDevice = vi.fn().mockResolvedValue(undefined);
    const { onCurrentDeviceRevoked } = renderSettings({ listPairedDevices, revokePairedDevice });

    await user.click(await screen.findByRole("button", { name: "Revoke Tablet" }));
    await user.click(screen.getByRole("button", { name: "Revoke device" }));

    await waitFor(() => expect(listPairedDevices).toHaveBeenCalledTimes(2));
    expect(revokePairedDevice).toHaveBeenCalledWith("device-other");
    expect(onCurrentDeviceRevoked).not.toHaveBeenCalled();
    expect(screen.queryByText("Tablet")).not.toBeInTheDocument();
    expect(screen.getByText(/My phone/)).toBeVisible();
  });
});
