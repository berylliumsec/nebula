import {expect, test} from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";
import {readFile} from "node:fs/promises";

test("running conversations stay first through refresh and activity changes", async ({page, request}, info) => {
  const headers = {Authorization: "Bearer conversation-order-test-token"};
  const local = `http://127.0.0.1:${new URL(String(info.project.use.baseURL)).port}`;
  const build = await (await request.get("/web-build.json")).json();
  expect(build).toEqual(JSON.parse(await readFile("dist/web-build.json", "utf8")));
  expect(new URL(String(info.project.use.baseURL)).hostname).not.toBe("127.0.0.1");
  const seeded = await request.post(local + "/fixture/conversations", {headers});
  expect(seeded.ok(), await seeded.text()).toBe(true);
  const {project, ids} = await seeded.json();
  const pairing = await (await request.post(local + "/api/v1/auth/pairings", {headers, data: {name: "Sidebar ordering"}})).json();
  await page.goto(`/#pair=${encodeURIComponent(pairing.secret)}&code=${encodeURIComponent(pairing.confirmation_code)}`);
  await page.getByLabel("Device name").fill("Sidebar ordering");
  await page.getByRole("button", {name: "Pair device", exact: true}).click();
  await expect(page.getByLabel("Device name")).toBeHidden({timeout: 20000});
  await page.goto(`/projects/${project}/workbench?view=chat&session=${ids["Paused chat"]}`);
  const sidebar = page.getByRole("complementary", {name: "Conversations", exact: true});
  const showSidebar = async () => {
    await expect(page.locator(".session-layout")).toBeVisible();
    if (!await sidebar.isVisible()) await page.getByRole("button", {
      name: (page.viewportSize()?.width ?? 1440) <= 760 ? "Open conversations" : "Show conversations", exact: true,
    }).click();
    await expect(sidebar).toBeVisible();
  };
  const running = sidebar.getByRole("region", {name: "Working", exact: true});
  const checkOrder = async () => {
    await expect(sidebar.locator(".session-list-group h3").first()).toHaveText("Working");
    await expect(running.locator(".conversation-title-text")).toHaveText(["Pinned running chat", "Running chat"]);
    await expect(sidebar.getByRole("region", {name: "Needs you", exact: true})).toContainText("Blocked chat");
    await expect(sidebar.getByRole("region", {name: "Pinned", exact: true})).toContainText("Pinned idle chat");
  };
  await showSidebar();
  await checkOrder();
  await page.reload();
  await showSidebar();
  await checkOrder();
  const search = sidebar.getByRole("searchbox", {name: "Search conversations"});
  await search.fill("Running chat");
  await expect(sidebar.locator(".session-select")).toHaveCount(2);
  await search.fill("");
  await checkOrder();
  // An authoritative Core state change is reflected by the normal UI activity poll.
  const finished = await request.post(local + `/fixture/conversations/${ids["Pinned running chat"]}/finish`, {headers});
  expect(finished.ok()).toBe(true);
  await page.evaluate(() => window.dispatchEvent(new Event("focus")));
  await expect(running.locator(".conversation-title-text")).toHaveText(["Running chat"]);
  await expect(sidebar.getByRole("region", {name: "Pinned", exact: true}).locator(".conversation-title-text"))
    .toContainText(["Pinned running chat", "Pinned idle chat"]);
  await expect(sidebar.locator(".session-list-group h3").first()).toHaveText("Working");
  expect((await new AxeBuilder({page}).include(".session-list-group").analyze()).violations).toEqual([]);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true);
  await page.screenshot({path: info.outputPath("running-first.png")});
});
