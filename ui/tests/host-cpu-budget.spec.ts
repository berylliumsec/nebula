import {expect, test} from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";

test("host CPU workers activate real processes, render, drain, retry and survive reload", async ({page, request}, info) => {
  const origin = String(info.project.use.baseURL);
  const headers = {Authorization: "Bearer cpu-budget-test-token"};
  const control = (data: Record<string, unknown>) => request.post(`${origin}/__cpu_fixture/state`, {headers, data});
  const reset = await control({reset: true}); expect(reset.ok()).toBeTruthy();
  const build = await reset.json();
  const read = async () => (await request.get(`${origin}/api/v1/host-cpu-budget`, {headers})).json();
  const invitation = await request.post(`http://127.0.0.1:${new URL(origin).port}/api/v1/auth/pairings`, {headers, data: {name: "Synthetic CPU worker device"}});
  expect(invitation.ok()).toBeTruthy();
  const pairing = await invitation.json();
  await page.goto(`/#pair=${encodeURIComponent(pairing.secret)}&code=${encodeURIComponent(pairing.confirmation_code)}`);
  await page.getByLabel("Device name").fill("Synthetic CPU worker device");
  await page.getByRole("button", {name: "Pair device", exact: true}).click();
  await expect(page.getByRole("heading", {name: "Pair this device", exact: true})).toBeHidden({timeout: 20000});
  await page.goto("/settings#setup-settings");
  const section = page.getByRole("region", {name: "CPU budget", exact: true});
  const toggle = section.getByLabel("Enable background PDF workers");
  const mode = section.getByLabel("CPU budget mode");
  const limit = section.getByLabel("Maximum background CPUs");
  const save = section.getByRole("button", {name: "Save CPU budget"});
  const reload = section.getByRole("button", {name: "Reload CPU budget"});
  const target = Math.min(2, build.policy.maximum_worker_cpus);
  await section.scrollIntoViewIfNeeded();
  await expect(toggle).not.toBeChecked();
  await expect(section.getByText("Background PDF workers are disabled.", {exact: true})).toBeVisible();
  await expect(section.getByText(/Chat runtimes, external tools and GPUs/)).toBeVisible();
  await mode.selectOption("custom");
  await limit.fill(String(build.policy.maximum_worker_cpus + 1));
  await expect(save).toBeDisabled();
  await limit.fill(String(target)); await save.click();
  await expect(save).toBeDisabled();
  expect((await read()).enabled).toBe(false);

  const current = await read();
  const changed = await request.put(`${origin}/api/v1/host-cpu-budget`, {headers, data: {host_id: current.host_id, expected_revision: current.revision, cpu_limit: 1, enabled: false}});
  expect(changed.ok()).toBeTruthy();
  await toggle.check(); await save.click();
  await expect(section.getByRole("alert")).toContainText(/changed|409/);
  await expect(toggle).toBeChecked();
  await reload.click(); await expect(toggle).not.toBeChecked();
  await expect(limit).toHaveValue("1");
  await limit.fill(String(target)); await toggle.check(); await save.click();
  await expect(section.getByText(`Running ${target} PDF workers on ${target} CPUs.`, {exact: true})).toBeVisible({timeout: 30000});
  const running = await read();
  expect(running.worker_count).toBe(target);
  expect(running.worker_pids).not.toContain(build.core_pid);
  expect(new Set(running.worker_affinities.flat()).size).toBe(target);
  expect(running.worker_affinities.every((cpus: number[]) => cpus.length === 1)).toBe(true);
  await page.reload(); await expect(toggle).toBeChecked();
  await expect(section.getByText(`Running ${target} PDF workers on ${target} CPUs.`, {exact: true})).toBeVisible();

  const render = await request.post(`${origin}/api/v1/reports/report/renders`, {headers, data: {report_revision: 1}});
  expect(render.ok()).toBeTruthy(); const renderId = (await render.json()).id;
  await expect.poll(async () => (await (await request.get(`${origin}/api/v1/report-renders/${renderId}`, {headers})).json()).status, {timeout: 30000}).toBe("completed");
  const pdf = await request.get(`${origin}/api/v1/report-renders/${renderId}/pdf`, {headers});
  expect(pdf.ok()).toBeTruthy(); expect((await pdf.body()).subarray(0, 5).toString()).toBe("%PDF-");

  expect((await control({hold: true})).ok()).toBeTruthy();
  await toggle.uncheck(); await save.click();
  await expect(section.getByText(/Draining accepted PDF work/)).toBeVisible();
  expect((await read()).worker_count).toBeGreaterThan(0);
  expect((await control({release: true})).ok()).toBeTruthy();
  await expect(section.getByText("Background PDF workers are disabled.", {exact: true})).toBeVisible({timeout: 30000});
  expect((await read()).worker_count).toBe(0);

  await control({fail: true});
  await toggle.check(); await save.click();
  await expect(section.getByText(/could not apply the setting/)).toBeVisible({timeout: 30000});
  expect((await read()).apply_state).toBe("failed");
  await control({fail: false}); await save.click();
  await expect(section.getByText(`Running ${target} PDF workers on ${target} CPUs.`, {exact: true})).toBeVisible({timeout: 30000});
  await toggle.uncheck(); await save.click();
  await expect(section.getByText("Background PDF workers are disabled.", {exact: true})).toBeVisible({timeout: 30000});
  await page.reload(); await expect(toggle).not.toBeChecked();
  expect((await read()).worker_count).toBe(0);

  await control({unsupported: true}); await reload.click();
  await expect(toggle).toBeDisabled();
  await expect(section.getByText(/Linux CPU affinity is unavailable/)).toBeVisible();
  await control({unsupported: false}); await reload.click();
  await expect(toggle).toBeEnabled();
  await section.scrollIntoViewIfNeeded();
  expect((await new AxeBuilder({page}).include(".host-cpu-settings").analyze()).violations).toEqual([]);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true);
  for (const element of await section.locator("input, select, button").all()) {
    expect((await element.boundingBox())?.height).toBeGreaterThanOrEqual(44);
  }
  await page.screenshot({path: info.outputPath("host-cpu-activation.png"), fullPage: true});
  await info.attach("candidate-build", {body: JSON.stringify({origin, bundleSha256: build.bundle_sha256, project: info.project.name, viewport: page.viewportSize(), observedRunning: running, final: await read()}), contentType: "application/json"});
});
