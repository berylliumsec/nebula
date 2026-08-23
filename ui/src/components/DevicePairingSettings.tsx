import { useCallback, useEffect, useState } from "react";
import { Link2, LoaderCircle, Smartphone, Trash2, X } from "lucide-react";
import { QRCodeSVG } from "qrcode.react";
import type { ApiClient } from "../api/client";
import type { PairedDevice } from "../api/types";
import { DiagnosticErrorNotice } from "../diagnostics";
import { ModalSurface, useConfirmation } from "./DialogSystem";

interface DevicePairingSettingsProps {
  api?: ApiClient;
  disabled: boolean;
  onCurrentDeviceRevoked?: () => void;
}

export function DevicePairingSettings({ api, disabled, onCurrentDeviceRevoked }: DevicePairingSettingsProps) {
  const confirm = useConfirmation();
  const [devices, setDevices] = useState<PairedDevice[]>([]);
  const [name, setName] = useState("My phone");
  const [pairingOpen, setPairingOpen] = useState(false);
  const [pairing, setPairing] = useState<{ secret: string; confirmationCode: string; expiresAt: string }>();
  const [busy, setBusy] = useState(false);
  const [revokingId, setRevokingId] = useState<string>();
  const [error, setError] = useState<string>();
  const [pairingError, setPairingError] = useState<string>();

  const apiHostname = (() => {
    try {
      return api ? new URL(api.baseUrl).hostname : "";
    } catch {
      return "";
    }
  })();
  const canCreatePairing = Boolean(
    !disabled
    && api?.getToken?.()
    && ["127.0.0.1", "localhost", "::1"].includes(apiHostname),
  );

  const refresh = useCallback(async () => {
    if (!api) return;
    try {
      setDevices(await api.listPairedDevices());
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Could not list paired devices.");
    }
  }, [api]);
  useEffect(() => { void refresh(); }, [refresh]);

  const createPairing = async () => {
    if (!api || !name.trim()) return;
    setBusy(true);
    setPairingError(undefined);
    try {
      setPairing(await api.createDevicePairing(name.trim()));
    } catch (caught) {
      setPairingError(caught instanceof Error ? caught.message : "Could not create a pairing.");
    } finally {
      setBusy(false);
    }
  };

  const closePairing = () => {
    if (busy) return;
    setPairingOpen(false);
    setPairing(undefined);
    setPairingError(undefined);
  };

  const pairingUrl = pairing
    ? `${window.location.origin}/#pair=${encodeURIComponent(pairing.secret)}&code=${encodeURIComponent(pairing.confirmationCode)}`
    : "";

  const revokeDevice = async (device: PairedDevice) => {
    if (!api || !await confirm({
      title: device.current ? "Unpair this browser?" : `Revoke ${device.name}?`,
      message: device.current
        ? "This browser will lose access immediately. You will need to pair it again to use Nebula from this device."
        : "That browser will lose access immediately and will need to be paired again.",
      confirmLabel: device.current ? "Unpair browser" : "Revoke device",
      tone: "danger",
    })) return;

    setRevokingId(device.id);
    setError(undefined);
    try {
      await api.revokePairedDevice(device.id);
      setDevices((current) => current.filter((candidate) => candidate.id !== device.id));
      if (device.current) {
        (onCurrentDeviceRevoked ?? (() => window.location.replace("/")))();
        return;
      }
      await refresh();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Could not revoke the paired device.");
    } finally {
      setRevokingId(undefined);
    }
  };

  return <section className="panel device-pairing-panel" id="device-pairing-settings">
    <header className="panel-header compact">
      <div><h2>Paired devices</h2><p>Companion browser access</p></div>
      {canCreatePairing
        ? <button className="button quiet device-pairing-action" type="button" onClick={() => setPairingOpen(true)}><Link2 size={14} /> Pair device</button>
        : <Smartphone size={19} />}
    </header>
    {!canCreatePairing && <div className="device-pairing-capability" role="note"><Smartphone size={17} /><span><strong>Pair new devices from the Nebula host</strong><small>Open the local interface with <code>nebula-core ui</code>, then return here to create a pairing link.</small></span></div>}
    {error && <DiagnosticErrorNotice error={error} fallback="Device pairing failed." compact />}
    <div className="operator-profile-list">{devices.map((device) => <article key={device.id}><span className="operator-profile-avatar"><Smartphone size={16} /></span><div><h3>{device.name}{device.current ? " · this device" : ""}</h3><p>Last used {new Date(device.lastUsedAt).toLocaleString()}</p></div><button className="icon-button subtle" type="button" disabled={disabled || Boolean(revokingId)} aria-label={`${device.current ? "Unpair" : "Revoke"} ${device.name}`} onClick={() => void revokeDevice(device)}>{revokingId === device.id ? <LoaderCircle className="spin" size={14} /> : <Trash2 size={14} />}</button></article>)}</div>
    {pairingOpen && <ModalSurface className="provider-dialog device-pairing-dialog" labelledBy="device-pairing-dialog-title" onClose={closePairing}>
      <header><div><small>Host-authorized access</small><h2 id="device-pairing-dialog-title">Pair a device</h2></div><button className="icon-button subtle" type="button" aria-label="Close pairing dialog" disabled={busy} onClick={closePairing}><X size={17} /></button></header>
      {pairing
        ? <div className="device-pairing-secret" role="status"><strong>Scan on the companion device</strong><QRCodeSVG value={pairingUrl} size={168} bgColor="#ffffff" fgColor="#05070b" level="M" marginSize={2} title="Nebula device pairing QR code" /><code>{pairingUrl}</code><p>Confirm code <b>{pairing.confirmationCode}</b> · expires {new Date(pairing.expiresAt).toLocaleTimeString()}. This link is single-use.</p></div>
        : <><label>Device name<input autoFocus required value={name} maxLength={200} onChange={(event) => setName(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter") { event.preventDefault(); void createPairing(); } }} /></label><p className="provider-dialog-note">The link expires in five minutes and can authorize one browser.</p></>}
      {pairingError && <DiagnosticErrorNotice error={pairingError} fallback="Could not create the pairing link." compact />}
      <footer><button className="button secondary" type="button" disabled={busy} onClick={closePairing}>{pairing ? "Done" : "Cancel"}</button>{!pairing && <button className="button primary" type="button" disabled={busy || !name.trim()} onClick={() => void createPairing()}>{busy ? <LoaderCircle className="spin" size={14} /> : <Link2 size={14} />} {busy ? "Creating…" : "Create pairing link"}</button>}</footer>
    </ModalSurface>}
  </section>;
}
