import { useCallback, useEffect, useState } from "react";
import { RefreshCw, Send, Smartphone, WifiOff } from "lucide-react";
import type { ApiClient } from "../api/client";
import { getPushStatus, readShellPush, registerPushDevice, runPushTest, setPushDeviceEnabled, SHELL_PUSH_EVENT, type PushStatus, type ShellPushState } from "../api/pushNotifications";
import { logCaughtDiagnostic } from "../diagnostics";
import styles from "./ShellPushNotifications.module.css";

function useShellPush(): ShellPushState | undefined {
  const [state, setState] = useState(readShellPush);
  useEffect(() => {
    const update = () => setState(readShellPush());
    window.addEventListener(SHELL_PUSH_EVENT, update);
    return () => window.removeEventListener(SHELL_PUSH_EVENT, update);
  }, []);
  return state;
}

/**
 * Registers this iPhone with Core whenever the app hands the page a push token.
 * Mounted once by the app shell, so registration does not depend on which
 * Butler section is open. Core links the token to the paired device, so
 * revoking the pairing also stops its notifications.
 */
export function ShellPushRegistration({ api }: { api: ApiClient }) {
  const push = useShellPush();
  const token = push?.token;
  useEffect(() => {
    if (!push || !token) return;
    registerPushDevice(api, { ...push, token }).catch((error: unknown) => logCaughtDiagnostic("interface.push.register_failed", "This phone could not register for Butler notifications.", error, "push-notifications"));
  }, [api, push, token]);
  return null;
}

type Phase = { kind: "idle" } | { kind: "busy" } | { kind: "done"; text: string } | { kind: "failed"; text: string };

/** Butler mode → Settings in the iPhone app: is this phone getting Butler alerts, and why not. */
export function ShellPushSettings({ api }: { api: ApiClient }) {
  const push = useShellPush();
  const [status, setStatus] = useState<PushStatus>();
  const [readError, setReadError] = useState<string>();
  const [phase, setPhase] = useState<Phase>({ kind: "idle" });

  const load = useCallback(async (signal?: AbortSignal) => {
    try {
      setStatus(await getPushStatus(api, signal));
      setReadError(undefined);
    } catch (error) {
      if (signal?.aborted) return;
      logCaughtDiagnostic("interface.push.status_failed", "Butler notification status could not be read.", error, "push-notifications");
      setReadError("Core did not return notification status.");
    }
  }, [api]);

  useEffect(() => {
    const controller = new AbortController();
    void load(controller.signal);
    return () => controller.abort();
  }, [load, push?.token]);

  const device = status?.devices.find(item => item.thisDevice);
  const run = async (action: () => Promise<string>) => {
    setPhase({ kind: "busy" });
    try {
      setPhase({ kind: "done", text: await action() });
    } catch (error) {
      logCaughtDiagnostic("interface.push.action_failed", "A Butler notification change failed.", error, "push-notifications");
      setPhase({ kind: "failed", text: error instanceof Error ? error.message : "Core refused the request." });
    }
    await load();
  };

  let state: { tone: "on" | "off" | "warn"; text: string };
  if (!push) state = { tone: "warn", text: "Update the Nebula app to get Butler notifications." };
  else if (push.authorization === "denied") state = { tone: "off", text: "Notifications are off for Nebula in iOS Settings → Notifications." };
  else if (!push.token) state = { tone: "warn", text: "Waiting for Apple to give this phone a notification address." };
  else if (readError) state = { tone: "warn", text: readError };
  else if (!status) state = { tone: "warn", text: "Checking…" };
  else if (!status.enabled) state = { tone: "off", text: "Butler notifications are turned off in Core's Settings → Push notifications." };
  else if (!status.configured) state = { tone: "warn", text: `Core has no Apple push key yet (missing ${status.missing.join(", ")}). Add it in full Nebula: Settings → Push notifications.` };
  else if (status.lastError) state = { tone: "warn", text: status.lastError };
  else if (!device) state = { tone: "warn", text: "This phone is not registered with Core yet." };
  else if (device.unregistered) state = { tone: "warn", text: "Apple retired this phone's notification address. Reopen the app to register again." };
  else if (!device.enabled) state = { tone: "off", text: "Butler notifications are paused for this phone." };
  else if (device.lastError) state = { tone: "warn", text: `Last alert failed: ${device.lastError}` };
  else state = { tone: "on", text: "On. Butler alerts this phone when a project needs you or is blocked." };

  return <section className={styles.card} aria-label="Push notifications">
    <div className={styles.head}>
      {state.tone === "on" ? <Smartphone size={16} aria-hidden="true" /> : <WifiOff size={16} aria-hidden="true" />}
      <h3>Push notifications</h3>
      <button type="button" className={styles.icon} aria-label="Refresh notification status" title="Refresh" onClick={() => void load()}><RefreshCw size={15} aria-hidden="true" /></button>
    </div>
    <p className={styles.state} data-tone={state.tone} role="status">{state.text}</p>
    {device && status?.configured && <div className={styles.actions}>
      <button type="button" className="button quiet" disabled={phase.kind === "busy"} onClick={() => void run(async () => {
        const next = await setPushDeviceEnabled(api, device.id, !device.enabled);
        return next.enabled ? "Notifications resumed." : "Notifications paused.";
      })}>{device.enabled ? "Pause" : "Resume"}</button>
      {device.enabled && <button type="button" className="button quiet" disabled={phase.kind === "busy"} onClick={() => void run(async () => {
        const outcome = await runPushTest(api, device.id);
        if (outcome.ok) return outcome.text;
        throw new Error(outcome.text);
      })}><Send size={14} aria-hidden="true" /> Send test</button>}
    </div>}
    {phase.kind === "busy" && <p className={styles.result} role="status">Waiting for Core…</p>}
    {phase.kind === "done" && <p className={styles.result} role="status">{phase.text}</p>}
    {phase.kind === "failed" && <p className={styles.result} data-tone="warn" role="alert">{phase.text}</p>}
  </section>;
}
