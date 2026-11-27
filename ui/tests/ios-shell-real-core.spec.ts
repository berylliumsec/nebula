import { generateKeyPairSync, randomUUID } from "node:crypto";
import { createServer, type IncomingHttpHeaders } from "node:http";
import type { AddressInfo } from "node:net";
import { networkInterfaces } from "node:os";
import { expect, test } from "@playwright/test";
import { startApprovalCore } from "./fixtures/approval-core";
import { withOwnedCleanup } from "./fixtures/owned-cleanup";

/*
 * The installed iPhone app (NebulaShell/1.0) against a production-built LAN Core:
 * pairing, Butler mode only (the page desktop shows; every other route lands
 * back in it), and Butler push notifications: the app's APNs token registers
 * through the pairing, and a chat waiting on an approval reaches a local fake
 * APNs service as an alert that opens Oversight. The iPhone app has no chat
 * view, so the turn is started and decided through Core's API. This journey lives outside
 * real-core.spec.ts, which is above the public commit guard's per-file limit.
 */

function localNetworkIpv4(): string {
  for (const addresses of Object.values(networkInterfaces())) {
    for (const address of addresses ?? []) {
      if (address.family === "IPv4" && !address.internal) return address.address;
    }
  }
  throw new Error("A non-loopback IPv4 address is required for LAN acceptance.");
}

interface FakeAlert { path: string; headers: IncomingHttpHeaders; body: {aps: {alert: {title: string; body: string}}; nebula_path: string} }

/** Stands in for Apple's push service; records what Core sends. */
async function startFakeApns() {
  const alerts: FakeAlert[] = [];
  const server = createServer((request, response) => {
    let raw = "";
    request.on("data", chunk => { raw += chunk; });
    request.on("end", () => {
      alerts.push({path: request.url ?? "", headers: request.headers, body: JSON.parse(raw || "{}")});
      response.writeHead(200).end();
    });
  });
  await new Promise<void>(resolve => server.listen(0, "127.0.0.1", resolve));
  return {alerts, url: `http://127.0.0.1:${(server.address() as AddressInfo).port}`, close: () => new Promise<void>(resolve => server.close(() => resolve()))};
}

