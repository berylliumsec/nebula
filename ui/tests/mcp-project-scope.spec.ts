import AxeBuilder from "@axe-core/playwright";
import {networkInterfaces} from "node:os";
import {expect, test, type Page} from "@playwright/test";
import {startApprovalCore} from "./fixtures/approval-core";
import {withOwnedCleanup} from "./fixtures/owned-cleanup";

function localNetworkIpv4(): string {
  for (const addresses of Object.values(networkInterfaces())) {
    for (const address of addresses ?? []) {
      if (address.family === "IPv4" && !address.internal) return address.address;
    }
  }
  throw new Error("A non-loopback IPv4 address is required for LAN acceptance.");
}

async function openProjectSwitcher(page: Page) {
  const trigger = page.getByRole("button", {name: "Switch project", exact: true});
  if (!await trigger.isVisible()) {
    const navigation = page.getByRole("button", {name: "Open navigation"});
    if (await page.evaluate(() => matchMedia("(hover: hover) and (pointer: fine)").matches)) await navigation.hover();
    else await navigation.click();
    await expect(trigger).toBeVisible();
  }
  await trigger.click();
  return page.getByRole("dialog", {name: "Project switcher"});
}

test("project MCP catalog shows one Notebook grant and keeps shared servers across project switches", async ({page}, info) => {
  test.setTimeout(90_000);
  const core = await startApprovalCore(localNetworkIpv4(), "settings");
  await withOwnedCleanup(async () => {
    const projects = await (await core.api.get("engagements")).json() as Array<{id: string; name: string}>;
    const projectA = projects[0];
    expect(projectA).toBeTruthy();
    const created = await core.api.post("engagements", {data: {name: "Notebook Project B", status: "active"}});
    expect(created.ok(), await created.text()).toBe(true);
    const projectB = await created.json() as {id: string; name: string};
    expect((await core.api.get(`engagements/${projectB.id}/notebooks/status`)).ok()).toBe(true);
    const shared = await core.api.post("mcp-servers", {data: {
      name: "shared-fixture", transport: "stdio", command: "/bin/true", arguments: [],
      trusted_stdio: true, enabled: false,
    }});
    expect(shared.ok(), await shared.text()).toBe(true);
    const catalog = await (await core.api.get("mcp-servers")).json() as Array<{id: string; name: string; enabled: boolean; metadata?: {engagement_id?: string}}>;
    const notebooks = catalog.filter(item => item.name === "Notebook");
    expect(notebooks).toHaveLength(2);
    expect(new Set(notebooks.map(item => item.metadata?.engagement_id))).toEqual(new Set([projectA.id, projectB.id]));

    const pairing = await (await core.api.post(`http://127.0.0.1:${core.port}/api/v1/auth/pairings`, {data: {name: "MCP project catalog"}})).json() as {secret: string; confirmation_code: string};
    await page.addInitScript((id) => localStorage.setItem("nebula.engagement", id), projectA.id);
    await page.goto(`${core.origin}/#pair=${encodeURIComponent(pairing.secret)}&code=${encodeURIComponent(pairing.confirmation_code)}`);
    await page.getByLabel("Device name").fill("MCP project catalog");
    await page.getByRole("button", {name: "Pair device", exact: true}).click();
    await expect(page.locator(".app-shell")).toBeVisible({timeout: 20_000});
    await page.goto(`${core.origin}/settings#harness-settings`);
    const section = page.locator("#mcp-settings");
    const notebookCard = () => section.locator(".integration-card").filter({has: page.getByRole("heading", {name: "Notebook", exact: true})});
    await expect(notebookCard()).toHaveCount(1);
    await expect(section.getByRole("heading", {name: "shared-fixture"})).toBeVisible();
    await page.reload();
    await expect(notebookCard()).toHaveCount(1);

    const switcher = await openProjectSwitcher(page);
    await switcher.locator(".project-switcher-row > button:first-child").filter({hasText: projectB.name}).click();
    await expect(page).toHaveURL(new RegExp(`/projects/${projectB.id}`));
    await page.goto(`${core.origin}/settings#harness-settings`);
    await expect(notebookCard()).toHaveCount(1);
    await expect(section.getByRole("heading", {name: "shared-fixture"})).toBeVisible();
    await notebookCard().getByRole("button", {name: "Disable"}).click();
    await expect(notebookCard().getByRole("button", {name: "Enable"})).toBeVisible();
    const saved = await (await core.api.get("mcp-servers")).json() as typeof catalog;
    expect(saved.find(item => item.metadata?.engagement_id === projectA.id)?.enabled).toBe(true);
    expect(saved.find(item => item.metadata?.engagement_id === projectB.id)?.enabled).toBe(false);
    await page.reload();
    await expect(notebookCard().getByRole("button", {name: "Enable"})).toBeVisible();
    expect(await section.evaluate(element => element.scrollWidth - element.clientWidth)).toBeLessThanOrEqual(1);
    expect((await new AxeBuilder({page}).include("#mcp-settings").analyze()).violations).toEqual([]);
    await info.attach("project-mcp-catalog", {body: JSON.stringify({origin: core.origin, build: "ui/dist production", project: info.project.name, viewport: page.viewportSize(), grants: notebooks.map(item => item.id)}), contentType: "application/json"});
  }, [{name: "Core fixture", dispose: () => core.stop()}]);
});
