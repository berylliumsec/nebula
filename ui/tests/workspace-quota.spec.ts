import { expect, test } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";

test("large linked workspace accepts upload and reload without quota rejection", async ({ page, request }, info) => {
  const origin = `http://127.0.0.1:${new URL(String(info.project.use.baseURL)).port}`;
  const pair = await (await request.post(`${origin}/api/v1/auth/pairings`, { headers: { Authorization: "Bearer model-test-token" }, data: { name: "Large workspace test" } })).json();
  await page.goto(`/#pair=${encodeURIComponent(pair.secret)}&code=${encodeURIComponent(pair.confirmation_code)}`);
  await page.getByLabel("Device name").fill("Large workspace test");
  await page.getByRole("button", { name: "Pair device", exact: true }).click();
  await expect(page.getByRole("button", { name: /Nebula Core (ready|degraded)/ })).toBeVisible({ timeout: 20000 });
  await page.goto("/projects/linked-model-regression/workbench?view=files");
  await expect(page.locator(".workspace-reset-summary")).toContainText("Linked folder");
  const filename = `upload-${info.project.name}.txt`;
  await page.getByLabel("Choose workspace file").setInputFiles({ name: filename, mimeType: "text/plain", buffer: Buffer.from("Saved in the original large workspace.") });
  await page.getByRole("button", { name: new RegExp(filename) }).click();
  await expect(page.locator(".workspace-file-preview pre")).toContainText("Saved in the original large workspace.");
  await page.reload();
  await page.getByRole("button", { name: new RegExp(filename) }).click();
  await expect(page.locator(".workspace-file-preview pre")).toContainText("Saved in the original large workspace.");
  await expect(page.getByText(/50,000 entries|5 GiB allocated|1 GiB per file/)).toHaveCount(0);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true);
  expect((await new AxeBuilder({ page }).include(".workspace-browser").analyze()).violations).toEqual([]);
  await page.screenshot({ path: info.outputPath("large-workspace.png"), fullPage: true });
});
