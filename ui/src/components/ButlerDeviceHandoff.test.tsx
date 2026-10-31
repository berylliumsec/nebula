import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { ApiClient } from "../api/client";
import type { PairedDevice } from "../api/types";
import { DialogProvider } from "./DialogSystem";
import { ButlerDeviceHandoff } from "./ButlerDeviceHandoff";

const projectId = "selected";
const deviceId = "9482d86e-2ddc-436b-90be-30b878f68537";
const createdAt = "2026-10-30T20:30:00.123456Z";
const device: PairedDevice = {
  id: deviceId,
  name: "Field phone",
  createdAt,
  lastUsedAt: createdAt,
  idleExpiresAt: "2026-11-29T20:30:00Z",
  absoluteExpiresAt: "2027-01-28T20:30:00Z",
  current: false,
};

function renderHandoff(action: {kind: "device_pair"; projectId: string; name: string}
    | {kind: "device_revoke"; projectId: string; deviceId: string; expectedName: string; expectedCreatedAt: string},
    api: Partial<ApiClient>, online = true) {
  const onOutcome = vi.fn();
  const onDismiss = vi.fn();
  const onSelfRevoked = vi.fn();
  render(<DialogProvider><ButlerDeviceHandoff
    api={{getToken: () => undefined, ...api} as ApiClient}
    online={online}
    action={action}
    currentProjectId={() => projectId}
    onOutcome={onOutcome}
    onDismiss={onDismiss}
    onSelfRevoked={onSelfRevoked}
  /></DialogProvider>);
  return {onOutcome, onDismiss, onSelfRevoked};
}

describe("ButlerDeviceHandoff", () => {
  it("keeps a host pairing secret on the requesting browser and reports only an offer", async () => {
    const user = userEvent.setup();
    const createDevicePairing = vi.fn().mockResolvedValue({secret: "single-use-secret", confirmationCode: "123456", expiresAt: "2027-01-01T00:00:00Z"});
    const {onOutcome, onDismiss} = renderHandoff(
      {kind: "device_pair", projectId, name: "Field phone"},
      {baseUrl: "http://127.0.0.1:8000/api/v1", getToken: vi.fn().mockReturnValue("host-token"), createDevicePairing},
    );
    await user.click(screen.getByRole("button", {name: "Create pairing offer"}));
    await waitFor(() => expect(createDevicePairing).toHaveBeenCalledWith("Field phone"));
    expect(screen.getByRole("status")).toHaveTextContent("Confirm code 123456");
    expect(screen.getByRole("status")).toHaveTextContent("single-use-secret");
    expect(onOutcome).toHaveBeenCalledOnce();
    expect(onOutcome.mock.calls[0][0]).toContain("Redemption on the companion device has not been verified");
    expect(onOutcome.mock.calls[0][0]).not.toContain("single-use-secret");
    expect(onOutcome.mock.calls[0][0]).not.toContain("123456");
    await user.click(screen.getByRole("button", {name: "Done"}));
    expect(onDismiss).toHaveBeenCalledOnce();
    expect(onOutcome).toHaveBeenCalledOnce();
  });

  it("refuses a pairing offer on a remote paired browser", async () => {
    const user = userEvent.setup();
    const createDevicePairing = vi.fn();
    const {onOutcome} = renderHandoff(
      {kind: "device_pair", projectId, name: "Field phone"},
      {baseUrl: "https://192.168.1.155:8000/api/v1", getToken: vi.fn().mockReturnValue("token"), createDevicePairing},
    );
    expect(screen.getByText(/only from the local Nebula host/)).toBeVisible();
    expect(screen.getByRole("button", {name: "Create pairing offer"})).toBeDisabled();
    await user.click(screen.getByRole("button", {name: "Cancel"}));
    expect(createDevicePairing).not.toHaveBeenCalled();
    expect(onOutcome).toHaveBeenCalledWith("No device pairing offer was created.");
  });

  it("rejects a changed device identity before any revoke call", async () => {
    const revokePairedDevice = vi.fn();
    renderHandoff(
      {kind: "device_revoke", projectId, deviceId, expectedName: "Field phone", expectedCreatedAt: createdAt},
      {listPairedDevices: vi.fn().mockResolvedValue([{...device, name: "Renamed"}]), revokePairedDevice},
    );
    expect(await screen.findByText(/device changed or is already absent/)).toBeVisible();
    expect(screen.getByRole("button", {name: "Revoke device"})).toBeDisabled();
    expect(revokePairedDevice).not.toHaveBeenCalled();
  });

  it("confirms exact revocation and reads Core until the device is absent", async () => {
    const user = userEvent.setup();
    const listPairedDevices = vi.fn().mockResolvedValueOnce([device]).mockResolvedValueOnce([device]).mockResolvedValueOnce([]);
    const revokePairedDevice = vi.fn().mockResolvedValue(undefined);
    const {onOutcome, onDismiss} = renderHandoff(
      {kind: "device_revoke", projectId, deviceId, expectedName: "Field phone", expectedCreatedAt: createdAt},
      {listPairedDevices, revokePairedDevice},
    );
    await user.click(await screen.findByRole("button", {name: "Revoke device"}));
    const dialog = screen.getByRole("dialog", {name: "Revoke Field phone?"});
    expect(revokePairedDevice).not.toHaveBeenCalled();
    await user.click(within(dialog).getByRole("button", {name: "Revoke device"}));
    await waitFor(() => expect(revokePairedDevice).toHaveBeenCalledWith(deviceId));
    await waitFor(() => expect(onOutcome).toHaveBeenCalledWith(expect.stringContaining("absent from Core's current paired-device list")));
    expect(onDismiss).toHaveBeenCalledOnce();
    expect(listPairedDevices).toHaveBeenCalledTimes(3);
  });

  it("reports an uncertain revoke instead of claiming cancellation was harmless", async () => {
    const user = userEvent.setup();
    const listPairedDevices = vi.fn().mockResolvedValue([device]);
    const revokePairedDevice = vi.fn().mockRejectedValue(new Error("connection lost"));
    const {onOutcome} = renderHandoff(
      {kind: "device_revoke", projectId, deviceId, expectedName: "Field phone", expectedCreatedAt: createdAt},
      {listPairedDevices, revokePairedDevice},
    );
    await user.click(await screen.findByRole("button", {name: "Revoke device"}));
    await user.click(within(screen.getByRole("dialog", {name: "Revoke Field phone?"})).getByRole("button", {name: "Revoke device"}));
    expect(await screen.findByText(/did not confirm whether revocation completed/)).toBeVisible();
    await user.click(screen.getByRole("button", {name: "Cancel"}));
    expect(onOutcome).toHaveBeenCalledWith(expect.stringContaining("Revocation result is uncertain"));
    expect(onOutcome).not.toHaveBeenCalledWith("No paired device was revoked.");
  });
});
