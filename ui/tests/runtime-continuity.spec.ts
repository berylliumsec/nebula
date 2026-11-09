import {createHash} from "node:crypto";
import {readFile, writeFile} from "node:fs/promises";
import {expect, test, type APIRequestContext, type Page} from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";

const observedErrors = new WeakMap<Page, {method: string; path: string; status: number}[]>();
test.beforeEach(async ({page}) => {
  const errors: {method: string; path: string; status: number}[] = [];
  observedErrors.set(page, errors);
  page.on("response", response => {
    if (response.status() >= 400) errors.push({method: response.request().method(), path: new URL(response.url()).pathname, status: response.status()});
  });
});

async function controller(request: APIRequestContext) {
  const authFile = test.info().config.metadata.runtimeContinuityAuthFile;
  if (typeof authFile !== "string") throw new Error("Use the dedicated runtime-continuity fixture configuration.");
  const {token, control_origin: origin} = JSON.parse(await readFile(authFile, "utf8"));
  const headers = {Authorization: `Bearer ${token}`};
  return {
    reset: async (cold = false, memory = false) => {
      const response = await request.post(`${origin}/reset`, {headers, data: {cold, memory}});
      expect(response.ok(), await response.text()).toBe(true);
      return response.json();
    },
    control: async (operation: string, values: Record<string, unknown> = {}) => {
      const response = await request.post(`${origin}/control`, {headers, data: {operation, ...values}});
      expect(response.ok(), await response.text()).toBe(true);
      return response.json();
    },
    cleanup: async () => {
      const response = await request.post(`${origin}/cleanup`, {headers});
      expect(response.ok(), await response.text()).toBe(true);
    },
  };
}

test.afterEach(async ({request, page}, info) => {
  if (info.status === "skipped") return;
  const control = await controller(request);
  try {
    if (info.status !== info.expectedStatus) await page.screenshot({path: info.outputPath("before-cleanup.png"), fullPage: true});
    const health = await control.control("diagnostics");
    const evidence = info.outputPath("before-cleanup.json");
    await writeFile(evidence, JSON.stringify({health, httpErrors: observedErrors.get(page) ?? []}, null, 2));
    await info.attach("before-cleanup-health", {path: evidence, contentType: "application/json"});
  } finally {
    await control.cleanup();
  }
});

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
  await expect(page.locator(".chat-message.assistant .assistant-markdown").filter({hasText: "First last"})).toHaveText("First last", {timeout: 60_000});
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

test("production Settings applies a live memory reduction to the original physical job after API replacement", async ({page, request}, info) => {
  test.skip(!process.env.NEBULA_TEST_MEMORY_RUNTIME || !process.env.NEBULA_TEST_MEMORY_IMAGE,
    "The physical allocator gate requires an explicitly selected disposable runtime and cached image.");
  const control = await controller(request);
  const fixture = await control.reset(false, true);
  await pair(page, request, fixture);
  const identity = "lan-physical-continuity";
  const headers = {Authorization: `Bearer ${fixture.token}`};
  await page.goto("/settings#memory-budget-settings");
  const section = page.getByRole("region", {name: "Memory budgets", exact: true});
  await section.getByText("Enforcement support", {exact: true}).click();
  const profile = section.getByRole("listitem").filter({hasText: "Disposable continuity allocator"});
  await profile.getByRole("button", {name: "Check enforcement", exact: true}).click();
  await expect(section.getByText(/Runner support checked/)).toBeVisible();
  await expect.poll(async () => {
    const response = await request.get(`${fixture.origin}/api/v1/memory-budgets`, {headers});
    const settings = await response.json();
    return settings.capabilities.find((item: {profile_id: string; environment: string}) =>
      item.profile_id === "continuity-allocator" && item.environment === "container")?.live_update;
  }).toBe(true);
  const read = async () => {
    const response = await request.get(`${fixture.origin}/api/v1/memory-budgets/jobs/${identity}`, {headers});
    return response.ok() ? response.json() : undefined;
  };
  await control.control("start-allocation", {job_id: identity});
  try {
    await expect.poll(async () => {
      const job = await read();
      return job?.state === "running" && job.evidence?.current_bytes >= 4 * 1024 ** 2;
    }, {timeout: 30_000}).toBe(true);
    const original = await read();
    await control.control("api", {version: "acceptance-v2"});
    await page.goto("/settings#memory-budget-settings");
    const job = section.getByRole("listitem").filter({hasText: identity});
    await expect(job).toContainText("running");
    await job.getByText("Change this running job’s limits", {exact: true}).click();
    const ceiling = job.getByLabel(/Running job ceiling/);
    await expect(ceiling).toHaveValue("0.03125");
    await expect(ceiling).toBeEnabled();
    await ceiling.fill("0.015625");
    await job.getByRole("checkbox", {name: "I understand that lowering a limit can terminate this job.", exact: true}).check();
    await job.getByRole("button", {name: "Apply to this job", exact: true}).click();
    await expect.poll(async () => (await read())?.evidence?.applied?.max_bytes).toBe(16 * 1024 ** 2);
    const observed = await read();
    expect(observed.application_state).toBe("verified");
    expect(observed.evidence.group_id).toBe(original.evidence.group_id);
    expect(observed.launch_owner).toEqual(original.launch_owner);
    expect(observed.launch_groups).toEqual(original.launch_groups);
    expect(observed.state).toBe("running");
    const physical = await control.control("physical-group", {job_id: identity});
    expect(physical.id).toBe(original.evidence.group_id);
    expect(physical.memory_bytes).toBe(16 * 1024 ** 2);
    expect(physical.running).toBe(true);
    expect(physical.oom_killed).toBe(false);
    await page.reload();
    await expect(job).toContainText("verified");
    await job.getByText("Change this running job’s limits", {exact: true}).click();
    await expect(ceiling).toHaveValue("0.015625");
    await ceiling.focus();
    expect(await ceiling.evaluate(element => {
      const box = element.getBoundingClientRect(); const viewport = visualViewport;
      return !viewport || (box.bottom <= viewport.offsetTop + viewport.height + 1 && box.top >= viewport.offsetTop - 1);
    })).toBe(true);
    for (const target of await job.locator("input[type=number]:visible,button:visible").all()) {
      expect((await target.boundingBox())?.height).toBeGreaterThanOrEqual(44);
    }
    expect((await job.getByRole("checkbox").locator("..").boundingBox())?.height).toBeGreaterThanOrEqual(44);
    expect((await new AxeBuilder({page}).include("#memory-budget-settings").analyze()).violations).toEqual([]);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true);
    await page.screenshot({path: info.outputPath("physical-live-memory.png"), fullPage: true});
  } finally {
    await control.control("release-allocation", {job_id: identity});
    const result = await control.control("allocation-result", {job_id: identity});
    expect(result.exit_code).toBe(0);
  }
  await expect.poll(async () => {
    const job = await read();
    return job?.state === "finished" && job.evidence?.group_empty && job.reserved_bytes === 0;
  }).toBe(true);
});
