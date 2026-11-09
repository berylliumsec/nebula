import {createHash} from "node:crypto";
import {readFile} from "node:fs/promises";
import {expect, test, type APIRequestContext, type Page} from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";

async function controller(request: APIRequestContext) {
  const {token, control_origin: origin} = JSON.parse(await readFile(process.env.NEBULA_MODEL_TEST_AUTH_FILE!, "utf8"));
  const headers = {Authorization: `Bearer ${token}`};
  return {
    reset: async (cold = false) => {
      const response = await request.post(`${origin}/reset`, {headers, data: {cold}});
      expect(response.ok(), await response.text()).toBe(true);
      return response.json();
    },
    control: async (operation: string, values: Record<string, unknown> = {}) => {
      const response = await request.post(`${origin}/control`, {headers, data: {operation, ...values}});
      expect(response.ok(), await response.text()).toBe(true);
      return response.json();
    },
  };
}

async function pair(page: Page, request: APIRequestContext, fixture: {token: string; origin: string; index_sha256: string}) {
  const response = await request.get(fixture.origin + "/index.html");
  expect(createHash("sha256").update(await response.body()).digest("hex")).toBe(fixture.index_sha256);
  const pairing = await request.post(`http://127.0.0.1:${new URL(fixture.origin).port}/api/v1/auth/pairings`, {
    headers: {Authorization: `Bearer ${fixture.token}`}, data: {name: "Continuity acceptance device"},
  });
  expect(pairing.ok(), await pairing.text()).toBe(true);
  const invitation = await pairing.json();
  await page.goto(`/#pair=${encodeURIComponent(invitation.secret)}&code=${encodeURIComponent(invitation.confirmation_code)}`);
  await page.getByLabel("Device name").fill("Continuity acceptance device");
  await page.getByRole("button", {name: "Pair device", exact: true}).click();
  await expect(page.getByLabel("Device name")).toBeHidden({timeout: 30_000});
}

test("accepted provider work survives a lost response, API replacement and reload without another submission", async ({page, request}, info) => {
  const control = await controller(request);
  const fixture = await control.reset();
  await pair(page, request, fixture);
  await control.control("hold", {name: "hold-provider"});
  await page.addInitScript(() => {
    const original = globalThis.fetch;
    let dropped = false;
    globalThis.fetch = async (...args) => {
      const response = await original(...args);
      if (!dropped && response.url.includes("/chat/completions")) {
        dropped = true;
        await response.body?.cancel();
        throw new TypeError("Synthetic lost acceptance response");
      }
      return response;
    };
  });
  let submissions = 0;
  page.on("request", value => {if (value.method() === "POST" && value.url().includes("/chat/completions")) submissions++;});
  await page.goto(`/projects/continuity-project/workbench?view=chat&session=${fixture.session_id}`);
  const composer = page.locator(".chat-composer textarea");
  await expect(composer).toBeVisible();
  await composer.fill("Run this synthetic continuity check once.");
  await page.getByRole("button", {name: "Send message", exact: true}).click();
  await expect.poll(async () => (await control.control("inspect")).events.filter((item: {event: string}) => item.event === "provider-start").length).toBe(1);
  await control.control("api", {version: "acceptance-v2"});
  await control.control("release", {name: "hold-provider"});
  const retry = page.getByRole("button", {name: "Check saved requests", exact: true});
  if (await retry.isVisible()) await retry.click();
  await expect(page.locator(".chat-message.assistant .assistant-markdown").filter({hasText: "First last"})).toHaveText("First last", {timeout: 60_000});
  await composer.fill("Unsent draft remains on this device.");
  await page.reload();
  await expect(composer).toHaveValue("Unsent draft remains on this device.");
  await expect(page).toHaveURL(new RegExp(`session=${fixture.session_id}`));
  expect(submissions).toBe(1);
  expect((await control.control("inspect")).events.filter((item: {event: string}) => item.event === "provider-start")).toHaveLength(1);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true);
  await composer.focus();
  expect(await composer.evaluate(element => {
    const box = element.getBoundingClientRect(); const viewport = visualViewport;
    return !viewport || (box.bottom <= viewport.offsetTop + viewport.height + 1 && box.top >= viewport.offsetTop - 1);
  })).toBe(true);
  expect((await new AxeBuilder({page}).include(".chat-message.assistant").analyze()).violations).toEqual([]);
  await page.screenshot({path: info.outputPath("retained-chat.png"), fullPage: true});
});

test("a paired device repairs the Core reserve from production Settings before any API worker exists", async ({page, request}, info) => {
  const control = await controller(request);
  const fixture = await control.reset(true);
  await pair(page, request, fixture);
  const auth = {Authorization: `Bearer ${fixture.token}`};
  const health = await request.get(`${fixture.origin}/api/v1/health`, {headers: auth});
  expect((await health.json()).api_ready).toBe(false);
  expect((await control.control("inspect")).processes).toEqual([]);
  await page.goto("/settings#memory-budget-settings");
  const section = page.getByRole("region", {name: "Memory budgets", exact: true});
  const reserve = section.getByLabel(/Core and services reserve/);
  await expect(reserve).toHaveValue("2");
  await reserve.fill("4");
  await section.getByRole("button", {name: "Save memory budgets", exact: true}).click();
  await expect(section.getByText(/Saved and read back/)).toBeVisible();
  const observed = await request.get(`${fixture.origin}/api/v1/memory-budgets`, {headers: auth});
  expect((await observed.json()).policy.reserve_bytes).toBe(4 * 1024 ** 3);
  await control.control("execution", {version: "acceptance-v1", project_id: "continuity-project"});
  await control.control("api", {version: "acceptance-v1"});
  await page.reload();
  await expect(reserve).toHaveValue("4");
  const ready = await request.get(`${fixture.origin}/api/v1/health`, {headers: auth});
  expect((await ready.json()).api_ready).toBe(true);
  expect((await new AxeBuilder({page}).include("#memory-budget-settings").analyze()).violations).toEqual([]);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true);
  for (const item of await section.locator("input[type=number]:visible,button:visible").all()) {
    expect((await item.boundingBox())?.height).toBeGreaterThanOrEqual(44);
  }
  await page.screenshot({path: info.outputPath("host-only-memory-recovery.png"), fullPage: true});
});
