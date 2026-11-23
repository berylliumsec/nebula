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
    if (!await sidebar.isVisible()) {
      const open = page.getByRole("button", {name: "Open conversations", exact: true});
      await (await open.isVisible() ? open : page.getByRole("button", {name: "Show conversations", exact: true})).click();
    }
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

test("parent project shows child conversations and opens their owned chat", async ({page, request}, info) => {
  const headers = {Authorization: "Bearer conversation-order-test-token"};
  const local = `http://127.0.0.1:${new URL(String(info.project.use.baseURL)).port}`;
  const build = await (await request.get("/web-build.json")).json();
  expect(build).toEqual(JSON.parse(await readFile("dist/web-build.json", "utf8")));
  expect(new URL(String(info.project.use.baseURL)).hostname).not.toBe("127.0.0.1");
  const seeded = await request.post(local + "/fixture/child-project-conversations", {headers});
  expect(seeded.ok(), await seeded.text()).toBe(true);
  const {parent, child, chat, waiting, approvals} = await seeded.json();
  const pairing = await (await request.post(local + "/api/v1/auth/pairings", {headers, data: {name: "Child project chats"}})).json();
  await page.goto(`/#pair=${encodeURIComponent(pairing.secret)}&code=${encodeURIComponent(pairing.confirmation_code)}`);
  await page.getByLabel("Device name").fill("Child project chats");
  await page.getByRole("button", {name: "Pair device", exact: true}).click();
  await expect(page.getByLabel("Device name")).toBeHidden({timeout: 20000});
  const reads: string[] = [];
  page.on("request", request => {
    const url = new URL(request.url());
    if (url.pathname.startsWith("/api/v1/chat")) reads.push(url.pathname + url.search);
  });
  await page.goto(`/projects/${parent}/workbench?view=chat`);
  const sidebar = page.getByRole("complementary", {name: "Conversations", exact: true});
  const showSidebar = async () => {
    await expect(page.locator(".session-layout")).toBeVisible();
    if (!await sidebar.isVisible()) {
      const open = page.getByRole("button", {name: "Open conversations", exact: true});
      await (await open.isVisible() ? open : page.getByRole("button", {name: "Show conversations", exact: true})).click();
    }
    await expect(sidebar).toBeVisible();
  };
  await showSidebar();
  const search = async (text: string) => {
    const box = sidebar.getByRole("searchbox", {name: "Search conversations"});
    await box.fill(text);
    // A refilled field keeps its old text selected; collapse the selection so
    // the phone selection toolbar does not cover the list.
    await box.press("End");
  };
  const section = sidebar.getByRole("region", {name: "Subproject conversations"});
  await expect(section).toBeVisible();
  // One list: no per-project folders, and no empty ones.
  await expect(section.getByText("No saved chats")).toHaveCount(0);
  await expect(section.getByRole("link").first()).toHaveAccessibleName("Open Approval needed chat in Approvals child, Work stage Recon, blocked");
  await expect(section.getByRole("group", {name: "Needs you"})).toContainText("Waiting for you");
  // The subproject has no stages of its own: its chat shows the parent's stage, inside its row on one line.
  const staged = section.getByRole("link").first();
  const stage = staged.locator(".chat-work-stage");
  await expect(stage).toHaveAttribute("data-stage-key", "recon");
  await expect(stage).toHaveAttribute("data-state", "blocked");
  await expect(stage.locator(".chat-work-stage-text")).toHaveText("Recon");
  const [row, label] = await Promise.all([staged.boundingBox(), stage.boundingBox()]);
  expect(row && label).toBeTruthy();
  expect(label!.x).toBeGreaterThanOrEqual(row!.x - 1);
  expect(label!.x + label!.width).toBeLessThanOrEqual(row!.x + row!.width + 1);
  expect(label!.height).toBeLessThan(24);
  await expect(section.getByRole("link")).toHaveCount(25);
  // The oldest chat is not on the first page by recency; Show more reveals it.
  const link = section.getByRole("link", {name: "Open Example child chat in Child project"});
  await expect(link).toHaveCount(0);
  await section.getByRole("button", {name: "Show more chats"}).click();
  await expect(link).toBeVisible();
  // A chat with no assigned Work item shows no stage.
  await expect(link).not.toHaveClass(/has-stage/);
  await expect(link.locator(".chat-work-stage")).toHaveCount(0);
  await expect(section.getByRole("button", {name: "Show more chats"})).toHaveCount(0);
  const linkedReads = reads.filter(path => path.includes("/linked-chats"));
  expect(linkedReads.length).toBeGreaterThanOrEqual(2);
  expect(linkedReads.length).toBeLessThanOrEqual(4);
  expect(reads.filter(path => path.includes("chat-sessions?engagement_id=") && !path.includes(parent))).toEqual([]);
  expect(reads.filter(path => path.includes("session-activity") && !path.includes(parent))).toEqual([]);
  await search("Example child");
  await expect(link).toBeVisible();
  await expect(section.getByRole("link")).toHaveCount(1);
  await search("no such chat anywhere");
  await expect(section).toHaveCount(0);
  await expect(sidebar.getByText("No conversations match “no such chat anywhere”.")).toBeVisible();
  await search("Example child");
  await expect(link).toBeVisible();
  await link.click();
  await expect(page).toHaveURL(new RegExp(`/projects/${child}/workbench\\?view=chat&session=${chat}$`));
  await showSidebar();
  await expect(sidebar.locator(".session-list-item.active .conversation-title-text")).toHaveText("Example child chat");
  await page.reload();
  await showSidebar();
  await expect(sidebar.locator(".session-list-item.active .conversation-title-text")).toHaveText("Example child chat");
  await page.goBack();
  await showSidebar();
  await expect(section).toBeVisible();
  await section.getByRole("link", {name: "Open Approval needed chat in Approvals child, Work stage Recon, blocked"}).click();
  await expect(page).toHaveURL(new RegExp(`/projects/${approvals}/workbench\\?view=chat&session=${waiting}$`));
  await page.goBack();
  await showSidebar();
  await expect(section).toBeVisible();
  expect((await new AxeBuilder({page}).include(".child-project-conversations").analyze()).violations).toEqual([]);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true);
});
