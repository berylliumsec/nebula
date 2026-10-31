import { useEffect, useRef, useState, type FormEvent } from "react";
import { QRCodeSVG } from "qrcode.react";
import type { ApiClient } from "../api/client";
import type { PairedDevice } from "../api/types";
import { DiagnosticErrorNotice, logCaughtDiagnostic } from "../diagnostics";
import { useConfirmation } from "./DialogSystem";
import { companionOrigin, companionPairingUrl } from "../devicePairingLink";
import type { SettingClientAction } from "./nebulaAssistantActions";
import styles from "./NebulaControlAssistant.module.css";

type DeviceAction = Extract<SettingClientAction, {kind: "device_pair" | "device_revoke"}>;

function isHostBearerBrowser(api: ApiClient | undefined): boolean {
  if (!api?.getToken()) return false;
  try {
    return ["127.0.0.1", "localhost", "::1"].includes(new URL(api.baseUrl).hostname);
  } catch {
    // diagnostic-expected: a missing or malformed Core URL cannot authorize pairing.
    return false;
  }
}

function exactDevice(devices: PairedDevice[], action: Extract<DeviceAction, {kind: "device_revoke"}>): PairedDevice | undefined {
  return devices.find((device) => device.id === action.deviceId
    && device.name === action.expectedName
    && device.createdAt === action.expectedCreatedAt);
}

