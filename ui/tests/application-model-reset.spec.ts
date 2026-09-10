import { expect, test } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";
import {modelAuthHeaders} from "./fixtures/model-auth";

test("scoped reset, cancellation, lost-response retry and fresh collection", async ({ page, request }, info) => {
  const headers = await modelAuthHeaders();
  const project = await (await request.post("/api/v1/engagements", { headers, data: { name: "Start over — " + "long project name ".repeat(6) } })).json();
  const base = `/api/v1/engagements/${project.id}/application-model`;
  const browser = (await (await request.get(`/api/v1/engagements/${project.id}/browser-workspace`, { headers })).json()).sessions[0];
  expect((await request.put(`/api/v1/browser-sessions/${browser.id}/tabs`, { headers, data: {
    expected_revision: browser.revision, tabs: [{ id: "fixture", url: "https://example.test/", title: "Fixture", position: 0 }], active_tab_id: "fixture", device_owner: "fixture",
  } })).ok()).toBeTruthy();
  async function capture() {
    const response = await request.post(`/api/v1/browser-sessions/${browser.id}/traffic`, { headers, data: { tab_id: "fixture", method: "GET", url: "https://example.test/", blocked: true, status_code: 403 } });
    expect(response.status(), await response.text()).toBe(201);
    return response.json();
  }
  await capture();
  async function object(revision: number, id: string) {
    const response = await request.post(base + "/transactions", { headers, data: { expected_revision: revision, idempotency_key: id, operations: [{ op: "put_object", id, label: id, properties: { purpose: { value: "Identify the entry point into the login workflow." } }, classification: { value: "Page" }, authentication_context: "anonymous" }] } });
    expect(response.ok(), await response.text()).toBeTruthy();
  }
  await object(0, "old-page");
  const origin = `http://127.0.0.1:${new URL(String(info.project.use.baseURL)).port}`;
  const pairing = await (await request.post(`${origin}/api/v1/auth/pairings`, { headers, data: { name: "Reset test" } })).json();
  await page.goto(`/#pair=${encodeURIComponent(pairing.secret)}&code=${encodeURIComponent(pairing.confirmation_code)}`);
  await page.getByLabel("Device name").fill("Reset test");
  await page.getByRole("button", { name: "Pair device", exact: true }).click();
  await expect(page.getByRole("button", { name: /Nebula Core (ready|degraded)/ })).toBeVisible({ timeout: 20_000 });
  await page.goto(`/projects/${project.id}/application-model?object=old-page`);
  await page.getByRole("button", { name: "Start over", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "Start this model over?" });
  await expect(dialog.getByText("1 object · 0 relationships · 1 capture")).toBeVisible();
  await expect(dialog.getByRole("button", { name: "Cancel", exact: true })).toBeFocused();
  const bounds = await dialog.boundingBox();
  expect(bounds!.x).toBeGreaterThanOrEqual(0);
  expect(bounds!.x + bounds!.width).toBeLessThanOrEqual(page.viewportSize()!.width);
  for (const button of await dialog.getByRole("button").all()) {
    expect((await button.boundingBox())!.height).toBeGreaterThanOrEqual(44);
  }
  const violations = (await new AxeBuilder({ page }).include('[role="dialog"]').analyze()).violations;
  expect(violations).toEqual([]);
  await page.screenshot({ path: info.outputPath("reset-confirmation.png") });
  const portrait = page.viewportSize()!;
  await page.setViewportSize({ width: 844, height: 320 });
  await expect(dialog).toBeVisible();
  const landscape = await dialog.boundingBox();
  expect(landscape!.height).toBeLessThanOrEqual(320);
  await dialog.getByRole("button", { name: "Cancel", exact: true }).scrollIntoViewIfNeeded();
  await expect(dialog.getByRole("button", { name: "Cancel", exact: true })).toBeInViewport();
  await page.setViewportSize(portrait);
  await dialog.getByRole("button", { name: "Cancel", exact: true }).click();
  await expect(page.getByRole("button", { name: "Start over", exact: true })).toBeFocused();
  expect((await (await request.get(base + "/reset-preview", { headers })).json()).objects).toBe(1);
  let lost = false;
  await page.route("**/application-model/reset", async route => {
    if (!lost) { lost = true; const response = await route.fetch(); expect(response.ok()).toBeTruthy(); await route.abort(); }
    else await route.continue();
  });
  await page.getByRole("button", { name: "Start over", exact: true }).click();
  await dialog.getByRole("button", { name: "Clear and start over", exact: true }).click();
  await expect(dialog.getByRole("button", { name: "Retry clear", exact: true })).toBeVisible();
  const fresh = await capture();
  await dialog.getByRole("button", { name: "Retry clear", exact: true }).click();
  await expect(page.getByRole("status").filter({ hasText: "Model and browser captures cleared" })).toBeVisible();
  expect(new URL(page.url()).search).toBe("");
  const cleared = await (await request.get(base + "/reset-preview", { headers })).json();
  expect(cleared).toMatchObject({ revision: 2, objects: 0, captures: 1 });
  expect((await (await request.get(base + "/evidence", { headers })).json()).evidence.map((e: { id: string }) => e.id)).toContain(fresh.id);
  await object(2, "fresh-page");
  await page.reload();
  await expect(page.getByText("Revision 3", { exact: true })).toBeVisible();
  await page.locator(".am-category summary").click();
  await expect(page.locator(".am-outline").getByText("fresh-page", { exact: true })).toBeVisible();
  await expect(page.locator(".am-outline").getByText("old-page", { exact: true })).toHaveCount(0);
});
