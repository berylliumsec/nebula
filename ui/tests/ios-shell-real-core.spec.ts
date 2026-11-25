import { randomUUID } from "node:crypto";
import { networkInterfaces } from "node:os";
import { expect, test } from "@playwright/test";
import { startApprovalCore } from "./fixtures/approval-core";
import { withOwnedCleanup } from "./fixtures/owned-cleanup";

/*
 * The installed iPhone app (NebulaShell/1.0) against a production-built LAN Core:
 * pairing, Butler mode (the page desktop shows), and the Projects snapshot of an active project whose
 * chat waits on an approval. The iPhone app has no chat view, so the turn is
 * started and decided through Core's API. This journey lives outside
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

test("iPhone assistant shell production LAN pairs into Butler mode, snapshots a project awaiting approval, and reconnects", async ({page}, testInfo) => {
  test.setTimeout(150_000);
  const core = await startApprovalCore(localNetworkIpv4(), "single");
  let pendingTurn: Promise<unknown> | undefined;
  await withOwnedCleanup(async () => {
    expect((await core.api.post("harnesses/inert-fixture/health")).ok()).toBe(true);
    const build = await (await page.request.get(`${core.origin}/web-build.json`)).json() as {commit: string; assets: Record<string, string>};
    expect(Object.keys(build.assets).some(asset => asset.startsWith("assets/index-") && asset.endsWith(".js"))).toBe(true);
    await testInfo.attach("iphone-shell-lan-build", {body: JSON.stringify({origin: core.origin, build, fixture: "inert harness; no commands"}), contentType: "application/json"});
    await page.addInitScript(() => {
      Object.defineProperty(navigator, "userAgent", {configurable: true, get: () => "Mozilla/5.0 (iPhone) AppleWebKit/605.1.15 NebulaShell/1.0"});
    });

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

    // No chats yet: the project is active but not listed.
    if (compactMode) {
      await page.getByRole("tab", {name: /More/}).click();
      const more = page.getByRole("dialog", {name: "More in Butler"});
      await expect(more.getByRole("button", {name: "Open full Nebula"})).toHaveCount(0);
      await more.getByRole("link", {name: "All projects"}).click();
    } else {
      await page.getByRole("link", {name: "All projects"}).click();
    }
    await expect(page).toHaveURL(/\/projects$/);
    const projects = page.getByRole("region", {name: "Projects"});
    await expect(projects.getByText("No active projects with chats")).toBeVisible({timeout: 20_000});

    // A chat elsewhere asks for approval; the snapshot shows it after a refresh.
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
    await projects.getByRole("button", {name: "Refresh projects"}).click();
    const card = projects.getByRole("listitem").filter({hasText: "1 chat"});
    await expect(card).toBeVisible({timeout: 20_000});
    await expect(card).toContainText("1 need you");
    await card.locator("summary").click();
    await expect(card.getByRole("list", {name: /^Recent chats in /}).getByRole("listitem")).toHaveCount(1);
    await testInfo.attach("iphone-shell-projects", {body: await page.screenshot(), contentType: "image/png"});

    // Decided in Core, the badge clears after the next read.
    const [attention] = await (await core.api.get("chat/attention")).json() as Array<{action_id: string}>;
    const decided = await core.api.post(`approvals/${encodeURIComponent(attention.action_id)}/decision`, {headers: {"Idempotency-Key": randomUUID()}, data: {decision: "approve"}});
    expect(decided.ok(), await decided.text()).toBe(true);
    await expect.poll(async () => (await (await core.api.get("chat/attention")).json() as unknown[]).length, {timeout: 30_000}).toBe(0);
    await projects.getByRole("button", {name: "Refresh projects"}).click();
    await expect(card).not.toContainText("need you", {timeout: 20_000});
    await expect(card).toContainText("1 chat");

    // Reload keeps the route; an outage recovers without re-pairing.
    await page.reload();
    await expect(page).toHaveURL(/\/projects$/);
    await expect(card).toBeVisible({timeout: 20_000});
    await core.disconnect();
    const retryConnection = page.getByRole("button", {name: "Retry connection"});
    await expect(retryConnection).toBeVisible({timeout: 30_000});
    await expect(page.getByText("Pair this phone to continue.")).toHaveCount(0);
    await core.restart();
    await retryConnection.click();
    await expect(page.locator(".workspace-state-banner.failed")).toHaveCount(0, {timeout: 30_000});
    await card.locator("summary").click();
    await card.getByRole("button", {name: /^Ask Butler about /}).click();
    await expect(page).toHaveURL(new RegExp(`/butler\\?project=${projectId}$`));
    await expect(page.getByRole("region", {name: "Status board"})).toBeVisible({timeout: 20_000});
    await page.goBack();
    await page.getByRole("navigation", {name: "Nebula app"}).getByRole("link", {name: "Butler"}).click();
    await expect(page).toHaveURL(new RegExp(`/butler\\?project=${projectId}$`));
  }, [
    {name: "Core fixture", dispose: () => core.stop()},
    {name: "pending fixture turn", dispose: async () => { await pendingTurn; }},
  ]);
});