export function ButlerDeviceHandoff({api, online, action, currentProjectId, onOutcome, onDismiss, onSelfRevoked}: {
  api?: ApiClient;
  online: boolean;
  action: DeviceAction;
  currentProjectId: () => string | undefined;
  onOutcome: (message: string) => void;
  onDismiss: () => void;
  onSelfRevoked: () => void;
}) {
  const confirm = useConfirmation();
  const [name, setName] = useState(action.kind === "device_pair" ? action.name : "");
  const [companionAddress, setCompanionAddress] = useState("");
  const [allowInsecureLan, setAllowInsecureLan] = useState(false);
  const [target, setTarget] = useState<PairedDevice>();
  const [offer, setOffer] = useState<{secret: string; confirmationCode: string; expiresAt: string; origin: string}>();
  const [loading, setLoading] = useState(action.kind === "device_revoke");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [recorded, setRecorded] = useState(false);
  const [uncertain, setUncertain] = useState(false);
  const currentProjectIdRef = useRef(currentProjectId);
  currentProjectIdRef.current = currentProjectId;
  const hostBrowser = isHostBearerBrowser(api);
  const projectCurrent = online && currentProjectId() === action.projectId;
  const reachableOrigin = companionOrigin(companionAddress, allowInsecureLan);

  useEffect(() => {
    if (action.kind !== "device_revoke" || !api || !online || currentProjectIdRef.current() !== action.projectId) return;
    let current = true;
    setLoading(true);
    void api.listPairedDevices().then((devices) => {
      if (!current) return;
      const found = exactDevice(devices, action);
      setTarget(found);
      if (!found) setError("The paired device changed or is already absent. Ask Butler to read the current device list.");
    }).catch((caught: unknown) => {
      if (!current) return;
      void logCaughtDiagnostic("interface.butler_device.list_failed", "Butler could not read the current paired devices.", caught, "butler-device-handoff");
      setError("Core could not load paired devices. Reconnect and ask Butler to read the list again.");
    }).finally(() => { if (current) setLoading(false); });
    return () => { current = false; };
  }, [action, api, online]);

  const dismiss = () => {
    if (busy) return;
    if (!recorded) onOutcome(uncertain
      ? action.kind === "device_pair"
        ? "Pairing offer result is uncertain. Let any unseen offer expire and inspect paired devices before trying again."
        : "Revocation result is uncertain. Inspect Core's current paired-device list before trying again."
      : action.kind === "device_pair" ? "No device pairing offer was created." : "No paired device was revoked.");
    onDismiss();
  };

  const createOffer = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!api || !projectCurrent || !hostBrowser || !name.trim() || !reachableOrigin || offer) {
      setError("Use local bearer access and enter this Core's reachable companion address before creating an offer.");
      return;
    }
    setBusy(true);
    setError("");
    setUncertain(false);
    try {
      const created = await api.createDevicePairing(name.trim());
      if (!created.secret || !/^\d{6}$/.test(created.confirmationCode)
          || !Number.isFinite(Date.parse(created.expiresAt))) {
        throw new Error("Core returned an incomplete pairing offer.");
      }
      if (currentProjectId() !== action.projectId) {
        throw new Error("The selected project changed after Core created the offer. Close this handoff; the offer expires in five minutes.");
      }
      setOffer({...created, origin: reachableOrigin});
      onOutcome(`Core created a five-minute pairing offer for ${name.trim()} on this host browser. Redemption on the companion device has not been verified. The one-time code stays on this browser.`);
      setRecorded(true);
    } catch (caught) {
      setUncertain(true);
      void logCaughtDiagnostic("interface.butler_device.pair_failed", "Butler could not confirm the host-only device pairing offer.", caught, "butler-device-handoff");
      setError("Core did not confirm the pairing offer. It may expire in five minutes; inspect paired devices before requesting another offer.");
    } finally {
      setBusy(false);
    }
  };

  const revoke = async () => {
    if (action.kind !== "device_revoke" || !api || !projectCurrent || !target || busy) return;
    const approved = await confirm({
      title: target.current ? "Unpair this browser?" : `Revoke ${target.name}?`,
      message: target.current
        ? "This browser will lose access immediately. Pair it again from the Nebula host to continue."
        : "This device will lose access immediately and must be paired again to return.",
      confirmLabel: target.current ? "Unpair browser" : "Revoke device",
      tone: "danger",
    });
    if (!approved) return;
    setBusy(true);
    setError("");
    setUncertain(false);
    let attempted = false;
    try {
      const before = await api.listPairedDevices();
      const exact = exactDevice(before, action);
      if (!exact || currentProjectId() !== action.projectId) {
        throw new Error("The selected project or device identity changed before revocation.");
      }
      attempted = true;
      try {
        await api.revokePairedDevice(action.deviceId);
      } catch (caught) {
        if (exact.current) throw caught;
        const afterFailure = await api.listPairedDevices();
        if (afterFailure.some((device) => device.id === action.deviceId)) throw caught;
      }
      if (!exact.current) {
        const observed = await api.listPairedDevices();
        if (observed.some((device) => device.id === action.deviceId)) {
          throw new Error("Core still lists the device as paired.");
        }
      }
      onOutcome(exact.current
        ? `Core revoked this browser's pairing for ${exact.name}; access is ending.`
        : `${exact.name} is absent from Core's current paired-device list after revocation.`);
      setRecorded(true);
      onDismiss();
      if (exact.current) onSelfRevoked();
    } catch (caught) {
      if (attempted) setUncertain(true);
      void logCaughtDiagnostic("interface.butler_device.revoke_failed", "Butler could not confirm paired-device revocation.", caught, "butler-device-handoff");
      setError(attempted
        ? "Core did not confirm whether revocation completed. Inspect the current paired-device list before trying again."
        : "The device or selected project changed. Ask Butler to read the current device list before retrying.");
    } finally {
      setBusy(false);
    }
  };

  const pairingUrl = offer
    ? companionPairingUrl(offer.origin, offer.secret, offer.confirmationCode)
    : "";

  return <section className={`${styles.libraryHandoff} ${styles.evidenceHandoff}`} aria-label={action.kind === "device_pair" ? "Pair device with Butler" : "Revoke paired device with Butler"}>
    {action.kind === "device_pair" ? <>
      <div><strong>Pair a device</strong><small>Create the one-time link on the Nebula host. Butler receives no secret or code.</small></div>
      {!hostBrowser && <p role="note">Pairing offers can be created only from the local Nebula host with bearer access. Open <code>nebula-core ui</code> on that host.</p>}
      {offer ? <div role="status" className={styles.devicePairingOffer}>
        <QRCodeSVG value={pairingUrl} size={168} bgColor="#ffffff" fgColor="#05070b" level="M" marginSize={2} title="Nebula device pairing QR code" />
        <code>{pairingUrl}</code><small>Confirm code {offer.confirmationCode} · expires {new Date(offer.expiresAt).toLocaleTimeString()}. Companion redemption is not yet verified.</small>
      </div> : <form className={styles.evidenceHandoffForm} onSubmit={(event) => void createOffer(event)}>
        <label>Device name<input value={name} maxLength={200} required disabled={busy} onChange={(event) => setName(event.target.value)} /></label>
        <label>Companion address<input type="url" inputMode="url" autoComplete="url" placeholder="https://nebula.example:8000" value={companionAddress} disabled={busy} onChange={(event) => setCompanionAddress(event.target.value)} /></label>
        <small>Enter this Core's HTTPS address as reached from the companion device. Check the host and port; localhost will not work on a phone.</small>
        <label><input type="checkbox" checked={allowInsecureLan} disabled={busy} onChange={(event) => setAllowInsecureLan(event.target.checked)} /> I started Core with <code>--allow-insecure-lan</code> on a trusted test network</label>
        {companionAddress && !reachableOrigin && <p role="alert">Enter a reachable HTTPS origin without a path, query, or fragment. HTTP requires the explicit insecure LAN option.</p>}
        <div className={styles.libraryHandoffActions}><button className="button primary" type="submit" disabled={busy || !projectCurrent || !hostBrowser || !name.trim() || !reachableOrigin}>Create pairing offer</button><button className="button quiet" type="button" disabled={busy} onClick={dismiss}>Cancel</button></div>
      </form>}
      {offer && <div className={styles.libraryHandoffActions}><button className="button quiet" type="button" onClick={dismiss}>Done</button></div>}
    </> : <>
      <div><strong>Revoke paired device</strong><small>Butler identified {action.expectedName}; Core will verify this exact device again before revocation.</small></div>
      {loading && <p role="status">Checking paired devices in Core…</p>}
      {target && <p>{target.name}{target.current ? " · this browser" : ""} · paired {new Date(target.createdAt).toLocaleString()}</p>}
      <div className={styles.libraryHandoffActions}><button className="button danger" type="button" disabled={busy || loading || !projectCurrent || !target} onClick={() => void revoke()}>{busy ? "Revoking…" : target?.current ? "Unpair this browser" : "Revoke device"}</button><button className="button quiet" type="button" disabled={busy} onClick={dismiss}>Cancel</button></div>
    </>}
    {error && <DiagnosticErrorNotice error={error} fallback="Device handoff failed." compact />}
  </section>;
}
