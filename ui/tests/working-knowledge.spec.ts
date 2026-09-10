import {expect, test} from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";
import {modelAuthHeaders} from "./fixtures/model-auth";

test("working knowledge survives retired model links, reload and project navigation", async ({page, request}, info) => {
  const headers = await modelAuthHeaders();
  const created = await request.post("/api/v1/engagements", {headers, data: {name: `Working knowledge ${info.project.name}`}});
  expect(created.ok()).toBeTruthy();
  const project = await created.json();
  const base = `/api/v1/engagements/${project.id}/application-model`;
  const seeded = await request.post(base + "/transactions", {headers, data: {
    expected_revision: 0, idempotency_key: "working-knowledge-fixture", operations: [{op: "put_object", id: "login-flow", label: "Local login workflow", classification: {value: "Page"}, authentication_context: "anonymous", properties: {purpose: {value: "Explain the local fixture login entry point."}}}],
  }});
  expect(seeded.ok(), await seeded.text()).toBeTruthy();
  const before = await (await request.get(base + "/graph", {headers})).json();
  const origin = `http://127.0.0.1:${new URL(String(info.project.use.baseURL)).port}`;
  const pairing = await (await request.post(origin + "/api/v1/auth/pairings", {headers, data: {name: "Working knowledge fixture"}})).json();
  await page.goto(`/#pair=${encodeURIComponent(pairing.secret)}&code=${encodeURIComponent(pairing.confirmation_code)}`);
  await page.getByLabel("Device name").fill("Working knowledge fixture");
  await page.getByRole("button", {name: "Pair device", exact: true}).click();
  await expect(page.getByRole("button", {name: /Nebula Core (ready|degraded)/})).toBeVisible({timeout: 20000});
  for (const old of [`/projects/${project.id}/application-model?object=login-flow&edge=old`, `/projects/${project.id}/workbench?view=model`]) {
    await page.goto(old);
    await expect(page).toHaveURL(new RegExp(`/projects/${project.id}/workbench\\?view=chat$`));
    await expect(page.getByRole("button", {name: "Start new chat", exact: true})).toBeVisible();
    await expect(page.getByRole("tab", {name: "Application model", exact: true})).toHaveCount(0);
    await expect(page.locator(".application-model-page")).toHaveCount(0);
  }
  await page.reload();
  await expect(page.getByRole("button", {name: "Start new chat", exact: true})).toBeVisible();
  const more = page.getByRole("button", {name: "More workbench views", exact: true});
  if (await more.isVisible()) {
    await more.click();
    await expect(page.getByRole("button", {name: /Inspect recorded application states/})).toHaveCount(0);
    await more.click();
  }
  const projectLink = page.getByRole("link", {name: "Project", exact: true});
  if (!await projectLink.isVisible()) await page.getByRole("button", {name: "Show sidebar", exact: true}).click();
  await projectLink.click();
  await expect(page.getByRole("navigation", {name: "Project sections"})).toBeVisible();
  await expect(page.getByRole("button", {name: "Application model", exact: true})).toHaveCount(0);
  await page.goBack();
  await expect(page).toHaveURL(new RegExp(`/projects/${project.id}/workbench\\?view=chat$`));
  expect(await (await request.get(base + "/graph", {headers})).json()).toEqual(before);
  expect((await new AxeBuilder({page}).include("main").analyze()).violations).toEqual([]);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true);
  await page.screenshot({path: info.outputPath("working-knowledge-assistant.png")});
});
