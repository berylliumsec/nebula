import {expect, test, type APIRequestContext, type Page, type TestInfo} from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";

const headers = {Authorization: "Bearer model-test-token"};

/** Pair a device, open the Assistant browser and load the fixed fixture page in the session it has on screen. */
async function openViewer(page: Page, request: APIRequestContext, info: TestInfo) {
  const origin = String(info.project.use.baseURL);
  const loopback = `http://127.0.0.1:${new URL(origin).port}`;
  const project = await (await request.post("/api/v1/engagements", {headers, data: {name: `Assistant viewer ${info.project.name}`}})).json();
  const scope = await request.put(`/api/v1/engagements/${project.id}/scope`, {headers, data: {allowed_urls: [loopback + "/fixture-site/manual-input"], allowed_ports: [Number(new URL(origin).port)]}});
  expect(scope.ok()).toBeTruthy();
  const pairing = await (await request.post(loopback + "/api/v1/auth/pairings", {headers, data: {name: "Assistant viewer fixture"}})).json();
  await page.goto(`/#pair=${encodeURIComponent(pairing.secret)}&code=${encodeURIComponent(pairing.confirmation_code)}`);
  await page.getByLabel("Device name").fill("Assistant viewer fixture");
  await page.getByRole("button", {name: "Pair device", exact: true}).click();
  await expect(page.getByRole("button", {name: /Nebula Core (ready|degraded)/})).toBeVisible({timeout: 20000});
  // The viewer opens a browser session of its own for the draft conversation. Record the page streams it
  // connects so the checks below can prove they ran against the session and tab on screen.
  const streams: string[] = [];
  page.on("websocket", socket => {
    const path = new URL(socket.url()).pathname;
    if (/^\/api\/v1\/browser-companion\/[^/]+\/tabs\/[^/]+\/stream$/.test(path)) streams.push(path);
  });
  const opening = page.waitForResponse(response => response.request().method() === "POST" && new URL(response.url()).pathname === `/api/v1/engagements/${project.id}/browser-companion`);
  await page.goto(`/projects/${project.id}/workbench?view=browser&browserEngine=managed`);
  const opened = await opening;
  expect(opened.ok(), await opened.text()).toBeTruthy();
  const viewer = page.getByRole("region", {name: "Assistant browser", exact: true});
  const image = viewer.getByRole("img", {name: /Assistant browser page — read-only/});
  await expect(image).toBeVisible({timeout: 30000});
  const draft = new URL(opened.url()).searchParams.get("draft_id");
  expect(draft, "the viewer opens the browser of its draft conversation").toBeTruthy();
  const prepared = await request.post(`/fixture-site/prepare-viewer/${project.id}`, {params: {draft_id: draft!}});
  expect(prepared.ok(), await prepared.text()).toBeTruthy();
  const session = await prepared.json();
  const base = `/api/v1/browser-companion/${session.session_id}`;
  const onScreen = () => expect(streams.at(-1), "the checked session and tab are the ones the viewer shows").toBe(`${base}/tabs/${session.active_tab_id}/stream`);
  onScreen();
  const paused = async () => (await (await request.get(base + "/control", {headers})).json()).paused;
  return {loopback, viewer, image, session, base, onScreen, paused};
}