test("iPhone assistant shell production LAN stays in Butler mode, registers for Butler alerts, alerts on an approval, and reconnects", async ({page}, testInfo) => {
  test.setTimeout(180_000);
  const apns = await startFakeApns();
  // Core sends to the fake service with a throwaway P-256 key; no Apple account is involved.
  process.env.NEBULA_APNS_BASE_URL = apns.url;
  process.env.NEBULA_TEST_APNS_KEY = generateKeyPairSync("ec", {namedCurve: "P-256"}).privateKey.export({format: "pem", type: "pkcs8"}).toString();
  const core = await startApprovalCore(localNetworkIpv4(), "single");
  const token = "ab".repeat(32);
  let pendingTurn: Promise<unknown> | undefined;
  await withOwnedCleanup(async () => {
    expect((await core.api.post("harnesses/inert-fixture/health")).ok()).toBe(true);
    const build = await (await page.request.get(`${core.origin}/web-build.json`)).json() as {commit: string; assets: Record<string, string>};
    expect(Object.keys(build.assets).some(asset => asset.startsWith("assets/index-") && asset.endsWith(".js"))).toBe(true);
    await testInfo.attach("iphone-shell-lan-build", {body: JSON.stringify({origin: core.origin, build, fixture: "inert harness; no commands"}), contentType: "application/json"});
    await page.addInitScript((pushToken) => {
      Object.defineProperty(navigator, "userAgent", {configurable: true, get: () => "Mozilla/5.0 (iPhone) AppleWebKit/605.1.15 NebulaShell/1.0"});
      // What the native app injects once iOS hands it an APNs token.
      (window as unknown as {__nebulaShellPush: unknown}).__nebulaShellPush = {token: pushToken, environment: "development", authorization: "authorized", bundleId: "com.berylliumsec.nebula.ios"};
    }, token);

    // Unpaired: the shell asks to pair instead of inventing an empty project list.
    await page.goto(core.origin);
    const unpairedBanner = page.locator(".assistant-only-shell > .main-content > .workspace-state-banner.failed");
    await expect(unpairedBanner).toBeVisible({timeout: 20_000});
    await expect(unpairedBanner.getByText("Pair this phone to continue.")).toBeVisible();
    await expect(page.getByRole("heading", {name: "No projects yet"})).toHaveCount(0);

    const pairing = await (await core.api.post(`http://127.0.0.1:${core.port}/api/v1/auth/pairings`, {data: {name: "iPhone shell acceptance"}})).json();
    await page.goto(`${core.origin}/#pair=${encodeURIComponent(pairing.secret)}&code=${encodeURIComponent(pairing.confirmation_code)}`);
    await page.getByLabel("Device name").fill("iPhone shell acceptance");
    await page.getByRole("button", {name: "Pair device", exact: true}).click();

    // Paired: the app opens Butler mode, the same page desktop shows, from the remembered project.
    await expect(page).toHaveURL(/\/butler\?project=[^&]+$/, {timeout: 20_000});
    const projectId = new URL(page.url()).searchParams.get("project")!;
    await expect(page.getByRole("heading", {name: "Butler", level: 1})).toBeVisible({timeout: 20_000});
    await expect(page.getByRole("region", {name: "Status board"})).toBeVisible({timeout: 20_000});
    await expect(page.getByRole("dialog", {name: "Butler"})).toBeHidden();
    await expect(page.getByRole("button", {name: "Exit Butler mode"})).toHaveCount(0);
    const compactMode = await page.evaluate(() => matchMedia("(max-width: 760px)").matches);
    if (compactMode) {
      await page.getByRole("tab", {name: /^Threads/}).click();
      if (!await page.getByRole("textbox", {name: "Message Butler"}).isVisible()) await page.getByRole("navigation", {name: "Butler threads"}).getByRole("button", {name: "New", exact: true}).click();
    }
    await expect(page.getByRole("textbox", {name: "Message Butler"})).toBeVisible();
    await expect(page.getByRole("combobox", {name: "Project for the new thread"})).toHaveValue(projectId);
    await testInfo.attach("iphone-shell-butler-mode", {body: await page.screenshot(), contentType: "image/png"});
    await page.getByRole("button", {name: "Start Butler Voice"}).click();
    await expect(page.getByRole("dialog", {name: "Butler Voice"}).getByRole("alert")).toContainText("trusted HTTPS address");
    await page.getByRole("button", {name: "Exit voice mode"}).click();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);

    // Butler mode only: no way out to Projects, and every other route lands back in Butler.
    await expect(page.getByRole("link", {name: "All projects"})).toHaveCount(0);
    for (const path of ["/projects", "/settings"]) {
      await page.goto(`${core.origin}${path}`);
      await expect(page).toHaveURL(new RegExp(`/butler\\?project=${projectId}$`), {timeout: 20_000});
    }

    // The page registered this phone's token with Core through its pairing.
    await expect.poll(async () => ((await (await core.api.get("notifications/push")).json()) as {devices: Array<{label: string}>}).devices.map(item => item.label), {timeout: 20_000}).toEqual(["iPhone shell acceptance"]);
    const configured = await core.api.put("notifications/push/settings", {data: {key_ref: "env:NEBULA_TEST_APNS_KEY", key_id: "ABC123DEFG", team_id: "TEAM123456"}});
    expect(configured.ok(), await configured.text()).toBe(true);
    // The first configured pass takes the baseline; the next welcomes the new phone.
    await expect.poll(() => apns.alerts.map(item => item.body.aps.alert.title), {timeout: 90_000}).toContain("Butler notifications are on");
    const welcome = apns.alerts[0];
    expect(welcome.path).toBe(`/3/device/${token}`);
    expect(welcome.headers["apns-topic"]).toBe("com.berylliumsec.nebula.ios");
    expect(String(welcome.headers.authorization)).toMatch(/^bearer ey/);

    // A chat elsewhere asks for approval; Butler opens an incident and the phone gets one alert.
    pendingTurn = core.api.post("chat/completions", {timeout: 120_000, headers: {"Idempotency-Key": randomUUID()}, data: {
      backend: "harness", harness_profile_id: "inert-fixture", model: "fixture", engagement_id: projectId,
      tools_enabled: true, stream: false, messages: [{role: "user", content: "Review the inert fixture request. No commands execute."}],
    }}).then(async response => ({status: response.status(), body: await response.text().catch(() => "")}), error => ({error: String(error)}));
    let settled: unknown;
    void pendingTurn.then(value => { settled = value; });
    await expect.poll(async () => {
      if (settled) throw new Error(`The fixture turn ended before asking for approval: ${JSON.stringify(settled).slice(0, 500)}`);
      return (await (await core.api.get("chat/attention")).json() as unknown[]).length;
    }, {timeout: 30_000}).toBe(1);
    await expect.poll(() => apns.alerts.filter(item => item.body.nebula_path?.includes("section=oversight") && item.body.aps.alert.title.endsWith("needs you")).length, {timeout: 60_000}).toBe(1);
    const alert = apns.alerts.find(item => item.body.aps.alert.title.endsWith("needs you"))!;
    expect(alert.body.nebula_path).toBe(`/butler?project=${projectId}&section=oversight`);
    await testInfo.attach("iphone-butler-alert", {body: JSON.stringify(alert.body, null, 2), contentType: "application/json"});

    // Tapping it opens Oversight in Butler mode.
    await page.goto(`${core.origin}${alert.body.nebula_path}`);
    await expect(page.getByRole("region", {name: "Oversight", exact: true})).toBeVisible({timeout: 20_000});

    // In the app: Butler mode → Settings shows this phone on.
    await page.goto(`${core.origin}/butler?project=${projectId}&section=settings`);
    const notifications = page.getByRole("region", {name: "Push notifications"});
    await expect(notifications.getByRole("status").first()).toContainText("On. Butler alerts this phone", {timeout: 20_000});
    await notifications.getByRole("button", {name: "Send test"}).click();
    await expect(notifications.getByText("Apple accepted the test alert")).toBeVisible({timeout: 40_000});
    expect(apns.alerts.map(item => item.body.aps.alert.title)).toContain("Test from Butler");
    await testInfo.attach("iphone-shell-notifications", {body: await page.screenshot(), contentType: "image/png"});

    // Decided in Core: no further alert for the same approval.
    const alertCount = apns.alerts.length;
    const [attention] = await (await core.api.get("chat/attention")).json() as Array<{action_id: string}>;
    const decided = await core.api.post(`approvals/${encodeURIComponent(attention.action_id)}/decision`, {headers: {"Idempotency-Key": randomUUID()}, data: {decision: "approve"}});
    expect(decided.ok(), await decided.text()).toBe(true);
    await expect.poll(async () => (await (await core.api.get("chat/attention")).json() as unknown[]).length, {timeout: 30_000}).toBe(0);
    await page.waitForTimeout(6_000);
    expect(apns.alerts.length).toBe(alertCount);

    // Reload keeps Butler mode; an outage recovers without re-pairing.
    await page.reload();
    await expect(page).toHaveURL(new RegExp(`/butler\\?project=${projectId}`));
    await core.disconnect();
    // Butler mode covers the app's connection banner, so its own alert offers the retry.
    const outage = page.getByRole("alert").filter({hasText: "Core is unavailable. Butler mode reconnects"});
    await expect(outage).toBeVisible({timeout: 30_000});
    await expect(page.getByText("Pair this phone to continue.")).toHaveCount(0);
    await core.restart();
    // The page may recover on its own before Retry is pressed; press it only while shown.
    await expect(async () => {
      if (await outage.isVisible()) await outage.getByRole("button", {name: "Retry connection"}).click({timeout: 2_000});
      await expect(outage).toHaveCount(0, {timeout: 2_000});
    }).toPass({timeout: 60_000});
    await expect(page.getByRole("heading", {name: "Butler", level: 1})).toBeVisible({timeout: 20_000});
  }, [
    {name: "Core fixture", dispose: () => core.stop()},
    {name: "pending fixture turn", dispose: async () => { await pendingTurn; }},
    {name: "fake APNs", dispose: () => apns.close()},
  ]);
});
