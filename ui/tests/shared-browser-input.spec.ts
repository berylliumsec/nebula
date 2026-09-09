import { expect, test } from "@playwright/test";

test("operator clicks and types in shared Chromium before and after reconnect", async ({ page, request }, info) => {
  const headers = { Authorization: "Bearer model-test-token" };
  const created = await request.post("/api/v1/engagements", { headers, data: { name: `Manual browser ${info.project.name}` } });
  expect(created.ok()).toBeTruthy();
  const project = await created.json();
  const origin = String(info.project.use.baseURL);
  const url = `${origin}/fixture-site/manual-input`;
  const scope = await request.put(`/api/v1/engagements/${project.id}/scope`, {
    headers, data: { allowed_urls: [url], allowed_ports: [Number(new URL(url).port)] },
  });
  expect(scope.ok()).toBeTruthy();
  const pairing = await (await request.post(`http://127.0.0.1:${new URL(origin).port}/api/v1/auth/pairings`, {
    headers, data: { name: "Manual input test" },
  })).json();
  await page.goto(`/#pair=${encodeURIComponent(pairing.secret)}&code=${encodeURIComponent(pairing.confirmation_code)}`);
  await page.getByLabel("Device name").fill("Manual input test");
  await page.getByRole("button", { name: "Pair device", exact: true }).click();
  await expect(page.getByRole("button", { name: /Nebula Core (ready|degraded)/ })).toBeVisible({ timeout: 20_000 });
  await page.goto(`/projects/${project.id}/workbench?view=browser&browserEngine=managed`);
  const address = page.getByRole("textbox", { name: "Browser address" });
  await expect(address).toBeVisible();
  await expect(page.getByRole("button", { name: "Go", exact: true })).toBeEnabled({ timeout: 30_000 });
  await address.fill(url);
  await page.getByRole("button", { name: "Go", exact: true }).click();
  const screen = page.locator(".managed-browser-screen img");
  await expect(screen).toBeVisible({ timeout: 30_000 });
  const session = await (await request.post(`/api/v1/engagements/${project.id}/browser-companion`, { headers })).json();
  const capture = async () => {
    const response = await request.post(`/api/v1/browser-companion/${session.session_id}/operations`, {
      headers, data: { operation: "capture", tab_id: session.active_tab_id ?? session.tabs[0].id },
    });
    if (!response.ok()) return `Capture pending: ${response.status()}`;
    return (await response.json()).text as string;
  };
  await expect.poll(capture).toContain("Click fixture");
  const clickAt = async (x: number, y: number) => {
    const position = await screen.evaluate((image: HTMLImageElement, point) => {
      const bounds = image.getBoundingClientRect();
      return { x: point.x * bounds.width / image.naturalWidth, y: point.y * bounds.height / image.naturalHeight };
    }, { x, y });
    if (info.project.use.isMobile) await screen.tap({ position });
    else await screen.click({ position });
  };
  await clickAt(120, 80);
  await expect.poll(capture).toContain("Clicked 1");
  await clickAt(100, 180);
  await page.keyboard.type("Manual input works");
  await expect.poll(capture).toContain("Manual input works");
  await page.getByRole("button", { name: "Reconnect view", exact: true }).click();
  await expect(page.getByRole("button", { name: "Go", exact: true })).toBeEnabled();
  await expect(page.locator(".managed-assistant-browser").getByRole("status")).toContainText("Shared Chromium");
  await expect(screen).toBeVisible();
  await clickAt(120, 80);
  await expect.poll(capture).toContain("Clicked 2");
  await page.screenshot({ path: info.outputPath("shared-browser-manual-input.png") });
});
