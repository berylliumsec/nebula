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

// Uses real Core reads and pairing; no provider turn or research work is run.
test("phone polling follows the visible drawer and selected title", async ({page, request}, info) => {
  const headers = {Authorization: "Bearer conversation-order-test-token"};
  const local = `http://127.0.0.1:${new URL(String(info.project.use.baseURL)).port}`;
  const build = await (await request.get("/web-build.json")).json();
  expect(build).toEqual(JSON.parse(await readFile("dist/web-build.json", "utf8")));
  expect(new URL(String(info.project.use.baseURL)).hostname).not.toBe("127.0.0.1");
  const seeded = await request.post(local + "/fixture/conversations?pending_title=true", {headers});
  expect(seeded.ok(), await seeded.text()).toBe(true);
  const {project, ids} = await seeded.json();
  const selected = ids["Paused chat"];
  await page.addInitScript(() => {
    Object.defineProperty(navigator, "userAgent", {get: () => "Mobile/15E148 NebulaShell/1.0"});
    // A desktop preference must not silently open the shell's polling scope.
    localStorage.setItem("nebula.conversations.open", "true");
  });
  const pairing = await (await request.post(local + "/api/v1/auth/pairings", {headers, data: {name: "Phone polling"}})).json();
  await page.goto(`/#pair=${encodeURIComponent(pairing.secret)}&code=${encodeURIComponent(pairing.confirmation_code)}`);
  await page.getByLabel("Device name").fill("Phone polling");
  await page.getByRole("button", {name: "Pair device", exact: true}).click();
  await expect(page.getByLabel("Device name")).toBeHidden({timeout: 20000});
  await page.clock.install();
  await page.goto(`/projects/${project}/workbench?view=chat&session=${selected}`);
  await expect(page.locator(".mobile-conversation-title")).toContainText("Paused chat");
  const drawer = page.getByRole("complementary", {name: "Conversations", exact: true});
  await expect(drawer).toBeHidden();
  await expect(page.getByRole("button", {name: "Send message", exact: true})).toBeVisible();
  const reads: string[] = [];
  const pending = new Set<object>();
  page.on("request", req => {
    const path = new URL(req.url()).pathname;
    if (req.method() === "GET" && path.startsWith("/api/") && !path.includes("/events")) {
      reads.push(path); pending.add(req);
    }
  });
  page.on("requestfinished", req => pending.delete(req));
  page.on("requestfailed", req => pending.delete(req));
  // Let each real transport settle before advancing its virtual deadlines.
  const tick = async (milliseconds: number) => {
    for (let elapsed = 0; elapsed < milliseconds; elapsed += 1000) {
      await page.clock.runFor(Math.min(1000, milliseconds - elapsed));
      await expect.poll(() => pending.size).toBe(0);
    }
  };
  const projectReads = () => reads.filter(path => path.endsWith("/chat-sessions") || path.endsWith("/chat/session-activity"));
  const selectedReads = () => reads.filter(path => path.endsWith(`/chat-sessions/${selected}`));
  const stateReads = () => reads.filter(path => path.endsWith(`/chat/sessions/${selected}/state`));
  await tick(12_000);
  await expect.poll(() => selectedReads().length).toBeGreaterThan(0);
  await expect.poll(() => stateReads().length).toBeGreaterThan(0);
  expect(projectReads()).toEqual([]);
  expect(reads.filter(path => /\/chat\/sessions\/[^/]+\/state$/.test(path)))
    .toEqual(stateReads());

  // Core owns the title; only this conversation is needed to update its header.
  const renamed = await request.patch(local + `/api/v1/chat-sessions/${selected}`, {headers, data: {title: "Named by Core"}});
  expect(renamed.ok(), await renamed.text()).toBe(true);
  await tick(6_000);
  await expect(page.locator(".mobile-conversation-title")).toContainText("Named by Core");
  const settledTitleReads = selectedReads().length;
  await tick(12_000);
  expect(selectedReads()).toHaveLength(settledTitleReads);
  expect(projectReads()).toEqual([]);

  await page.getByRole("button", {name: "Open conversations", exact: true}).click();
  await expect(drawer).toBeVisible();
  await expect.poll(() => projectReads().length).toBeGreaterThan(0);
  // Hide while the drawer is open: no new polls, then an immediate resync.
  await page.evaluate(() => {
    Object.defineProperty(document, "visibilityState", {configurable: true, get: () => "hidden"});
    Object.defineProperty(document, "hidden", {configurable: true, get: () => true});
    document.dispatchEvent(new Event("visibilitychange"));
  });
  const hiddenReads = reads.length;
  await tick(30_000);
  expect(reads).toHaveLength(hiddenReads);
  await page.evaluate(() => {
    Object.defineProperty(document, "visibilityState", {configurable: true, get: () => "visible"});
    Object.defineProperty(document, "hidden", {configurable: true, get: () => false});
    document.dispatchEvent(new Event("visibilitychange"));
  });
  await expect.poll(() => reads.length).toBeGreaterThan(hiddenReads);
  await drawer.getByRole("button", {name: "Hide conversations", exact: true}).click();
  await expect(drawer).toBeHidden();
  const closedReads = projectReads().length;
  await tick(30_000);
  expect(projectReads()).toHaveLength(closedReads);
  // Only the stream transport is inert; status invalidation still reads real
  // Core state. Stream events must not fan out to the closed project list.
  const activityBeforeStream = reads.filter(path => path.endsWith("/chat/session-activity")).length;
  const statesBeforeStream = stateReads().length;
  await page.route("**/api/v1/chat/completions", route => route.fulfill({
    status: 200, contentType: "text/event-stream", body: [
      {type: "status", payload: {phase: "working", detail: "Inert status fixture"}},
      {type: "error", detail: "Inert stream ended"},
    ].map(event => `data: ${JSON.stringify(event)}\n\n`).join(""),
  }));
  await page.locator(".chat-composer textarea").fill("Polling transport fixture");
  await page.getByRole("button", {name: "Send message", exact: true}).click();
  await expect(page.getByText("Inert stream ended", {exact: true}).first()).toBeVisible();
  await expect.poll(() => pending.size).toBe(0);
  await expect.poll(() => stateReads().length).toBeGreaterThan(statesBeforeStream);
  expect(reads.filter(path => path.endsWith("/chat/session-activity"))).toHaveLength(activityBeforeStream);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true);
  await page.screenshot({path: info.outputPath("phone-polling.png")});
});
