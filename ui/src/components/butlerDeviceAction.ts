import type { ApiClient } from "../api/client";
import type { PairedDevice } from "../api/types";
import { logCaughtDiagnostic } from "../diagnostics";
import type { SettingClientAction } from "./nebulaAssistantActions";

type DeviceAction = Extract<SettingClientAction, {kind: "device_pair" | "device_revoke"}>;

/** Pairing offers and revocation confirmation stay on the requesting browser. */
export async function applyButlerDeviceAction(
  api: ApiClient,
  action: DeviceAction,
  selectedProject: () => string | undefined,
  confirm: (request: {title: string; message: string; confirmLabel: string; tone: "danger"}) => Promise<boolean>,
  openDevices: () => void,
): Promise<string> {
  if (selectedProject() !== action.projectId) {
    throw new Error("Select the original project before changing paired devices.");
  }
  if (action.kind === "device_pair") {
    openDevices();
    return "Opened Paired devices. Create the one-time pairing link on the Nebula host browser; Butler has not created an offer or received its secret.";
  }
  const readDevice = async (): Promise<PairedDevice> => {
    const device = (await api.listPairedDevices()).find(item => item.id === action.deviceId);
    if (!device) throw new Error("The device is no longer paired. Ask Butler to list current devices.");
    if (device.name !== action.name || Date.parse(device.createdAt) !== Date.parse(action.expectedCreatedAt)) {
      throw new Error("The paired device changed. Ask Butler to read its current identity.");
    }
    return device;
  };
  const device = await readDevice();
  if (device.current) {
    openDevices();
    return "Opened Paired devices for this browser. Use its Unpair control: this device loses Core access immediately, so Butler cannot confirm the result afterward.";
  }
  const approved = await confirm({title: `Revoke ${device.name}?`,
    message: "That browser will lose access immediately and will need to be paired again.",
    confirmLabel: "Revoke device", tone: "danger"});
  if (!approved) return `${device.name} was not revoked.`;
  if (selectedProject() !== action.projectId) throw new Error("The selected project changed. Ask Butler to list devices again.");
  const current = await readDevice();
  if (current.current) throw new Error("This is now the current browser. Open Paired devices to unpair it directly.");
  await api.revokePairedDevice(current.id);
  try {
    if ((await api.listPairedDevices()).some(item => item.id === current.id)) {
      return `Core accepted revocation of ${current.name}, but list absence could not be confirmed. Inspect Paired devices before retrying.`;
    }
  } catch (caught) {
    logCaughtDiagnostic("interface.device_pairing.revoke_readback_failed", "The paired-device revocation could not be confirmed.", caught, "device-pairing");
    return `Core accepted revocation of ${current.name}, but readback failed. Inspect Paired devices before retrying.`;
  }
  return `Revoked ${current.name} and confirmed that Core no longer lists the device.`;
}
