import {expect, test} from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";

test("host CPU budget saves, reloads, handles conflicts and distinguishes applied from inactive", async ({page, request}, info) => {
  const origin = String(info.project.use.baseURL);
  const headers = {Authorization: "Bearer cpu-budget-test-token"};
  const fixture = await request.post(`${origin}/__cpu_fixture/state`, {headers, data: {reset: true}});
  expect(fixture.ok()).toBeTruthy();
  const build = await fixture.json();
  const localOrigin = `http://127.0.0.1:${new URL(origin).port}`;
  const invitation = await request.post(`${localOrigin}/api/v1/auth/pairings`, {headers, data: {name: "Synthetic CPU settings device"}});
  expect(invitation.ok()).toBeTruthy();
  const pairing = await invitation.json();
  await page.goto(`/#pair=${encodeURIComponent(pairing.secret)}&code=${encodeURIComponent(pairing.confirmation_code)}`);
  await page.getByLabel("Device name").fill("Synthetic CPU settings device");
  await page.getByRole("button", {name: "Pair device", exact: true}).click();
  await expect(page.getByRole("heading", {name: "Pair this device", exact: true})).not.toBeVisible({timeout: 20000});
  await page.goto("/settings#setup-settings");
  const section = page.getByRole("region", {name: "CPU budget", exact: true});
  await section.scrollIntoViewIfNeeded();
  const mode = section.getByLabel("CPU budget mode");
  const save = section.getByRole("button", {name: "Save CPU budget"});
  await expect(section.getByText("Applied: 2 CPUs.", {exact: true})).toBeVisible();
  await mode.selectOption("custom");
  const limit = section.getByLabel("Maximum background CPUs");
  await limit.fill("5");
  await expect(save).toBeDisabled();
  await expect(section.getByRole("alert")).toContainText("up to 4");
  await limit.fill("3"); await save.click();
  await expect(section.getByText("Saved and applied: 3 CPUs.", {exact: true})).toBeVisible();
  await page.reload();
  await expect(mode).toHaveValue("custom"); await expect(limit).toHaveValue("3");
  await expect(section.getByText("Applied: 3 CPUs.", {exact: true})).toBeVisible();

  // An independent authenticated client changes the same host's revision.
  const current = await (await request.get(`${origin}/api/v1/host-cpu-budget`, {headers})).json();
  const changed = await request.put(`${origin}/api/v1/host-cpu-budget`, {headers, data: {host_id: current.host_id, expected_revision: current.revision, cpu_limit: 1}});
  expect(changed.ok()).toBeTruthy();
  await limit.fill("2.5"); await save.click();
  await expect(section.getByRole("alert")).toContainText(/changed|409/);
  await expect(limit).toHaveValue("2.5");
  await section.getByRole("button", {name: "Reload CPU budget"}).click();
  await expect(limit).toHaveValue("1");
  await mode.selectOption("automatic"); await save.click();
  await expect(section.getByText("Saved and applied: 2 CPUs.", {exact: true})).toBeVisible();

  await request.post(`${origin}/__cpu_fixture/state`, {headers, data: {fail: true}});
  await mode.selectOption("custom"); await limit.fill("3"); await save.click();
  await expect(section.getByText(/executor did not apply/).first()).toBeVisible();
  expect((await (await request.get(`${origin}/api/v1/host-cpu-budget`, {headers})).json()).apply_state).toBe("failed");
  await request.post(`${origin}/__cpu_fixture/state`, {headers, data: {fail: false}});
  await save.click();
  await expect(section.getByText("Saved and applied: 3 CPUs.", {exact: true})).toBeVisible();

  await request.post(`${origin}/__cpu_fixture/state`, {headers, data: {inactive: true}});
  await section.getByRole("button", {name: "Reload CPU budget"}).click();
  await expect(section.getByText(/shared background executor is inactive/)).toBeVisible();
  await mode.selectOption("automatic"); await save.click();
  const observed = await (await request.get(`${origin}/api/v1/host-cpu-budget`, {headers})).json();
  expect(observed.cpu_limit).toBeNull(); expect(observed.apply_state).toBe("inactive");
  await page.reload();
  await expect(mode).toHaveValue("automatic");
  await expect(section.getByText(/shared background executor is inactive/)).toBeVisible();
  await section.scrollIntoViewIfNeeded();
  expect((await new AxeBuilder({page}).include(".host-cpu-settings").analyze()).violations).toEqual([]);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true);
  for (const control of await section.locator("input, select, button").all()) {
    const box = await control.boundingBox();
    expect(box?.height).toBeGreaterThanOrEqual(44);
  }
  await page.screenshot({path: info.outputPath("host-cpu-budget.png"), fullPage: true});
  await info.attach("candidate-build", {body: JSON.stringify({origin, bundleSha256: build.bundle_sha256, project: info.project.name, viewport: page.viewportSize(), policy: observed}), contentType: "application/json"});
});
