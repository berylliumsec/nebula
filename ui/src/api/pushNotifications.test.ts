import { afterEach, describe, expect, it } from "vitest";
import { isApnsKey, readShellPush, testOutcome, type PushDevice } from "./pushNotifications";
import { settingCatalog, settingCatalogEntry, settingCatalogText } from "../settingsCatalog";
import { settingToOpen } from "../components/nebulaAssistantActions";

const device: PushDevice = { id: "p", label: "iPhone", environment: "development", enabled: true, thisDevice: true, createdAt: "2026-11-27T00:00:00Z", testPending: false, unregistered: false };

describe("Butler phone notifications", () => {
  afterEach(() => { delete window.__nebulaShellPush; });

  it("reads only a well-formed push state from the iPhone app", () => {
    expect(readShellPush()).toBeUndefined();
    window.__nebulaShellPush = { token: "ab".repeat(32), environment: "development", authorization: "authorized", bundleId: "com.berylliumsec.nebula.ios" };
    expect(readShellPush()?.token).toBe("ab".repeat(32));
    window.__nebulaShellPush = { token: null, environment: "production", authorization: "denied", bundleId: "x" };
    expect(readShellPush()?.authorization).toBe("denied");
    window.__nebulaShellPush = { token: "not hex", environment: "production", authorization: "authorized", bundleId: "x" };
    expect(readShellPush()).toBeUndefined();
  });

  it("accepts only an APNs .p8 private key", () => {
    expect(isApnsKey("-----BEGIN PRIVATE KEY-----\nMIGTAgEAMBMGByqGSM49AgEGCCqGSM49AwEHBHkwdwIBAQQg\n-----END PRIVATE KEY-----\n")).toBe(true);
    expect(isApnsKey("-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----")).toBe(false);
    expect(isApnsKey("hello")).toBe(false);
  });

  it("reports a test alert only when Apple answered after it was queued", () => {
    const since = Date.parse("2026-11-27T10:00:00Z");
    expect(testOutcome({ ...device, lastSentAt: "2026-11-27T10:00:02Z" }, since).ok).toBe(true);
    expect(testOutcome({ ...device, lastSentAt: "2026-11-27T09:00:00Z" }, since).ok).toBe(false);
    expect(testOutcome({ ...device, lastError: "Apple refused the alert: BadDeviceToken" }, since)).toEqual({ ok: false, text: "Apple refused the alert: BadDeviceToken" });
    expect(testOutcome(undefined, since).text).toContain("did not answer");
  });

  it("is discoverable in Settings and opens from an assistant request", () => {
    const entry = settingCatalogEntry("settings.push-notifications");
    expect(entry.target).toBe("push-notification-settings");
    const text = settingCatalogText(entry).toLowerCase();
    for (const term of ["notifications", "apns", "blocked", "needs you", "iphone"]) expect(text).toContain(term);
    expect(settingCatalog.filter(item => item.keywords.includes("push notifications")).map(item => item.id)).toEqual(["settings.push-notifications"]);
    expect(settingToOpen("Open phone notifications settings")?.id).toBe("settings.push-notifications");
    expect(settingToOpen("take me to push notifications")?.id).toBe("settings.push-notifications");
  });
});
