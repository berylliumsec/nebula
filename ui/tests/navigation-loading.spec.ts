import {networkInterfaces} from "node:os";
import {expect, test} from "@playwright/test";
import {startApprovalCore} from "./fixtures/approval-core";
import {withOwnedCleanup} from "./fixtures/owned-cleanup";

function lanAddress(): string {
  for (const addresses of Object.values(networkInterfaces())) {
    for (const address of addresses ?? []) {
      if (address.family === "IPv4" && !address.internal) return address.address;
    }
  }
  throw new Error("A non-loopback IPv4 address is required for the LAN workflow.");
}

test("older conversation link restores after the recent page wins a Core read race", async ({page}) => {
  test.setTimeout(120_000);
  const core = await startApprovalCore(lanAddress(), "navigation");
  await withOwnedCleanup(async () => {
    const projects = await (await core.api.get("engagements")).json() as Array<{id: string}>;
    const projectId = projects[0]?.id;
    expect(projectId).toBeTruthy();
    const olderId = "navigation-older";
    const recent = await (await core.api.get(`chat-sessions?engagement_id=${projectId}&recent_first=true&limit=100`)).json() as Array<{id: string}>;
    expect(recent).toHaveLength(100);
    expect(recent.some(session => session.id === olderId)).toBe(false);

    const pairResponse = await core.api.post(`http://127.0.0.1:${core.port}/api/v1/auth/pairings`, {data: {name: "Navigation test device"}});
    expect(pairResponse.ok(), await pairResponse.text()).toBe(true);
    const pairing = await pairResponse.json() as {secret: string; confirmation_code: string};
    await page.goto(`${core.origin}/#pair=${encodeURIComponent(pairing.secret)}&code=${encodeURIComponent(pairing.confirmation_code)}`);
    // PairingGate clears the fragment before redeeming, then reloads the page.
    // Wait for that reload so it cannot abort the older-conversation navigation.
    const pairedLoad = page.waitForEvent("load");
    await page.getByRole("button", {name: "Pair device", exact: true}).click();
    await pairedLoad;

    let exactReads = 0;
    let terminalBootstrapReads = 0;
    page.on("request", request => {
      if (/\/container-terminal\/(capabilities|recover)$|\/container-terminals\/recover$/.test(new URL(request.url()).pathname)) terminalBootstrapReads += 1;
    });
    await page.route(`**/api/v1/chat-sessions/${olderId}`, async route => {
      if (route.request().method() === "GET") {
        exactReads += 1;
        await new Promise(resolve => setTimeout(resolve, 300));
      }
      await route.continue();
    });
    const url = `${core.origin}/projects/${projectId}/workbench?view=chat&session=${olderId}`;
    await page.goto(url);
    await expect(page.getByText("Older pinned context", {exact: true}).last()).toBeVisible();
    await page.reload();
    await expect(page.getByText("Older pinned context", {exact: true}).last()).toBeVisible();
    const show = (page.viewportSize()?.width ?? 1440) <= 760 ? "Open conversations" : "Show conversations";
    await page.getByRole("button", {name: show, exact: true}).click();
    const sidebar = page.getByRole("complementary", {name: "Conversations"});
    const loadMore = sidebar.getByRole("button", {name: "Load older conversations"});
    await expect(loadMore).toBeVisible();
    await loadMore.focus();
    await page.keyboard.press("Enter");
    await expect(loadMore).toHaveCount(0);
    await expect(sidebar.locator('.session-select[data-session-id="navigation-older"]')).toBeVisible();
    expect(exactReads).toBeGreaterThanOrEqual(2);
    expect(exactReads).toBeLessThanOrEqual(4);
    expect(terminalBootstrapReads).toBe(0);
  }, [{name: "Navigation Core fixture", dispose: () => core.stop()}]);
});
