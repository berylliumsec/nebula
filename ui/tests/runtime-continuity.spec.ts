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
    reset: async (cold = false, memory = false, scenario?: "provider-tools-context") => {
      const response = await request.post(`${origin}/reset`, {headers, data: {cold, memory, scenario}});
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
    const health = await control.control("diagnostics", {session_id: new URL(page.url()).searchParams.get("session")});
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

test("loaded provider tools survive compaction, reload, execution replacement and a historical fork", async ({page, request}, info) => {
  test.setTimeout(300_000);
  const control = await controller(request);
  const fixture = await control.reset(false, false, "provider-tools-context");
  expect(fixture.scenario).toBe("provider-tools-context");
  const schema = fixture.scenario_receipt.input_schema;
  const deferredName = fixture.scenario_receipt.deferred_tool;
  const auth = {Authorization: `Bearer ${fixture.token}`};
  const read = async (path: string) => {
    const response = await request.get(fixture.origin + path, {headers: auth});
    expect(response.ok(), await response.text()).toBe(true);
    return response.json();
  };
  const manifest = async (session = fixture.session_id) => (await control.control("tool-context", {session_id: session})).manifest;
  const checkDefinition = (value: {definitions: Record<string, unknown>[]}) => {
    const definition = value.definitions.find(item => item.name === deferredName);
    expect(definition).toBeDefined();
    expect(definition!.representation).toBe("deferred");
    expect(definition!.schema_present).toBe(true);
    expect(definition!.input_schema).toEqual(schema);
    expect(createHash("sha256").update(String(definition!.description)).digest("hex")).toBe(fixture.scenario_receipt.description_sha256);
    return definition!;
  };
  await pair(page, request, fixture);
  await page.goto(`/projects/continuity-project/workbench?view=chat&session=${fixture.session_id}`);
  const composer = page.locator(".chat-composer textarea");
  await expect(composer).toBeVisible();
  await page.getByRole("button", {name: "Assistant settings", exact: true}).click();
  const settings = page.getByRole("dialog", {name: "Assistant settings", exact: true});
  await expect(settings.getByRole("combobox", {name: "Chat provider"})).toHaveValue("continuity-provider");
  await expect(settings.getByRole("checkbox", {name: /Synthetic-native-source/})).toBeChecked();
  await expect(settings.getByRole("checkbox", {name: /Synthetic-deferred-source/})).not.toBeChecked();
  await settings.getByRole("button", {name: "Close assistant settings", exact: true}).click();
  let submissions = 0;
  let sent = 0;
  page.on("request", value => {if (value.method() === "POST" && value.url().includes("/chat/completions")) submissions++;});
  const send = async (content: string, answer: string, session = fixture.session_id) => {
    const previous = await page.locator(".chat-message.assistant").count();
    await composer.fill(content);
    await page.getByRole("button", {name: "Send message", exact: true}).click();
    sent++;
    await expect.poll(() => submissions).toBe(sent);
    await expect(page.locator(".chat-message.assistant")).toHaveCount(previous + 1);
    await expect(page.locator(".chat-message.assistant .assistant-markdown").last()).toContainText(answer, {timeout: 60_000});
    await expect.poll(async () => read(`/api/v1/chat/sessions/${session}/pending-turn`), {timeout: 30_000}).toBeNull();
    await expect(page.getByRole("button", {name: "Stop response", exact: true})).toBeHidden();
  };
  await send("[TOOLS_CONTEXT:LEARN]", "TOOLS_CONTEXT_LEARNED ABC");
  const learned = await manifest();
  const definition = checkDefinition(learned);
  const native = learned.definitions.find((item: {source_id: string}) => item.source_id === "mcp:" + fixture.scenario_receipt.mcp_server_ids[0]);
  expect(native?.representation).toBe("native");
  expect(native?.schema_present).toBe(true);
  await send("[TOOLS_CONTEXT:USE] before compaction", "TOOLS_CONTEXT_USED ABC");
  expect(checkDefinition(await manifest()).definition_digest).toBe(definition.definition_digest);
  let context = await read(`/api/v1/chat/sessions/${fixture.session_id}/context`);
  for (let index = 1; index <= 6 && context.compacted_through === 0; index++) {
    await send(`[TOOLS_CONTEXT:FILL] ${index}`, "TOOLS_CONTEXT_PADDING");
    context = await read(`/api/v1/chat/sessions/${fixture.session_id}/context`);
  }
  expect(context.compacted_through).toBeGreaterThan(0);
  expect(context.snapshot).toBeTruthy();
  expect(checkDefinition(await manifest()).definition_digest).toBe(definition.definition_digest);
  await page.getByRole("button", {name: /^Open context details/}).click();
  const inspector = page.locator(".session-inspector");
  await expect(inspector.getByRole("heading", {name: "Working context", exact: true})).toBeVisible();
  await expect(inspector.getByText(`Compacted through message ${context.compacted_through}. The original messages are unchanged and searchable.`, {exact: true})).toBeVisible();
  await inspector.getByRole("button", {name: "Close details", exact: true}).click();
  await page.reload();
  await expect(composer).toBeVisible();
  await expect(page).toHaveURL(new RegExp(`session=${fixture.session_id}`));
  await expect(page.locator(".chat-message.assistant .assistant-markdown").filter({hasText: "TOOLS_CONTEXT_LEARNED ABC"})).toBeVisible();
  await send("[TOOLS_CONTEXT:USE] after reload", "TOOLS_CONTEXT_USED ABC");
  const beforeHandoff = await manifest();
  const beforeEvents = (await control.control("inspect")).events;
  const oldProducer = beforeEvents.find((item: {event: string}) => item.event === "provider-tools-effect").pid;
  const target = await control.control("handoff", {version: "acceptance-v2", project_id: "continuity-project"});
  expect(target.pid).not.toBe(oldProducer);
  await expect.poll(async () => {
    const status = await read("/api/v1/deployment/status");
    return status.projects.some((item: {project_id: string; runtime_id: string; phase: string}) =>
      item.project_id === "continuity-project" && item.runtime_id === target.runtime_id && item.phase === "verified");
  }, {timeout: 60_000}).toBe(true);
  expect((await manifest()).digest).toBe(beforeHandoff.digest);
  await send("[TOOLS_CONTEXT:USE] in the fresh execution worker", "TOOLS_CONTEXT_USED ABC");
  const restored = await manifest();
  expect(checkDefinition(restored).definition_digest).toBe(definition.definition_digest);
  const originalEvents = (await control.control("inspect")).events;
  const calls = originalEvents.filter((item: {event: string}) => item.event === "provider-tools-request");
  const effects = originalEvents.filter((item: {event: string}) => item.event === "provider-tools-effect");
  expect(calls.filter((item: {stage: string}) => item.stage === "load")).toHaveLength(1);
  expect(calls.every((item: {canonical_sections: number}) => item.canonical_sections === 1)).toBe(true);
  expect(calls.filter((item: {stage: string}) => item.stage !== "load").every((item: {complete_definition_count: number}) => item.complete_definition_count === 1)).toBe(true);
  expect(calls.every((item: {load_result_schema_count: number}) => item.load_result_schema_count === 0)).toBe(true);
  expect(new Set(calls.filter((item: {stage: string}) => item.stage !== "load").map((item: {canonical_sha256: string}) => item.canonical_sha256)).size).toBe(1);
  expect(calls.every((item: {native_names: string[]}) => item.native_names.includes(native.name))).toBe(true);
  expect(effects).toHaveLength(4);
  expect(effects.at(-1).pid).toBe(target.pid);
  expect(originalEvents.some((item: {event: string; stage: string}) => item.event === "provider-tools-operation" && item.stage === "summary")).toBe(true);
  expect((await new AxeBuilder({page}).include(".chat-message.assistant").analyze()).violations).toEqual([]);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true);
  await page.screenshot({path: info.outputPath("provider-tools-restored.png"), fullPage: true});
  const historical = page.locator(".chat-message.assistant").filter({hasText: "TOOLS_CONTEXT_LEARNED ABC"});
  await historical.hover();
  await historical.getByRole("button", {name: "Fork conversation here", exact: true}).click();
  await expect.poll(() => {
    const selected = new URL(page.url()).searchParams.get("session");
    return Boolean(selected && selected !== fixture.session_id);
  }).toBe(true);
  const forkId = new URL(page.url()).searchParams.get("session");
  expect(forkId).toBeTruthy();
  await expect(page.locator(".chat-action-status").filter({hasText: "Branched conversation"})).toBeVisible();
  await expect(page.locator(".chat-message.assistant").filter({hasText: "TOOLS_CONTEXT_PADDING"})).toHaveCount(0);
  expect((await read(`/api/v1/chat/sessions/${forkId}`)).parent_session_id).toBe(fixture.session_id);
  expect(checkDefinition(await manifest(forkId)).definition_digest).toBe(definition.definition_digest);
  await send("[TOOLS_CONTEXT:USE] from the historical fork", "TOOLS_CONTEXT_USED ABC", forkId);
  expect(submissions).toBe(sent);
  const finalEvents = (await control.control("inspect")).events;
  expect(finalEvents.filter((item: {event: string; stage: string}) => item.event === "provider-tools-request" && item.stage === "load")).toHaveLength(1);
  expect(finalEvents.filter((item: {event: string}) => item.event === "provider-tools-effect")).toHaveLength(5);
  const evidence = info.outputPath("provider-tools-context.json");
  await writeFile(evidence, JSON.stringify({initialization: fixture.scenario_receipt, learned, context, restored,
    forkId, forkManifest: await manifest(forkId), submissions, events: finalEvents}, null, 2));
  await info.attach("durable-provider-tools", {path: evidence, contentType: "application/json"});
});
