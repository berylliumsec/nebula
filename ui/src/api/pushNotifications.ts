import type { ApiClient } from "./client";

/** What the iPhone app tells the page about this phone (see ios/Nebula/PushPolicy.swift). */
export interface ShellPushState {
  token: string | null;
  environment: "development" | "production";
  authorization: "authorized" | "provisional" | "ephemeral" | "denied" | "notDetermined";
  bundleId: string;
}

declare global {
  interface Window { __nebulaShellPush?: ShellPushState }
}

export const SHELL_PUSH_EVENT = "nebula-shell-push";

export function readShellPush(): ShellPushState | undefined {
  const value = typeof window === "undefined" ? undefined : window.__nebulaShellPush;
  if (!value || typeof value !== "object") return undefined;
  if (value.token !== null && (typeof value.token !== "string" || !/^[0-9a-f]{16,200}$/.test(value.token))) return undefined;
  return value;
}

export interface PushDevice {
  id: string;
  label: string;
  environment: string;
  enabled: boolean;
  thisDevice: boolean;
  lastSentAt?: string;
  lastError?: string;
  createdAt: string;
  testPending: boolean;
  unregistered: boolean;
}

export interface PushStatus {
  /** Core holds an APNs key, key ID, team ID and topic, and the switch is on. */
  configured: boolean;
  enabled: boolean;
  missing: string[];
  keySaved: boolean;
  keyId?: string;
  teamId?: string;
  topic?: string;
  lastError?: string;
  devices: PushDevice[];
  revision: number;
}

interface WirePushDevice {
  id: string;
  label: string;
  environment: string;
  enabled: boolean;
  this_device: boolean;
  last_sent_at?: string | null;
  last_error?: string | null;
  created_at: string;
  test_pending?: boolean;
  unregistered?: boolean;
}

interface WirePushStatus {
  configured: boolean;
  enabled?: boolean;
  missing?: string[];
  key_saved?: boolean;
  key_id?: string | null;
  team_id?: string | null;
  topic?: string | null;
  last_error?: string | null;
  devices?: WirePushDevice[];
  revision?: number;
}

function mapDevice(wire: WirePushDevice): PushDevice {
  return {
    id: wire.id,
    label: wire.label,
    environment: wire.environment,
    enabled: wire.enabled,
    thisDevice: wire.this_device,
    lastSentAt: wire.last_sent_at ?? undefined,
    lastError: wire.last_error ?? undefined,
    createdAt: wire.created_at,
    testPending: wire.test_pending ?? false,
    unregistered: wire.unregistered ?? false,
  };
}

function mapStatus(wire: WirePushStatus): PushStatus {
  return {
    configured: wire.configured,
    enabled: wire.enabled ?? true,
    missing: wire.missing ?? [],
    keySaved: wire.key_saved ?? false,
    keyId: wire.key_id ?? undefined,
    teamId: wire.team_id ?? undefined,
    topic: wire.topic ?? undefined,
    lastError: wire.last_error ?? undefined,
    devices: (wire.devices ?? []).map(mapDevice),
    revision: wire.revision ?? 1,
  };
}

export interface PushSettingsChange {
  keyRef?: string;
  keyId?: string;
  teamId?: string;
  topic?: string;
  enabled?: boolean;
}

/** The .p8 key goes to the credential vault first; Core stores only its reference. */
export async function savePushSettings(api: ApiClient, change: PushSettingsChange, expectedRevision?: number): Promise<PushStatus> {
  const body: Record<string, unknown> = {};
  if (change.keyRef !== undefined) body.key_ref = change.keyRef;
  if (change.keyId !== undefined) body.key_id = change.keyId;
  if (change.teamId !== undefined) body.team_id = change.teamId;
  if (change.topic !== undefined) body.topic = change.topic;
  if (change.enabled !== undefined) body.enabled = change.enabled;
  if (expectedRevision !== undefined) body.expected_revision = expectedRevision;
  return mapStatus(await api.request<WirePushStatus>("notifications/push/settings", { method: "PUT", body: JSON.stringify(body) }));
}

export async function removePushDevice(api: ApiClient, deviceId: string): Promise<void> {
  await api.request<void>(`notifications/push/devices/${encodeURIComponent(deviceId)}`, { method: "DELETE" });
}

/** A real APNs .p8 key: one PEM private key block, nothing else. */
export function isApnsKey(text: string): boolean {
  const trimmed = text.trim();
  return /^-----BEGIN PRIVATE KEY-----\s+[A-Za-z0-9+/=\s]+-----END PRIVATE KEY-----$/.test(trimmed) && trimmed.length < 4096;
}

export async function getPushStatus(api: ApiClient, signal?: AbortSignal): Promise<PushStatus> {
  return mapStatus(await api.request<WirePushStatus>("notifications/push", { signal }));
}

/** Idempotent: Core keys the phone by its APNs token and links it to this paired device. */
export async function registerPushDevice(api: ApiClient, state: ShellPushState & { token: string }): Promise<PushDevice> {
  return mapDevice(await api.request<WirePushDevice>("notifications/push/devices", {
    method: "PUT",
    body: JSON.stringify({ token: state.token, environment: state.environment, topic: state.bundleId, platform: "ios" }),
  }));
}

export async function setPushDeviceEnabled(api: ApiClient, deviceId: string, enabled: boolean): Promise<PushDevice> {
  return mapDevice(await api.request<WirePushDevice>(`notifications/push/devices/${encodeURIComponent(deviceId)}`, {
    method: "PATCH",
    body: JSON.stringify({ enabled }),
  }));
}

/** Queues one test alert; Core's sender records Apple's answer on the phone within seconds. */
export async function sendTestPush(api: ApiClient, deviceId: string): Promise<PushDevice> {
  return mapDevice(await api.request<WirePushDevice>(`notifications/push/devices/${encodeURIComponent(deviceId)}/test`, { method: "POST", body: "{}" }));
}

/** Polls until the queued test has an answer from Apple, or gives up after ``timeoutMs``. */
async function awaitTestResult(api: ApiClient, deviceId: string, timeoutMs: number): Promise<PushDevice | undefined> {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    await new Promise(resolve => setTimeout(resolve, 1500));
    const device = (await getPushStatus(api)).devices.find(item => item.id === deviceId);
    if (!device) return undefined;
    if (!device.testPending) return device;
  }
  return undefined;
}

/** Queue a test alert and wait for Apple's answer, as Core recorded it. */
export async function runPushTest(api: ApiClient, deviceId: string, timeoutMs = 30_000): Promise<{ ok: boolean; text: string }> {
  const since = Date.now();
  await sendTestPush(api, deviceId);
  return testOutcome(await awaitTestResult(api, deviceId, timeoutMs), since);
}

/** Whether the phone's last answer from Apple came after ``since`` and succeeded. */
export function testOutcome(device: PushDevice | undefined, since: number): { ok: boolean; text: string } {
  if (!device) return { ok: false, text: "Core's sender did not answer within 30 seconds. Check that Core is running, then try again." };
  const sent = device.lastSentAt ? Date.parse(device.lastSentAt) : 0;
  if (sent >= since - 1000 && !device.lastError) return { ok: true, text: "Apple accepted the test alert. It should be on the phone now." };
  return { ok: false, text: device.lastError ?? "Apple did not accept the test alert." };
}
