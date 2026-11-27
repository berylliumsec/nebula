import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";
import { Pause, Play, Send, Smartphone, Trash2 } from "lucide-react";
import type { ApiClient } from "../api/client";
import { getPushStatus, isApnsKey, removePushDevice, runPushTest, savePushSettings, setPushDeviceEnabled, type PushStatus } from "../api/pushNotifications";
import { DiagnosticErrorNotice, logCaughtDiagnostic } from "../diagnostics";
import { useConfirmation } from "./DialogSystem";
import styles from "./PushNotificationSettings.module.css";

const ID_PATTERN = /^[A-Z0-9]{10}$/;

/**
 * Settings → Push notifications. Core sends Butler alerts to the iPhone app
 * through Apple's push service; this panel holds the APNs key (saved to the
 * credential vault, write-only) and lists the phones that receive alerts.
 */
export function PushNotificationSettings({ api, disabled, active = true }: { api?: ApiClient; disabled: boolean; active?: boolean }) {
  const confirm = useConfirmation();
  const [status, setStatus] = useState<PushStatus>();
  const [error, setError] = useState<unknown>();
  const [notice, setNotice] = useState<{ ok: boolean; text: string }>();
  const [busy, setBusy] = useState<string>();
  const [keyText, setKeyText] = useState("");
  const [keyName, setKeyName] = useState("");
  const [keyId, setKeyId] = useState("");
  const [teamId, setTeamId] = useState("");
  const fileRef = useRef<HTMLInputElement>(null);

  const load = useCallback(async (signal?: AbortSignal) => {
    if (!api) return;
    try {
      const next = await getPushStatus(api, signal);
      setStatus(next);
      setKeyId(current => current || next.keyId || "");
      setTeamId(current => current || next.teamId || "");
      setError(undefined);
    } catch (caught) {
      if (signal?.aborted) return;
      logCaughtDiagnostic("interface.push.settings_read_failed", "Phone notification settings could not be read.", caught, "push-notifications");
      setError(caught);
    }
  }, [api]);

  // Read Core only while the section can be seen, not whenever Settings mounts.
  useEffect(() => {
    if (!active) return;
    const controller = new AbortController();
    void load(controller.signal);
    return () => controller.abort();
  }, [load, active]);

  const act = async (label: string, action: () => Promise<{ ok: boolean; text: string } | void>) => {
    setBusy(label);
    setNotice(undefined);
    try {
      const outcome = await action();
      if (outcome) setNotice(outcome);
    } catch (caught) {
      logCaughtDiagnostic("interface.push.settings_change_failed", "A phone notification setting could not be changed.", caught, "push-notifications");
      setNotice({ ok: false, text: caught instanceof Error ? caught.message : "Core refused the change." });
    } finally {
      setBusy(undefined);
      await load();
    }
  };

  const pickKey = async (file: File | undefined) => {
    if (!file) return;
    const text = await file.text();
    if (!isApnsKey(text)) {
      setNotice({ ok: false, text: `${file.name} is not an APNs .p8 key. Download the key from Apple Developer → Keys.` });
      setKeyText("");
      setKeyName("");
      return;
    }
    setNotice(undefined);
    setKeyText(text.trim());
    setKeyName(file.name);
    // Apple names the file AuthKey_<KEY ID>.p8.
    const match = /AuthKey_([A-Z0-9]{10})\.p8$/i.exec(file.name);
    if (match) setKeyId(match[1].toUpperCase());
  };

  const save = (event: FormEvent) => {
    event.preventDefault();
    if (!api || !status) return;
    void act("save", async () => {
      let keyRef: string | undefined;
      let where = "the credential vault";
      if (keyText) {
        try {
          keyRef = (await api.createCredential(keyText, "vault")).reference;
        } catch (caught) {
          // A headless Core's OS keyring is often locked; Nebula's encrypted store works there.
          logCaughtDiagnostic("interface.push.vault_unavailable", "The OS credential vault refused the APNs key; using Nebula's encrypted store.", caught, "push-notifications");
          keyRef = (await api.createCredential(keyText, "store")).reference;
          where = "Nebula's encrypted credential store";
        }
      }
      await savePushSettings(api, { keyRef, keyId: keyId.trim().toUpperCase(), teamId: teamId.trim().toUpperCase() }, status.revision);
      setKeyText("");
      setKeyName("");
      if (fileRef.current) fileRef.current.value = "";
      return { ok: true, text: keyRef ? `Saved. The key is in ${where}.` : "Saved." };
    });
  };

  const idsValid = ID_PATTERN.test(keyId.trim().toUpperCase()) && ID_PATTERN.test(teamId.trim().toUpperCase());
  const canSave = Boolean(api && status && !disabled && !busy && idsValid && (keyText || status.keySaved));
  const summary = !status ? "Checking…"
    : !status.enabled ? "Off"
    : status.configured ? `On · ${status.devices.filter(item => item.enabled).length} phone${status.devices.filter(item => item.enabled).length === 1 ? "" : "s"}`
    : `Needs ${status.missing.join(", ")}`;

  return <section className={`panel ${styles.panel}`} id="push-notification-settings" aria-labelledby="push-notification-title">
    <header className="panel-header compact"><div><h2 id="push-notification-title">Push notifications</h2><p>{summary}</p></div><Smartphone size={19} aria-hidden="true" /></header>
    <p className={styles.help}>Butler alerts the Nebula iPhone app when a project needs you or is blocked. Add an APNs key from Apple Developer → Certificates, IDs &amp; Profiles → Keys, with Apple Push Notifications service enabled.</p>
    {error !== undefined && <DiagnosticErrorNotice error={error} fallback="Core did not return notification settings." compact />}
    {status?.lastError && <p className={styles.help} role="alert">{status.lastError}</p>}
    <form className={styles.form} onSubmit={save}>
      <label>APNs key (.p8)
        <input ref={fileRef} type="file" accept=".p8,application/pkcs8,application/x-pem-file,text/plain" disabled={disabled || Boolean(busy)} onChange={event => void pickKey(event.target.files?.[0])} />
        <small>{keyName ? `${keyName} ready to save` : status?.keySaved ? "A key is saved. Choose a file only to replace it." : "No key saved"}</small>
      </label>
      <label>Key ID<input value={keyId} maxLength={10} autoCapitalize="characters" spellCheck={false} disabled={disabled || Boolean(busy)} onChange={event => setKeyId(event.target.value)} placeholder="ABC123DEFG" /></label>
      <label>Team ID<input value={teamId} maxLength={10} autoCapitalize="characters" spellCheck={false} disabled={disabled || Boolean(busy)} onChange={event => setTeamId(event.target.value)} placeholder="TEAM123456" /></label>
      <div className={styles.actions}>
        <button className="button primary" type="submit" disabled={!canSave}>{busy === "save" ? "Saving…" : "Save"}</button>
        {status?.configured !== undefined && status.keySaved && <button className="button quiet" type="button" disabled={disabled || Boolean(busy)} onClick={() => void act("switch", async () => {
          await savePushSettings(api!, { enabled: !status.enabled }, status.revision);
          return { ok: true, text: status.enabled ? "Butler notifications are off for every phone." : "Butler notifications are on." };
        })}>{status.enabled ? "Turn off" : "Turn on"}</button>}
      </div>
    </form>
    <h3 className={styles.subhead}>Phones</h3>
    {status && status.devices.length === 0 && <p className={styles.help}>No phone has registered yet. Open the Nebula iPhone app and allow notifications.</p>}
    {status && status.devices.length > 0 && <ul className={styles.devices}>
      {status.devices.map(device => <li key={device.id}>
        <div>
          <strong>{device.label}{device.thisDevice ? " (this device)" : ""}</strong>
          <small>{device.unregistered ? "Apple retired this address; it registers again when the app opens" : !device.enabled ? "Paused" : device.lastError ? device.lastError : device.lastSentAt ? `Last alert ${new Date(device.lastSentAt).toLocaleString()}` : "Registered"} · {device.environment === "development" ? "development build" : "App Store build"}</small>
        </div>
        <button className="icon-button subtle" type="button" aria-label={`Send a test alert to ${device.label}`} title="Send test" disabled={disabled || Boolean(busy) || !status.configured || !device.enabled} onClick={() => void act(`test-${device.id}`, () => runPushTest(api!, device.id))}><Send size={14} aria-hidden="true" /></button>
        <button className="icon-button subtle" type="button" aria-label={device.enabled ? `Pause alerts to ${device.label}` : `Resume alerts to ${device.label}`} title={device.enabled ? "Pause" : "Resume"} disabled={disabled || Boolean(busy)} onClick={() => void act(`pause-${device.id}`, async () => { await setPushDeviceEnabled(api!, device.id, !device.enabled); })}>{device.enabled ? <Pause size={14} aria-hidden="true" /> : <Play size={14} aria-hidden="true" />}</button>
        <button className="icon-button subtle" type="button" aria-label={`Remove ${device.label}`} title="Remove" disabled={disabled || Boolean(busy)} onClick={() => void act(`remove-${device.id}`, async () => {
          if (!await confirm({ title: `Stop alerts to ${device.label}?`, message: "The phone registers again the next time the Nebula app opens. To stop it for good, pause it instead.", confirmLabel: "Remove", tone: "danger" })) return;
          await removePushDevice(api!, device.id);
        })}><Trash2 size={14} aria-hidden="true" /></button>
      </li>)}
    </ul>}
    {busy?.startsWith("test-") && <p className={styles.help} role="status">Sending a test alert…</p>}
    {notice && <p className={styles.help} data-tone={notice.ok ? undefined : "warn"} role={notice.ok ? "status" : "alert"}>{notice.text}</p>}
  </section>;
}
