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
    reset: async (cold = false, memory = false, scenario?: "provider-tools-context" | "standing-instructions-native" | "work-settings") => {
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
  const headers = {Authorization: `Bearer ${fixture.token}`};
  const read = async (path: string) => {
    const response = await request.get(fixture.origin + path, {headers});
    expect(response.ok(), await response.text()).toBe(true);
    return response.json();
  };
  await expect.poll(() => read(`/api/v1/chat/sessions/${fixture.session_id}/pending-turn`), {timeout: 30_000}).toBeNull();
  const original = await read(`/api/v1/chat-sessions/${fixture.session_id}`);
  // This public operation saves a visible empty conversation and an unstarted
  // draft goal. It sends no provider work; generic session creation is read-only.
  let created = await request.post(`${fixture.origin}/api/v1/chat/goal-conversations`, {
    headers: {...headers, "Idempotency-Key": "newer-empty-reopen-distractor"},
    data: {engagement_id: "continuity-project", backend: "provider", provider_id: "continuity-provider", model: "model-a",
      objective: "Newer empty continuity conversation", completion_criteria: ["Keep this synthetic goal as an unstarted draft."]},
  });
  if (created.status() === 202) {
    const accepted = await created.json();
    const command = accepted.command_id ?? accepted.receipt.command_id;
    await expect.poll(async () => {
      created = await request.get(`${fixture.origin}/api/v1/runtime/requests/${command}/response`, {headers});
      return created.status();
    }, {timeout: 30_000}).not.toBe(202);
  }
  expect(created.ok(), await created.text()).toBe(true);
  const saved = await created.json();
  expect(saved.session.id).not.toBe(fixture.session_id);
  expect(saved.goal.session_id).toBe(saved.session.id);
  expect(saved.goal.status).toBe("draft");
  const newer = await read(`/api/v1/chat-sessions/${saved.session.id}`);
  expect(newer.engagement_id).toBe(original.engagement_id);
  expect(Date.parse(newer.created_at)).toBeGreaterThan(Date.parse(original.created_at));
  expect(Date.parse(newer.updated_at)).toBeGreaterThan(Date.parse(original.updated_at));
  expect(await read(`/api/v1/chat/sessions/${newer.id}/messages?include_replaced=true`)).toEqual([]);
  const recent = await read("/api/v1/chat-sessions?engagement_id=continuity-project&recent_first=true&limit=20");
  expect(recent[0].id).toBe(newer.id);
  expect(recent.findIndex((item: {id: string}) => item.id === fixture.session_id)).toBeGreaterThan(0);
  await expect(composer).toHaveValue("Unsent draft remains on this device.");
  // Open a fresh application document without a selection hint. Only the
  // product may restore the remembered older chat and its device-local draft.
  await page.goto("/");
  await expect(page).toHaveURL(new RegExp(`/projects/continuity-project/workbench\\?view=chat&session=${fixture.session_id}`));
  await expect(page.locator(".chat-message.assistant .assistant-markdown").filter({hasText: "First last"})).toHaveText("First last", {timeout: 60_000});
  await expect(composer).toHaveValue("Unsent draft remains on this device.");
  const reopenedUrl = page.url();
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
  // An intentional explicit link remains authoritative over remembered state.
  await page.goto(`/projects/continuity-project/workbench?view=chat&session=${newer.id}`);
  await expect(composer).toBeVisible();
  await expect(page).toHaveURL(new RegExp(`session=${newer.id}`));
  await expect(page.getByText("Loading conversation…", {exact: true})).toBeHidden();
  await expect(composer).toHaveValue("");
  await expect(page.locator(".chat-message.assistant")).toHaveCount(0);
  expect((await read(`/api/v1/chat/sessions/${newer.id}/goal`)).status).toBe("draft");
  expect(submissions).toBe(1);
  const observed = await control.control("inspect");
  const starts = observed.events.filter((item: {event: string}) => item.event === "provider-start");
  expect(starts).toHaveLength(1);
  const evidence = info.outputPath("navigation-reopen.json");
  await writeFile(evidence, JSON.stringify({creation: "authenticated public empty conversation with an unstarted draft goal",
    original, newer, goal: saved.goal, recentIds: recent.map((item: {id: string}) => item.id), reopenedEntry: "/", reopenedUrl,
    explicitUrl: page.url(), submissions, providerStarts: starts}, null, 2));
  await info.attach("automatic-selected-chat-reopen", {path: evidence, contentType: "application/json"});
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
  expect((await read(`/api/v1/chat-sessions/${forkId}`)).parent_session_id).toBe(fixture.session_id);
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

test("native standing instructions survive lossy compaction, reconnect and API replacement without transcript injection", async ({page, request}, info) => {
  test.setTimeout(300_000);
  const control = await controller(request);
  const fixture = await control.reset(false, false, "standing-instructions-native");
  expect(fixture.scenario).toBe("standing-instructions-native");
  const setup = fixture.scenario_receipt;
  const headers = {Authorization: `Bearer ${fixture.token}`};
  const read = async (path: string) => {
    const response = await request.get(fixture.origin + path, {headers});
    expect(response.ok(), await response.text()).toBe(true);
    return response.json();
  };
  const delivered = async (initial: Awaited<ReturnType<APIRequestContext["post"]>>) => {
    let response = initial;
    if (response.status() === 202) {
      const saved = await response.json();
      const identity = saved.command_id ?? saved.receipt.command_id;
      await expect.poll(async () => {
        response = await request.get(`${fixture.origin}/api/v1/runtime/requests/${identity}/response`, {headers});
        return response.status();
      }, {timeout: 30_000}).not.toBe(202);
    }
    expect(response.ok(), await response.text()).toBe(true);
    return response.json();
  };
  let controls = 0;
  const native = async (operation: "capture" | "compact" | "reconnect") => delivered(await request.post(
    fixture.origin + setup.control_path, {
      headers: {...headers, "Idempotency-Key": `native-fixture-${operation}-${++controls}`}, data: {operation},
    }));
  const captures: Record<string, unknown>[] = [];
  const capture = async () => {
    let value = await native("capture");
    await expect.poll(async () => {
      value = await native("capture");
      return value.turn_active;
    }).toBe(false);
    captures.push(value);
    return value;
  };
  const checkHead = (value: Record<string, any>, alpha: number, beta: number) => {
    expect(value.system_head_available).toBe(true);
    expect(value.alpha_copies).toBe(alpha);
    expect(value.beta_copies).toBe(beta);
    expect(value.effective_context.startsWith(value.system_head)).toBe(true);
    expect(createHash("sha256").update(value.system_head).digest("hex")).toBe(value.system_head_sha256);
    expect(createHash("sha256").update(value.standing_snapshot.text).digest("hex")).toBe(value.standing_snapshot.sha256);
    expect(value.system_head.split(value.standing_snapshot.text)).toHaveLength(2);
    const opened = value.native.requests.filter((item: Record<string, any>) => item.managed
      && item.method === "session/new" && item.native_session_id === value.active_native_session_id);
    expect(opened).toHaveLength(1);
    const channel = opened[0].params._meta.rules;
    expect(typeof channel).toBe("string");
    expect(channel.split(value.standing_snapshot.text)).toHaveLength(2);
    expect(value.system_head.split(channel)).toHaveLength(2);
    expect(createHash("sha256").update(channel).digest("hex")).toBe(value.channel_receipt.sha256);
    expect(value.channel_receipt.external_session_id).toBe(value.active_native_session_id);
  };
  await pair(page, request, fixture);
  await page.goto(`/projects/continuity-project/workbench?view=chat&session=${fixture.session_id}`);
  const composer = page.locator(".chat-composer textarea");
  await expect(composer).toBeVisible();
  await expect(page.locator(".chat-composer footer")).toContainText(setup.profile_name);
  let sent = 0;
  let submissions = 0;
  page.on("request", value => {if (value.method() === "POST" && value.url().includes("/chat/completions")) submissions++;});
  const assistantReplies = async () => {
    const messages = await read(`/api/v1/chat/sessions/${fixture.session_id}/messages?include_replaced=true`) as {id: string; role: string; content: string}[];
    return messages.filter(item => item.role === "assistant");
  };
  const send = async (text: string, answer: string) => {
    const previous = await assistantReplies();
    await composer.fill(text);
    await page.getByRole("button", {name: "Send message", exact: true}).click();
    sent++;
    await expect.poll(() => submissions).toBe(sent);
    let current = await assistantReplies();
    await expect.poll(async () => {
      current = await assistantReplies();
      return current.length;
    }).toBe(previous.length + 1);
    const added = current.filter(item => !previous.some(saved => saved.id === item.id));
    expect(added).toHaveLength(1);
    expect(added[0].content).toBe(answer);
    await expect(page.locator(`#chat-message-${added[0].id} .assistant-markdown`)).toHaveText(answer, {timeout: 60_000});
    await expect(page.getByRole("button", {name: "Stop response", exact: true})).toBeHidden();
    return capture();
  };
  const first = await send("Confirm the initial native instruction fixture.", "Native fixture confirmed ALPHA.");
  checkHead(first, 1, 0);
  const initialNativeId = first.active_native_session_id;
  expect(initialNativeId).toBeTruthy();
  const compacted = await native("compact");
  expect(compacted.native.compactions).toHaveLength(1);
  expect(compacted.native.compactions[0].removed_items).toBeGreaterThan(0);
  expect(compacted.native.compactions[0].system_sha256_after).toBe(compacted.native.compactions[0].system_sha256_before);
  const afterCompaction = await send("Confirm the fixture after its lossy native summary.", "Native fixture confirmed ALPHA.");
  checkHead(afterCompaction, 1, 0);
  expect(afterCompaction.active_native_session_id).toBe(initialNativeId);
  expect(afterCompaction.system_head_sha256).toBe(first.system_head_sha256);
  await native("reconnect");
  const afterReconnect = await send("Confirm the fixture after native transport reconnect.", "Native fixture confirmed ALPHA.");
  checkHead(afterReconnect, 1, 0);
  expect(afterReconnect.active_native_session_id).toBe(initialNativeId);
  expect(afterReconnect.system_head_sha256).toBe(first.system_head_sha256);
  await control.control("api", {version: "acceptance-v2"});
  await page.reload();
  await expect(composer).toBeVisible();
  await expect(page).toHaveURL(new RegExp(`session=${fixture.session_id}`));
  await expect(page.locator(".chat-message.assistant .assistant-markdown").last()).toHaveText("Native fixture confirmed ALPHA.");
  const afterApi = await send("Confirm the same native session after API replacement and reload.", "Native fixture confirmed ALPHA.");
  checkHead(afterApi, 1, 0);
  expect(afterApi.active_native_session_id).toBe(initialNativeId);
  const decisions = await read(`/api/v1/chat/sessions/${fixture.session_id}/decisions`);
  const decision = decisions.find((item: {id: string}) => item.id === setup.decision_id);
  expect(decision.text).toBe(setup.rule_alpha);
  const edited = await delivered(await request.put(`${fixture.origin}/api/v1/chat/sessions/${fixture.session_id}/decisions/${setup.decision_id}`, {
    headers: {...headers, "Idempotency-Key": "native-fixture-save-beta"},
    data: {expected_revision: decision.revision, kind: "constraint", text: setup.rule_beta, action: "save"},
  }));
  expect(edited.text).toBe(setup.rule_beta);
  expect(edited.revision).toBe(decision.revision + 1);
  const observedDecision = (await read(`/api/v1/chat/sessions/${fixture.session_id}/decisions`)).find((item: {id: string}) => item.id === setup.decision_id);
  expect(observedDecision.text).toBe(setup.rule_beta);
  expect(observedDecision.revision).toBe(edited.revision);
  const beta = await send("Confirm the fixture after the saved operator constraint changed.", "Native fixture confirmed BETA.");
  checkHead(beta, 0, 1);
  expect(beta.active_native_session_id).not.toBe(initialNativeId);
  expect(beta.system_head_sha256).not.toBe(first.system_head_sha256);
  expect((await read(`/api/v1/chat-sessions/${fixture.session_id}`)).id).toBe(fixture.session_id);
  const prompts = beta.native.requests.filter((item: {managed: boolean; method: string}) => item.managed && item.method === "session/prompt");
  expect(prompts).toHaveLength(sent);
  expect(beta.native.requests.some((item: {managed: boolean; method: string}) => item.managed && item.method === "session/load")).toBe(true);
  const inspected = await control.control("inspect");
  const original = inspected.events.find((item: {event: string; version: string}) => item.event === "project-constructed" && item.version === "acceptance-v1");
  const retained = original.source.split("/source/nebula/")[0];
  expect(prompts.every((item: {release_version: string; fixture_source: string; python: string; params: {prompt: unknown}}) =>
    item.release_version === "acceptance-v1" && item.fixture_source.startsWith(retained + "/") && item.python.startsWith(retained + "/python/")
    && !JSON.stringify(item.params.prompt).includes(setup.rule_alpha) && !JSON.stringify(item.params.prompt).includes(setup.rule_beta))).toBe(true);
  expect(submissions).toBe(sent);
  await composer.focus();
  expect(await composer.evaluate(element => {
    const box = element.getBoundingClientRect(); const viewport = visualViewport;
    return !viewport || (box.bottom <= viewport.offsetTop + viewport.height + 1 && box.top >= viewport.offsetTop - 1);
  })).toBe(true);
  expect((await new AxeBuilder({page}).include(".chat-message.assistant").analyze()).violations).toEqual([]);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true);
  await page.screenshot({path: info.outputPath("native-standing-instructions.png"), fullPage: true});
  const evidence = info.outputPath("native-standing-instructions.json");
  await writeFile(evidence, JSON.stringify({scope: "in-process zero-worker ACP transport; actual Grok adapter and public Core routes",
    mutation: "operator decision updated through authenticated public API; replies and reload through visible UI",
    initialization: setup, captures, edited, submissions, native: beta.native, runtime: inspected}, null, 2));
  await info.attach("native-instruction-delivery", {path: evidence, contentType: "application/json"});
});