test("Assistant browser stays read-only through pointer, keyboard and reconnect", async ({page, request}, info) => {
  const {loopback, viewer, image, session, base, onScreen, paused} = await openViewer(page, request, info);
  await expect(viewer.getByText(/Read-only view/)).toBeVisible();
  await expect(page.getByLabel("Browser address")).toHaveAttribute("readonly", "");
  expect((await page.getByLabel("Browser address").boundingBox())!.width).toBeGreaterThanOrEqual(Math.min(120, (await viewer.boundingBox())!.width / 3));
  for (const name of ["Go", "New tab", "Close tab", "Pick element", "Select region"]) await expect(viewer.getByRole("button", {name, exact: true})).toHaveCount(0);
  const capture = async () => {
    const response = await request.post(base + "/operations", {headers, data: {operation: "capture", tab_id: session.active_tab_id}});
    expect(response.ok(), await response.text()).toBeTruthy();
    return (await response.json()).text;
  };
  const before = await capture();
  expect(before).toContain("Click fixture");
  // The viewer shows the fixture page before input is tried on it.
  await expect(page.getByLabel("Browser address")).toHaveValue(loopback + "/fixture-site/manual-input");
  const point = await image.evaluate((img: HTMLImageElement) => ({x: 120 * img.clientWidth / img.naturalWidth, y: 80 * img.clientHeight / img.naturalHeight}));
  if (info.project.use.isMobile) await image.tap({position: point}); else await image.click({position: point});
  await page.keyboard.type("Must not reach the page");
  await image.dispatchEvent("wheel", {deltaY: 100});
  expect(await capture()).toBe(before);
  expect((await request.post(base + "/operations", {headers, data: {operation: "navigate", url: loopback}})).status()).toBe(403);
  const closeCode = await page.evaluate(({base, tab}) => new Promise<number>((resolve, reject) => {
    const ws = new WebSocket(`${location.origin.replace(/^http/, "ws")}${base}/tabs/${tab}/stream`, "nebula.browser.v1");
    const timer = setTimeout(() => {ws.close(); reject(new Error("Viewer input was not rejected"));}, 10000);
    ws.onopen = () => ws.send(JSON.stringify({kind: "text", text: "Must not reach the page"}));
    ws.onclose = event => {clearTimeout(timer); resolve(event.code);};
  }), {base, tab: session.active_tab_id});
  expect(closeCode).toBe(4403);
  expect(await capture()).toBe(before);
  await viewer.getByRole("button", {name: "Reconnect view", exact: true}).click();
  await expect(image).toBeVisible();
  await expect(viewer.getByRole("status")).toContainText("Assistant paused");
  await viewer.getByRole("button", {name: "Resume assistant control", exact: true}).click();
  await expect(viewer.getByRole("button", {name: "Stop assistant control", exact: true})).toBeEnabled();
  expect(await paused()).toBe(false);
  await viewer.getByRole("button", {name: "Stop assistant control", exact: true}).click();
  // Stop is done only when Core has saved it and the viewer says so.
  await expect(viewer.getByRole("button", {name: "Resume assistant control", exact: true})).toBeEnabled();
  await expect(viewer.getByRole("status")).toContainText("Assistant paused");
  expect(await paused()).toBe(true);
  expect(await capture()).toBe(before);
  onScreen();
  expect((await new AxeBuilder({page}).include(".managed-assistant-browser").analyze()).violations).toEqual([]);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true);
  await page.screenshot({path: info.outputPath("assistant-browser-read-only.png")});
});

// WebKit service-worker-controlled requests bypass page.route() fault injection.
const refusedStop = test.extend({serviceWorkers: "block"});

refusedStop("Assistant browser keeps a Stop that Core did not save on screen until a retry saves it", async ({page, request}, info) => {
  const {viewer, base, onScreen, paused} = await openViewer(page, request, info);
  const control = viewer.getByRole("button", {name: /^(Stop|Resume) assistant control$/});
  await viewer.getByRole("button", {name: "Resume assistant control", exact: true}).click();
  await expect(viewer.getByRole("button", {name: "Stop assistant control", exact: true})).toBeEnabled();
  expect(await paused()).toBe(false);
  // Refuse the first Stop before it reaches Core, the way a stale session revision once did.
  let refused = 0;
  await page.route(url => url.pathname === `${base}/control` && url.searchParams.get("paused") === "true", async route => {
    if (route.request().method() !== "PUT" || refused) return route.fallback();
    refused += 1;
    await route.fulfill({status: 409, contentType: "application/json", body: JSON.stringify({detail: "revision conflict: expected 4, found 5", reason_code: "stale_state"})});
  });
  await viewer.getByRole("button", {name: "Stop assistant control", exact: true}).click();
  const notice = viewer.getByRole("alert").filter({hasText: "Assistant control was not stopped."});
  await expect(notice).toContainText("The Assistant may still act in this browser.");
  await expect(notice).toContainText("revision conflict: expected 4, found 5");
  // Nothing claims a stop that Core does not hold: the control still offers Stop and Core still grants control.
  await expect(viewer.getByRole("button", {name: "Stop assistant control", exact: true})).toBeEnabled();
  await expect(viewer.getByRole("status")).toContainText("Assistant control enabled");
  expect(refused).toBe(1);
  expect(await paused()).toBe(false);
  const retry = notice.getByRole("button", {name: "Retry stop", exact: true});
  const target = (await retry.boundingBox())!;
  expect(target.height).toBeGreaterThanOrEqual(44);
  expect(target.width).toBeGreaterThanOrEqual(44);
  expect((await new AxeBuilder({page}).include(".managed-assistant-browser").analyze()).violations).toEqual([]);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true);
  await page.screenshot({path: info.outputPath("assistant-browser-stop-not-saved.png")});
  if (info.project.use.isMobile) await retry.tap(); else {await retry.focus(); await page.keyboard.press("Enter");}
  await expect(notice).toHaveCount(0);
  await expect(viewer.getByRole("status")).toContainText("Assistant paused");
  await expect(control).toHaveAccessibleName("Resume assistant control");
  if (!info.project.use.isMobile) await expect(control).toBeFocused();
  expect(await paused()).toBe(true);
  // The viewer's next polls keep what Core saved.
  await page.waitForResponse(response => response.request().method() === "GET" && new URL(response.url()).pathname === `${base}/control`);
  await expect(viewer.getByRole("status")).toContainText("Assistant paused");
  await expect(notice).toHaveCount(0);
  onScreen();
});
