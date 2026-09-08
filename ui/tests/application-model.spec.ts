import { expect, test } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";

test("recorded browser state and solver remain selectable after reload", async ({ page, request }, info) => {
  const headers = { Authorization: "Bearer model-test-token" };
  const projectResponse = await request.post("/api/v1/engagements", { headers, data: { name: `Model ${info.project.name}` } });
  expect(projectResponse.ok()).toBeTruthy();
  const project = await projectResponse.json();
  const bw = await (await request.get(`/api/v1/engagements/${project.id}/browser-workspace`, { headers })).json();
  const browser = bw.sessions[0];
  const base = `/api/v1/engagements/${project.id}/application-model`;
  // Pairing issuance is intentionally localhost-only, including the LAN run.
  const pairingOrigin = `http://127.0.0.1:${new URL(String(info.project.use.baseURL)).port}`;
  const pairingResponse = await request.post(`${pairingOrigin}/api/v1/auth/pairings`, { headers, data: { name: "Model acceptance" } });
  expect(pairingResponse.ok()).toBeTruthy();
  const pairing = await pairingResponse.json();
  await page.goto(`/#pair=${encodeURIComponent(pairing.secret)}&code=${encodeURIComponent(pairing.confirmation_code)}`);
  await page.getByLabel("Device name").fill("Model acceptance");
  await page.getByRole("button", { name: "Pair device" }).click();
  await expect(page.getByRole("button", { name: "Pair device", exact: true })).not.toBeVisible();
  await expect(page.getByRole("button", { name: /Nebula Core (ready|degraded)/ })).toBeVisible();
  await page.goto(`/projects/${project.id}/application-model`);
  await expect(page.getByRole("heading", { name: "Application model", exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Create collection" }).click();
  await expect.poll(() => new URL(page.url()).searchParams.get("collection")).toBeTruthy();
  const collection = new URL(page.url()).searchParams.get("collection")!;
  // A committed traffic record exercises the production capture and
  // projection seam without claiming native browser process coverage.
  const sync = await request.put(`/api/v1/browser-sessions/${browser.id}/tabs`, { headers, data: { expected_revision: browser.revision, tabs: [{ id: "fixture", url: "https://example.test/", title: "Fixture", position: 0 }], active_tab_id: "fixture", device_owner: "fixture-desktop" } });
  expect(sync.ok()).toBeTruthy();
  // Blocked traffic remains a recorded observation even without target grants.
  const capture = await request.post(`/api/v1/browser-sessions/${browser.id}/traffic`, { headers, data: { tab_id: "fixture", method: "GET", url: "https://example.test/", blocked: true, status_code: 403 } });
  expect(capture.ok()).toBeTruthy();
  await expect.poll(async () => (await (await request.get(`${base}/sessions/${collection}/workspace`, { headers })).json()).states.length).toBe(1);
  await expect(page.getByLabel("Knowledge state")).not.toHaveValue("");
  await page.getByRole("button", { name: "Solver", exact: true }).click();
  const options = page.getByLabel("Property").locator("option");
  const statusOption = await options.evaluateAll(nodes => nodes.map(n => ({ text: n.textContent, value: (n as HTMLOptionElement).value })).find(n => n.text?.startsWith("status_code")));
  expect(statusOption).toBeTruthy();
  await page.getByLabel("Property").selectOption(statusOption!.value);
  await page.getByLabel("Value", { exact: true }).fill("403");
  await page.getByRole("button", { name: "Check consistency" }).click();
  await expect(page.getByRole("heading", { name: "SAT", exact: true })).toBeVisible();
  await page.reload();
  await expect(page.getByRole("heading", { name: "SAT", exact: true })).toBeVisible();
  await expect(page.getByLabel("Collection")).toHaveValue(collection);
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth + 1);
  expect(overflow).toBe(false);
  const accessibility = await new AxeBuilder({ page }).include(".application-model-page").analyze();
  expect(accessibility.violations).toEqual([]);
  expect(await page.locator(".project-tabs button").evaluateAll(buttons => buttons.every(button => button.scrollWidth <= button.clientWidth + 1))).toBe(true);
  await page.getByRole("heading", { name: "SAT", exact: true }).scrollIntoViewIfNeeded();
  await page.screenshot({ path: info.outputPath("application-model-solver.png"), fullPage: true });
  await page.getByRole("button", { name: "Model map", exact: true }).click();
  await expect(page.getByLabel("Application model graph")).toBeVisible();
  const objectNode = page.locator("button.model-node.object").first();
  await objectNode.click();
  await expect(page.getByLabel("Graph selection inspector").getByText("status_code", { exact: true })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1)).toBe(true);
  await page.screenshot({ path: info.outputPath("application-model-map.png"), fullPage: true });
  await page.getByRole("button", { name: "Pause", exact: true }).click();
  await expect(page.getByRole("button", { name: "Resume", exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Resume", exact: true }).click();
  page.once("dialog", dialog => dialog.accept());
  await page.getByRole("button", { name: "Delete collection" }).click();
  await expect.poll(() => new URL(page.url()).searchParams.get("collection")).toBeNull();
  const retained = await request.get(`/api/v1/engagements/${project.id}/browser-workspace`, { headers });
  expect((await retained.json()).traffic.length).toBe(1);
});