test("global Work observes distinct execution workers across API replacement and Settings reads host-wide recorded usage", async ({page, request}, info) => {
  test.setTimeout(300_000);
  const control = await controller(request);
  const fixture = await control.reset(false, false, "work-settings");
  expect(fixture.scenario).toBe("work-settings");
  const setup = fixture.scenario_receipt;
  const projects = setup.projects as {project_id: string; name: string; session_id: string; profile_id: string; profile_name: string}[];
  expect(projects).toHaveLength(2);
  const headers = {Authorization: `Bearer ${fixture.token}`};
  const read = async (path: string) => {
    const response = await request.get(fixture.origin + path, {headers});
    expect(response.ok(), await response.text()).toBe(true);
    return response.json();
  };
  const mutate = async (path: string, data: Record<string, unknown>, key: string) => {
    let response = await request.post(fixture.origin + path, {headers: {...headers, "Idempotency-Key": key}, data});
    if (response.status() === 202) {
      const accepted = await response.json();
      const command = accepted.command_id ?? accepted.receipt.command_id;
      await expect.poll(async () => {
        response = await request.get(`${fixture.origin}/api/v1/runtime/requests/${command}/response`, {headers});
        return response.status();
      }, {timeout: 30_000}).not.toBe(202);
    }
    expect(response.ok(), await response.text()).toBe(true);
    return response.json();
  };
  const sse: {status: number; contentType: string; apiVersion?: string}[] = [];
  page.on("response", response => {
    if (new URL(response.url()).pathname === "/api/v1/work/events") {
      sse.push({status: response.status(), contentType: response.headers()["content-type"] ?? "",
        apiVersion: response.headers()["x-nebula-api-version"]});
    }
  });
  await pair(page, request, fixture);
  expect(await read("/api/v1/harnesses")).toEqual([]);
  await control.control("execution", {version: "acceptance-v1", project_id: projects[1].project_id});
  const initial = await control.control("inspect");
  const executionPids = initial.processes.filter((item: {role: string; alive: boolean}) => item.role === "execution" && item.alive)
    .map((item: {pid: number}) => item.pid).sort((a: number, b: number) => a - b);
  expect(executionPids).toHaveLength(2);
  expect(new Set(executionPids).size).toBe(2);
  const originalApi = initial.processes.find((item: {role: string; alive: boolean}) => item.role === "api" && item.alive);
  expect(originalApi).toBeDefined();
  await page.goto("/projects");
  await expect(page.getByRole("heading", {name: "All projects", exact: true})).toBeVisible();
  await expect(page.locator(".work-live-state")).toHaveText("Live", {timeout: 30_000});
  expect(sse.some(item => item.status === 200 && item.contentType.includes("text/event-stream"))).toBe(true);
  expect(await read("/api/v1/work/items")).toEqual([]);
  expect(await read("/api/v1/work/updates")).toEqual([]);
  expect(await read("/api/v1/work/agents")).toEqual([]);
  await expect(page.getByText("No agents are working right now.", {exact: true})).toBeVisible();
  const items: {id: string; engagement_id: string; title: string; revision: number}[] = [];
  const updates: {id: string; item_id: string; engagement_id: string; summary: string}[] = [];
  const viewers: Page[] = [];
  let completedUsage: unknown;
  await control.control("hold", {name: "hold-provider"});
  try {
    for (const [index, project] of projects.entries()) {
      const item = await mutate(`/api/v1/engagements/${project.project_id}/work`, {
        title: `Continuity Work ${index + 1}`, description: "Synthetic cross-worker SQL and streaming check.",
        status: "in_progress", assignee_session_id: project.session_id,
        source_kind: "chat", source_id: project.session_id, request_id: `work-create-${index}`,
      }, `work-create-${index}`);
      items.push(item);
      const update = await mutate(`/api/v1/engagements/${project.project_id}/work/${item.id}/updates`, {
        summary: `Initial persisted check-in ${index + 1}`, status: "in_progress",
        expected_revision: item.revision, request_id: `work-update-before-${index}`,
      }, `work-update-before-${index}`);
      updates.push(update);
      await expect(page.locator(".work-project-latest").filter({hasText: item.title})).toBeVisible({timeout: 30_000});
      await expect(page.locator(".work-update-preview").filter({hasText: update.summary})).toBeVisible({timeout: 30_000});
      const viewer = await page.context().newPage();
      viewers.push(viewer);
      await viewer.goto(`/projects/${project.project_id}/workbench?view=chat&session=${project.session_id}`);
      const composer = viewer.locator(".chat-composer textarea");
      await expect(composer).toBeVisible();
      await composer.fill(`Run the inert Work activity check for ${project.project_id}.`);
      await viewer.getByRole("button", {name: "Send message", exact: true}).click();
      await expect.poll(async () => (await control.control("inspect")).events
        .filter((item: {event: string}) => item.event === "provider-start").length, {timeout: 30_000}).toBe(index + 1);
      await expect(viewer.locator(".chat-message.assistant .assistant-markdown").last()).toContainText("First", {timeout: 30_000});
      await viewer.close();
    }
    const working = page.getByRole("region", {name: "Agents working now", exact: true});
    await expect(working.locator("li")).toHaveCount(2, {timeout: 30_000});
    for (const item of items) await expect(working).toContainText(item.title);
    const agents = await read("/api/v1/work/agents");
    expect(agents.map((item: {session_id: string}) => item.session_id).sort()).toEqual(projects.map(item => item.session_id).sort());
    expect(agents.every((item: {state: string}) => item.state === "working")).toBe(true);
    const before = await control.control("inspect");
    const writes = before.events.filter((item: {event: string}) => item.event === "work-mutation");
    expect(writes).toHaveLength(4);
    expect([...new Set(writes.map((item: {pid: number}) => item.pid))].sort((a, b) => Number(a) - Number(b))).toEqual(executionPids);
    await page.screenshot({path: info.outputPath("global-work-active.png"), fullPage: true});
    const streamsBeforeReplacement = sse.filter(item => item.status === 200).length;
    const promoted = await control.control("api", {version: "acceptance-v2"});
    await control.control("retire", {runtime_id: originalApi.id});
    await expect.poll(async () => (await control.control("inspect")).processes
      .find((item: {id: string}) => item.id === originalApi.id)?.alive).toBe(false);
    await expect.poll(() => sse.filter(item => item.status === 200).length, {timeout: 30_000}).toBeGreaterThan(streamsBeforeReplacement);
    expect(sse.filter(item => item.status === 200).every(item => item.contentType.includes("text/event-stream"))).toBe(true);
    await expect(page.locator(".work-live-state")).toHaveText("Live", {timeout: 30_000});
    for (const [index, project] of projects.entries()) {
      const item = await read(`/api/v1/engagements/${project.project_id}/work/${items[index].id}`);
      const update = await mutate(`/api/v1/engagements/${project.project_id}/work/${item.id}/updates`, {
        summary: `Execution ${index + 1} persisted after API replacement`, next_step: "Keep the same saved Work identity.",
        status: "review", expected_revision: item.revision, request_id: `work-update-after-${index}`,
      }, `work-update-after-${index}`);
      updates.push(update);
      await expect(page.locator(".work-update-preview").filter({hasText: update.summary})).toBeVisible({timeout: 30_000});
    }
    const persisted = await control.control("work-evidence");
    expect(persisted.items.map((item: {id: string}) => item.id).sort()).toEqual(items.map(item => item.id).sort());
    expect(persisted.updates.map((item: {id: string}) => item.id).sort()).toEqual(updates.map(item => item.id).sort());
    expect((await read("/api/v1/work/items?engagement_id=continuity-project")).map((item: {id: string}) => item.id)).toEqual([items[0].id]);
    const after = await control.control("inspect");
    expect(after.processes.filter((item: {role: string; alive: boolean}) => item.role === "execution" && item.alive)
      .map((item: {pid: number}) => item.pid).sort((a: number, b: number) => a - b)).toEqual(executionPids);
    expect(after.events.filter((item: {event: string}) => item.event === "work-mutation")).toHaveLength(6);
    expect(after.events.filter((item: {event: string}) => item.event === "provider-start")).toHaveLength(2);
    for (const target of [page.getByRole("button", {name: "Refresh Work", exact: true}), page.getByRole("button", {name: "New item", exact: true}), page.getByRole("searchbox", {name: "Search projects", exact: true})]) {
      expect((await target.boundingBox())?.height).toBeGreaterThanOrEqual(44);
    }
    expect((await new AxeBuilder({page}).include(".work-page").analyze()).violations).toEqual([]);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true);
    await page.screenshot({path: info.outputPath("global-work-after-replacement.png"), fullPage: true});
    await control.control("release", {name: "hold-provider"});
    await expect.poll(async () => Promise.all(projects.map(item => read(`/api/v1/chat/sessions/${item.session_id}/pending-turn`))), {timeout: 30_000}).toEqual([null, null]);
    await expect(working.locator("li")).toHaveCount(0, {timeout: 30_000});
    expect(await read("/api/v1/work/agents")).toEqual([]);
    expect(await read("/api/v1/harnesses")).toEqual([]);
    const firstUsage = page.waitForResponse(response => new URL(response.url()).pathname === "/api/v1/runtime-usage" && response.request().method() === "GET");
    await page.goto("/settings#usage-settings");
    const initialUsage = await firstUsage;
    expect(initialUsage.ok(), await initialUsage.text()).toBe(true);
    const section = page.getByRole("region", {name: "Provider and harness usage", exact: true});
    await expect(section.getByRole("heading", {name: "Balances & activity", exact: true})).toBeVisible();
    for (const project of projects) {
      const card = section.getByRole("article", {name: `${project.profile_name} usage`, exact: true});
      await expect(card.getByText("Unavailable", {exact: true})).toBeVisible();
      await expect(card.locator(".runtime-usage-recorded span").filter({hasText: "tokens tracked in Nebula"}).getByText("6", {exact: true})).toBeVisible();
    }
    const refreshed = page.waitForResponse(response => {
      const url = new URL(response.url());
      return url.pathname === "/api/v1/runtime-usage" && url.searchParams.get("refresh") === "true";
    });
    const refresh = section.getByRole("button", {name: "Refresh provider and harness usage", exact: true});
    await refresh.click();
    const response = await refreshed;
    expect(response.ok(), await response.text()).toBe(true);
    const usage = await response.json();
    completedUsage = usage;
    expect(usage.items).toHaveLength(2);
    for (const project of projects) {
      const item = usage.items.find((item: {profile_id: string}) => item.profile_id === project.profile_id);
      expect(item.kind).toBe("provider");
      expect(item.provider_type).toBe("vllm");
      expect(item.vendor.state).toBe("unavailable");
      expect(item.recorded).toMatchObject(setup.expected_usage_per_profile);
    }
    await refresh.focus();
    expect((await refresh.boundingBox())?.height).toBeGreaterThanOrEqual(44);
    expect((await new AxeBuilder({page}).include("#runtime-usage").analyze()).violations).toEqual([]);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true);
    await page.screenshot({path: info.outputPath("global-runtime-usage.png"), fullPage: true});
    const final = await control.control("inspect");
    expect(final.events.filter((item: {event: string}) => item.event === "provider-start")).toHaveLength(2);
    expect(final.processes.filter((item: {role: string; alive: boolean}) => item.role === "execution" && item.alive)
      .map((item: {pid: number}) => item.pid).sort((a: number, b: number) => a - b)).toEqual(executionPids);
    const evidence = info.outputPath("global-work-settings.json");
    await writeFile(evidence, JSON.stringify({scope: "public SQL-backed Work/SSE and host usage; local inert providers, no saved harnesses or vendor calls",
      viewerLifecycle: "normal paired chat viewers closed after first output; Core-owned turns remained visible in global Work",
      initialization: setup, initial, promoted, items, updates, agents, persisted, streamsBeforeReplacement, sse, usage, final,
      completed: await control.control("work-evidence")}, null, 2));
    await info.attach("global-work-and-host-usage", {path: evidence, contentType: "application/json"});
  } catch (error) {
    await info.attach("work-before-release", {body: JSON.stringify({runtime: await control.control("inspect"),
      work: await control.control("work-evidence"), usage: completedUsage}, null, 2), contentType: "application/json"});
    throw error;
  } finally {
    await control.control("release", {name: "hold-provider"});
    await Promise.allSettled(viewers.filter(viewer => !viewer.isClosed()).map(viewer => viewer.close()));
  }
});
