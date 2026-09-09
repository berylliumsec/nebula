import { expect, test } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";

test("archived project deletion confirms, persists and preserves its linked folder", async ({ page, request }, info) => {
  const api = `http://127.0.0.1:${new URL(String(info.project.use.baseURL)).port}/api/v1`;
  const headers = { Authorization: "Bearer model-test-token" };
  const linked = await (await request.get(`${api}/engagements/linked-model-regression`, { headers })).json();
  const name = `Archived project with retained host files ${info.project.name}`;
  const created = await request.post(`${api}/engagements`, { headers, data: { name, status: "archived", workspace_path: linked.workspace_path } });
  expect(created.ok(), await created.text()).toBe(true);
  const project = await created.json();
  const unusedSession = await request.post(`${api.replace("/api/v1", "")}/fixture-site/unused-harness-session/${project.id}`);
  expect(unusedSession.ok(), await unusedSession.text()).toBe(true);
  const child = await request.post(`${api}/assets`, { headers, data: { engagement_id: project.id, name: "Fixture asset" } });
  expect(child.ok(), await child.text()).toBe(true);
  const pair = await (await request.post(`${api}/auth/pairings`, { headers, data: { name: "Archived deletion test" } })).json();
  await page.goto(`/#pair=${encodeURIComponent(pair.secret)}&code=${encodeURIComponent(pair.confirmation_code)}`);
  await page.getByLabel("Device name").fill("Archived deletion test");
  await page.getByRole("button", { name: "Pair device", exact: true }).click();
  await expect(page.getByRole("button", { name: /Nebula Core (ready|degraded)/ })).toBeVisible({ timeout: 20000 });
  await page.goto("/projects/linked-model-regression/workbench?view=files");
  const openArchives = async () => {
    const sidebar = page.getByRole("button", { name: "Show sidebar", exact: true });
    const switcher = page.getByRole("button", { name: "Switch project", exact: true });
    await expect(sidebar.or(switcher).filter({ visible: true }).first()).toBeVisible();
    if (await sidebar.isVisible()) await sidebar.click();
    await switcher.click();
    await page.getByRole("button", { name: /^Archived projects/ }).click();
  };
  await openArchives();
  await page.getByRole("button", { name: `Delete project ${name}`, exact: true }).click();
  const dialog = page.getByRole("dialog", { name: `Permanently delete ${name}?`, exact: true });
  await expect(dialog).toContainText("will not be deleted");
  await dialog.getByRole("button", { name: "Cancel", exact: true }).click();
  expect((await request.get(`${api}/engagements/${project.id}`, { headers })).status()).toBe(200);
  await page.getByRole("button", { name: `Delete project ${name}`, exact: true }).click();
  await expect(dialog).toHaveCSS("opacity", "1");
  await expect(dialog).toHaveCSS("filter", /^(none|blur\(0px\))$/);
  expect((await new AxeBuilder({ page }).include(".confirmation-dialog").analyze()).violations).toEqual([]);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true);
  await page.screenshot({ path: info.outputPath("delete-confirmation.png"), fullPage: true });
  // Verify actionable failure stays in place; persistence below uses the real endpoint.
  await page.route(`**/api/v1/engagements/${project.id}`, async route => {
    if (route.request().method() === "DELETE") await route.fulfill({ status: 409, contentType: "application/json", body: JSON.stringify({ detail: "Project has queued follow-ups. Restore it and clear the queue before deleting." }) });
    else await route.continue();
  });
  await dialog.getByRole("button", { name: "Delete permanently", exact: true }).click();
  await expect(page.getByRole("alert").filter({ hasText: "clear the queue" })).toBeVisible();
  await page.unroute(`**/api/v1/engagements/${project.id}`);
  await page.getByRole("button", { name: `Delete project ${name}`, exact: true }).click();
  await dialog.getByRole("button", { name: "Delete permanently", exact: true }).click();
  await expect(page.getByRole("status").filter({ hasText: "Project deleted. Workspace files were kept." })).toBeVisible();
  await expect(page.getByRole("button", { name: `Delete project ${name}`, exact: true })).toHaveCount(0);
  expect((await request.get(`${api}/engagements/${project.id}`, { headers })).status()).toBe(404);
  await page.reload();
  await openArchives();
  await expect(page.getByRole("button", { name: `Delete project ${name}`, exact: true })).toHaveCount(0);
  // A second project linked to the same folder still reads the original file.
  const read = await request.get(`${api}/engagements/linked-model-regression/workspace/preview?path=linked-inspection.txt`, { headers });
  expect(read.ok(), await read.text()).toBe(true);
  expect(await read.text()).toContain("Linked folder inspection remains visible.");
});
