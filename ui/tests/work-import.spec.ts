import AxeBuilder from "@axe-core/playwright";
import { spawn, spawnSync, type ChildProcessWithoutNullStreams } from "node:child_process";
import { existsSync } from "node:fs";
import { mkdir, mkdtemp, readFile, readdir, writeFile } from "node:fs/promises";
import { networkInterfaces, tmpdir } from "node:os";
import path from "node:path";
import { expect, request as playwrightRequest, test } from "@playwright/test";
import { coreMutation, removeCoreData, waitForCoreApi } from "./fixtures/core-api";
import { removeCoreSockets, stopCoreWorkers } from "./fixtures/core-harness";
import { withOwnedCleanup } from "./fixtures/owned-cleanup";

interface TestCore {
  process: ChildProcessWithoutNullStreams;
  dataDir: string;
  origin: string;
  token: string;
  output?: () => string;
}

function localNetworkIpv4(): string {
  for (const addresses of Object.values(networkInterfaces())) {
    for (const address of addresses ?? []) {
      if (address.family === "IPv4" && !address.internal) return address.address;
    }
  }
  throw new Error("A non-loopback IPv4 address is required for LAN acceptance.");
}

async function startCore(): Promise<TestCore> {
  const repository = path.resolve(import.meta.dirname, "../..");
  const common = spawnSync("git", ["-C", repository, "rev-parse", "--path-format=absolute", "--git-common-dir"], { encoding: "utf8" }).stdout.trim();
  const candidates = [
    process.env.NEBULA_TEST_CORE_BIN,
    process.env.NEBULA_CORE_BINARY,
    path.join(repository, ".venv/bin/nebula-core"),
    common ? path.join(path.dirname(common), ".venv/bin/nebula-core") : undefined,
  ].filter((value): value is string => Boolean(value));
  const binary = candidates.find(existsSync);
  if (!binary) throw new Error("No Nebula Core test binary is available.");
  const dataDir = await mkdtemp(path.join(tmpdir(), "nebula-work-import-core-"));
  const token = "work-import-test-administration-token";
  const child = spawn(binary, [
    "serve", "--host", "0.0.0.0", "--port", "0", "--token", token,
    "--allow-remote", "--allow-insecure-device-pairing", "--allow-browser-diagnostics",
    "--data-dir", dataDir, "--static-dir", path.join(repository, "ui/dist"),
  ], {
    cwd: repository,
    env: { ...process.env, PYTHONUNBUFFERED: "1", PYTHONPATH: [path.join(repository, "src"), process.env.PYTHONPATH].filter(Boolean).join(path.delimiter) },
  });
  let output = "";
  const startupTimeout = Math.max(30_000, Number(process.env.NEBULA_TEST_CORE_STARTUP_TIMEOUT_MS) || 0);
  const origin = await new Promise<string>((resolve, reject) => {
    const timeout = setTimeout(() => reject(new Error(`Real Core did not become ready.\n${output}`)), startupTimeout);
    const inspect = (chunk: Buffer) => {
      output += chunk.toString("utf8");
      const port = output.match(/"url"\s*:\s*"http:\/\/[^:\"]+:(\d+)"/);
      if (port) {
        clearTimeout(timeout);
        resolve(`http://${localNetworkIpv4()}:${port[1]}`);
      }
    };
    child.stdout.on("data", inspect);
    child.stderr.on("data", (chunk) => { output += chunk.toString("utf8"); });
    child.once("exit", (code) => {
      clearTimeout(timeout);
      reject(new Error(`Real Core exited with ${code}.\n${output}`));
    });
  }).catch(error => withOwnedCleanup(async () => { throw error; }, [
    {name: "failed Work fixture Core", dispose: () => stopCore({process: child, dataDir, origin: "", token, output: () => output})},
  ]));
  return { process: child, dataDir, origin, token, output: () => output };
}

async function stopCore(core: TestCore): Promise<void> {
  if (core.process.exitCode === null) {
    core.process.kill("SIGTERM");
    await Promise.race([
      new Promise<void>((resolve) => core.process.once("exit", () => resolve())),
      new Promise<void>((resolve) => setTimeout(resolve, 5_000)),
    ]);
    if (core.process.exitCode === null) core.process.kill("SIGKILL");
  }
  await stopCoreWorkers(core.dataDir);
  await removeCoreSockets(core.dataDir);
  const evidence = process.env.NEBULA_TEST_CORE_EVIDENCE_DIR;
  if (evidence) {
    const directory = path.join(evidence, path.basename(core.dataDir));
    await mkdir(directory, {recursive: true});
    const scrub = (value: string) => value.replaceAll(core.token, "<synthetic-token>");
    await writeFile(path.join(directory, "fixture-output.log"), scrub(core.output?.() ?? ""));
    const logs = path.join(core.dataDir, "logs");
    for (const name of await readdir(logs).catch(() => [] as string[])) {
      if (name.endsWith(".log")) await writeFile(path.join(directory, name), scrub(await readFile(path.join(logs, name), "utf8")));
    }
  }
  await removeCoreData(core.dataDir);
}

test("work hub import finds a project and its saved task after refresh", async ({ page }, testInfo) => {
  test.setTimeout(120_000);
  const core = await startCore();
  const api = await playwrightRequest.newContext({ baseURL: `${core.origin}/api/v1/`, extraHTTPHeaders: { Authorization: `Bearer ${core.token}` } });
  try {
    await waitForCoreApi(api);
    // Exceed the 80-row cap without relying on the optional Scratch project.
    const projects = Array.from({ length: 81 }, (_, index) => ({
      external_id: `sample-${index + 1}`, name: `Sample project ${index + 1}`,
    }));
    const first = await coreMutation(api, "post", "work/import", { source: "sample-tracker", projects: projects.slice(0, 50) });
    expect(first.ok(), await first.text()).toBe(true);
    const lastBatch = { source: "sample-tracker", projects: [
      ...projects.slice(50),
      { external_id: "imported-plan", name: "Imported plan", status: "active", items: [{
        external_id: "review-task", title: "Review the release plan", description: "Initial plan needs review", status: "blocked",
        update: { summary: "Draft is waiting for approval", blocker: "Approval pending", next_step: "Ask the reviewer" },
      }] },
    ] };
    const imported = await coreMutation(api, "post", "work/import", lastBatch);
    expect(imported.ok(), await imported.text()).toBe(true);
    const importedResult = await imported.json() as { projects: Array<{ engagement_id: string; items: Array<{ item_id: string }> }> };
    const parentId = (await first.json() as { projects: Array<{ engagement_id: string }> }).projects[0].engagement_id;
    const repository = path.resolve(import.meta.dirname, "../..");
    const commonGitDir = spawnSync("git", ["-C", repository, "rev-parse", "--path-format=absolute", "--git-common-dir"], { encoding: "utf8" }).stdout.trim();
    const python = process.env.NEBULA_TEST_PYTHON ?? path.join(path.dirname(commonGitDir), ".venv/bin/python");
    const seeded = spawnSync(python, ["-c", [
      "import sys",
      "from pathlib import Path",
      "from nebula.v3.domain import ChatSession",
      "from nebula.v3.storage import NebulaStore",
      "import asyncio",
      "from nebula.v3.async_utils import close_store_resources",
      "async def _seed():",
      "    store = None",
      "    try:",
      "        store = NebulaStore(Path(sys.argv[1]) / 'nebula.db')",
      "        (await store.create(ChatSession(id='sample-active-session', engagement_id=sys.argv[2], title='Sample research handoff', model='test-model', provider_profile_id='provider-1')))",
      "    finally:",
      "        await close_store_resources([item for item in [store] if item is not None])",
      "asyncio.run(_seed())"
    ].join("\n"), core.dataDir, parentId], { cwd: repository, env: { ...process.env, PYTHONPATH: path.join(repository, "src") }, encoding: "utf8" });
    expect(seeded.status, seeded.stderr || seeded.stdout).toBe(0);
    const linked = await coreMutation(api, "patch", "work/projects", { project_ids: [importedResult.projects.at(-1)!.engagement_id], parent_engagement_id: parentId });
    expect(linked.ok(), await linked.text()).toBe(true);
    const repeated = await coreMutation(api, "post", "work/import", lastBatch);
    expect(repeated.ok(), await repeated.text()).toBe(true);
    expect(await repeated.json()).toEqual(importedResult);

    await pairBrowser(page, core, "Work import browser");
    await page.route("**/api/v1/work/agents", (route) => route.fulfill({ json: [{
      session_id: "sample-active-session", engagement_id: parentId,
      title: "Sample research handoff", state: "working", turn_id: "sample-turn",
    }] }));
    await page.goto(`${core.origin}/projects`);
    await expect(page.getByRole("heading", { name: "All projects", exact: true })).toBeVisible();
    const parentProject = page.locator(".work-project-list").getByRole("link", { name: /Sample project 1/ }).first();
    await expect(parentProject).toContainText("1 working, 1 blocked");
    await expect(page.locator(".work-project-list").getByRole("link", { name: /Latest check-in: Review the release plan/ }).first()).toHaveAttribute("href", new RegExp(`/projects/${importedResult.projects.at(-1)!.engagement_id}/work/${importedResult.projects.at(-1)!.items[0].item_id}$`));
    await page.getByRole("button", { name: "Track work" }).click();
    const trackDialog = page.getByRole("dialog", { name: "Track conversation work" });
    await expect(trackDialog).toContainText("Sample project 1 · Sample research handoff");
    await trackDialog.getByRole("button", { name: "Create and link" }).click();
    await expect(trackDialog).toBeHidden({timeout: 20_000});
    type SavedItem = { id: string; title: string; status: string; assignee_session_id: string | null; source_id: string | null };
    const tracked = await (await api.get(`engagements/${parentId}/work`)).json() as SavedItem[];
    const firstItem = tracked.find((item) => item.assignee_session_id === "sample-active-session");
    expect(firstItem?.source_id).toBe("sample-active-session");
    // Finishing the tracked item and tracking the conversation again creates a new open item instead of returning the Done one.
    const finished = await coreMutation(api, "post", `engagements/${parentId}/work/${firstItem!.id}/updates`, { summary: "Handoff finished", status: "done" });
    expect(finished.ok(), await finished.text()).toBe(true);
    await expect.poll(async () => (await (await api.get(`engagements/${parentId}/work/${firstItem!.id}`)).json() as SavedItem).status).toBe("done");
    await page.reload();
    await page.getByRole("button", { name: "Track work" }).click();
    const trackTitle = trackDialog.getByRole("textbox", { name: "Title" });
    await trackTitle.fill("Second research handoff");
    // fill() leaves the replaced text selected, which opens the selected-text toolbar over the dialog; a typing operator's caret closes it.
    await trackTitle.press("End");
    await trackDialog.getByRole("button", { name: "Create and link" }).click();
    await expect(trackDialog).toBeHidden({timeout: 20_000});
    const retracked = await (await api.get(`engagements/${parentId}/work`)).json() as SavedItem[];
    const sessionItems = retracked.filter((item) => item.assignee_session_id === "sample-active-session");
    expect(sessionItems.map((item) => [item.title, item.status]).sort()).toEqual([["Sample research handoff", "done"], ["Second research handoff", "in_progress"]]);
    const trackedItem = sessionItems.find((item) => item.status !== "done");
    await page.reload();
    await page.getByRole("button", { name: "1 Working now" }).click();
    const workingDetails = page.getByRole("region", { name: "Working now details" });
    await expect(workingDetails).toContainText("Sample project 1 · Working · Second research handoff");
    await expect(workingDetails.getByRole("link", { name: "Work item" })).toHaveAttribute("href", new RegExp(`/projects/${parentId}/work/${trackedItem!.id}$`));
    await expect(workingDetails.getByRole("link", { name: /Sample research handoff/ })).toHaveAttribute("href", new RegExp(`/projects/${parentId}/workbench\\?view=chat&session=sample-active-session$`));
    await workingDetails.getByRole("link", { name: /Sample research handoff/ }).click();
    await expect(page).toHaveURL(new RegExp(`/projects/${parentId}/workbench\\?view=chat&session=sample-active-session$`));
    await expect(page.getByRole("main").getByText("Sample research handoff", { exact: true })).toBeVisible({ timeout: 20_000 });
    await expect(page.getByRole("textbox", { name: "Message the analyst assistant" })).toBeVisible();
    await page.goBack();
    await expect(page.getByRole("heading", { name: "All projects", exact: true })).toBeVisible();
    await expect(page.getByText("Showing 80 rows. Search to narrow the list.")).toBeVisible();
    await expect(page.getByRole("button", { name: "Show subprojects of Sample project 1" })).toBeVisible();
    await page.getByRole("searchbox", { name: "Search projects" }).fill("Imported plan");
    await expect(page.locator(".work-project-list").getByRole("link", { name: /Sample project 1/ }).first()).toBeVisible();
    await page.locator(".work-project-list .work-project-primary").filter({ hasText: "Imported plan" }).click();
    await expect(page.getByRole("heading", { name: "Imported plan", exact: true })).toBeVisible();
    await page.goBack();
    await expect(page.getByRole("heading", { name: "All projects", exact: true })).toBeVisible();
    await page.locator(".work-project-list").getByRole("link", { name: /Sample project 1/ }).first().click();
    await expect(page.getByRole("heading", { name: "Sample project 1", exact: true })).toBeVisible();
    await page.getByRole("navigation", { name: "Project sections" }).getByRole("button", { name: "Work" }).click();
    await expect(page.getByRole("heading", { name: "Subprojects" })).toBeVisible();
    await expect(page.getByRole("link", { name: /Imported plan/ })).toBeVisible();
    await page.getByRole("link", { name: /Imported plan/ }).click();
    await expect(page.getByRole("button", { name: "Disable agent tools" })).toBeVisible({ timeout: 20_000 });
    await page.getByRole("button", { name: "1 Blocked" }).click();
    const blockedDetails = page.getByRole("region", { name: "Blocked details" });
    await expect(blockedDetails).toContainText("Imported plan");
    await expect(blockedDetails.getByRole("link", { name: /Review the release plan/ })).toHaveAttribute("href", new RegExp(`/projects/${importedResult.projects.at(-1)!.engagement_id}/work/${importedResult.projects.at(-1)!.items[0].item_id}$`));
    await page.getByRole("button", { name: "New item" }).click();
    const createDialog = page.getByRole("dialog", { name: "New work item" });
    await createDialog.getByRole("textbox", { name: "Title" }).fill("Prepare follow-up");
    await createDialog.getByRole("button", { name: "Create item" }).click();
    await expect(page.getByRole("heading", { name: "Prepare follow-up" })).toBeVisible({timeout: 20_000});
    await page.locator(".work-card").filter({ hasText: "Review the release plan" }).click();
    await expect(page.getByRole("heading", { name: "Review the release plan" })).toBeVisible();
    await expect(page.getByRole("region", { name: "Current progress" })).toContainText("Draft is waiting for approval");
    await expect(page.getByText("Initial plan needs review")).toBeHidden();
    await page.reload();
    await expect(page.getByRole("heading", { name: "Review the release plan" })).toBeVisible();
    await expect(page.getByRole("region", { name: "Current progress" })).toContainText("Approval pending");
    const selected = importedResult.projects.at(-1)!;
    await expect(page.locator(".work-live-state")).toHaveText("Live");
    const liveUpdate = await coreMutation(api, "post", `engagements/${selected.engagement_id}/work/${selected.items[0].item_id}/updates`, { summary: "Reviewer approved the plan", status: "done" });
    expect(liveUpdate.ok(), await liveUpdate.text()).toBe(true);
    await expect(page.getByRole("region", { name: "Current progress" })).toContainText("Reviewer approved the plan");
    await expect(page.locator(".work-timeline")).toContainText("Draft is waiting for approval");
    await page.getByRole("button", { name: "Disable agent tools" }).click();
    await expect(page.getByRole("button", { name: "Enable agent tools" })).toBeVisible();
    await page.reload();
    await expect(page.getByRole("button", { name: "Enable agent tools" })).toBeVisible();
    await expect(page.getByRole("region", { name: "Current progress" })).toContainText("Reviewer approved the plan");
    await expect(page.getByText("Initial plan needs review")).toBeHidden();
    expect(page.url()).toContain(selected.items[0].item_id);
    // Core saves a check-in with a blocker as Blocked whatever status is chosen; the form shows that before saving.
    const updateForm = page.locator(".work-checkin-form");
    const statusSelect = updateForm.getByRole("combobox", { name: "Status" });
    await updateForm.getByRole("textbox", { name: "Progress" }).fill("Release notes need legal review");
    await statusSelect.selectOption("review");
    await updateForm.getByRole("textbox", { name: "Blocker" }).fill("Waiting for legal");
    await expect(statusSelect).toHaveValue("blocked");
    await expect(statusSelect).toBeDisabled();
    await expect(statusSelect).toHaveAccessibleDescription("A blocker saves this update as Blocked.");
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1)).toBe(true);
    expect((await new AxeBuilder({ page }).include(".work-checkin-form").analyze()).violations).toEqual([]);
    await updateForm.getByRole("button", { name: "Save update" }).click();
    await expect(page.getByRole("region", { name: "Current progress" })).toContainText("Blocked: Waiting for legal");
    await expect.poll(async () => (await (await api.get(`engagements/${selected.engagement_id}/work/${selected.items[0].item_id}`)).json() as { status?: string }).status).toBe("blocked");
    await page.reload();
    await expect(page.getByRole("region", { name: "Current progress" })).toContainText("Blocked: Waiting for legal");
    await expect(statusSelect).toHaveValue("blocked");
    await expect(statusSelect).toBeEnabled();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1)).toBe(true);
    const accessibility = await new AxeBuilder({ page }).include(".work-page").analyze();
    expect(accessibility.violations).toEqual([]);
    await testInfo.attach("work-import", { body: JSON.stringify({ origin: core.origin, build: "production", viewport: page.viewportSize(), projectId: selected.engagement_id }), contentType: "application/json" });
  } finally {
    await api.dispose();
    await stopCore(core);
  }
});

async function pairBrowser(page: import("@playwright/test").Page, core: TestCore, name: string): Promise<void> {
  const pairingApi = await playwrightRequest.newContext({ baseURL: `http://127.0.0.1:${new URL(core.origin).port}/api/v1/`, extraHTTPHeaders: { Authorization: `Bearer ${core.token}` } });
  const pairing = await (await pairingApi.post("auth/pairings", { data: { name } })).json() as { secret: string; confirmation_code: string };
  await pairingApi.dispose();
  await page.goto(`${core.origin}/#pair=${encodeURIComponent(pairing.secret)}&code=${encodeURIComponent(pairing.confirmation_code)}`);
  await page.getByLabel("Device name").fill(name);
  // WebKit can report the post-pairing reload without a response object; the paired shell is the evidence.
  await Promise.all([
    page.waitForURL(`${core.origin}/`, { waitUntil: "load", timeout: 20_000 }),
    page.getByRole("button", { name: "Pair device" }).click(),
  ]);
  await expect(page.getByRole("button", { name: "Pair device" })).toHaveCount(0, { timeout: 20_000 });
}

test("work hub stages: define, view gaps, group and move items, resync from import", async ({ page }, testInfo) => {
  test.setTimeout(120_000);
  const core = await startCore();
  const api = await playwrightRequest.newContext({ baseURL: `${core.origin}/api/v1/`, extraHTTPHeaders: { Authorization: `Bearer ${core.token}` } });
  try {
    await waitForCoreApi(api);
    const batch = (stage: string, updateId: string, status: string) => ({ source: "sample-tracker", projects: [
      { external_id: "site", name: "Website refresh", status: "active", items: [
        { external_id: "copy", title: "Home page copy", status: "in_progress", stage, update: { summary: `Now in ${stage}`, status, update_id: updateId } },
        { external_id: "pricing", title: "Pricing table", status: "blocked", stage: "draft" },
      ] },
      { external_id: "docs", name: "Docs cleanup", status: "active" },
    ] });
    const imported = await coreMutation(api, "post", "work/import", batch("draft", "u1", "in_progress"));
    expect(imported.ok(), await imported.text()).toBe(true);
    const result = await imported.json() as { projects: Array<{ engagement_id: string; items: Array<{ item_id: string }> }> };
    const siteId = result.projects[0].engagement_id;
    const copyId = result.projects[0].items[0].item_id;

    await pairBrowser(page, core, "Work stages browser");
    await page.goto(`${core.origin}/projects/${siteId}/work?stages=edit`);
    const editor = page.getByRole("dialog", { name: "Work stages" });
    await expect(editor).toBeVisible({ timeout: 20_000 });
    for (const [index, name] of ["Draft", "Review", "Ship"].entries()) {
      await editor.getByRole("button", { name: "Add stage" }).click();
      await editor.getByRole("textbox", { name: `Stage ${index + 1} name` }).fill(name);
    }
    await editor.getByRole("checkbox").nth(1).check();
    await editor.getByRole("button", { name: "Save stages" }).click();
    await expect(editor).toBeHidden({ timeout: 20_000 });
    type Project = { work_stages: Array<{ key: string; label: string; flag_when_empty: boolean }> };
    await expect.poll(async () => (await (await api.get(`engagements/${siteId}`)).json() as Project).work_stages.map((stage) => [stage.key, stage.flag_when_empty]))
      .toEqual([["draft", false], ["review", true], ["ship", false]]);

    await page.goto(`${core.origin}/projects`);
    const strip = page.getByRole("list", { name: "Website refresh stages" });
    await expect(strip.getByRole("link", { name: "Draft: 2 open, 1 blocked" })).toBeVisible({ timeout: 20_000 });
    await expect(strip.getByRole("link", { name: "Review: none yet" })).toBeVisible();
    await expect(page.locator(".work-project-list").getByRole("link", { name: "Define stages" }).first()).toBeVisible();
    await page.getByRole("button", { name: /Missing a stage/ }).click();
    await expect(page.locator(".work-project-list")).not.toContainText("Docs cleanup");
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1)).toBe(true);
    expect((await new AxeBuilder({ page }).include(".work-projects").analyze()).violations).toEqual([]);

    await strip.getByRole("link", { name: "Draft: 2 open, 1 blocked" }).click();
    await expect(page).toHaveURL(new RegExp(`/projects/${siteId}/work\\?group=stage&stage=draft$`));
    const board = page.getByRole("region", { name: "Project board by stage" });
    await expect(board.locator(".work-column[data-focused]")).toContainText("Home page copy");
    await board.getByRole("link", { name: /Home page copy/ }).click();
    await page.getByRole("combobox", { name: "Stage" }).selectOption("review");
    await expect.poll(async () => (await (await api.get(`engagements/${siteId}/work/${copyId}`)).json() as { stage?: string }).stage).toBe("review");
    await page.reload();
    await expect(page.getByRole("combobox", { name: "Stage" })).toHaveValue("review");
    await expect(board.locator(".work-column").filter({ has: page.getByRole("heading", { name: "Review" }) })).toContainText("Home page copy");

    // The source owns an imported item's stage: a resync moves it, and a new update_id posts another update.
    await expect(page.locator(".work-live-state")).toHaveText("Live");
    const resync = await coreMutation(api, "post", "work/import", batch("ship", "u2", "review"));
    expect(resync.ok(), await resync.text()).toBe(true);
    await expect(page.getByRole("combobox", { name: "Stage" })).toHaveValue("ship", { timeout: 20_000 });
    await expect(page.getByRole("region", { name: "Current progress" })).toContainText("Now in ship");
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1)).toBe(true);
    expect((await new AxeBuilder({ page }).include(".work-page").analyze()).violations).toEqual([]);
    await testInfo.attach("work-stages", { body: JSON.stringify({ origin: core.origin, build: "production", viewport: page.viewportSize(), projectId: siteId }), contentType: "application/json" });
  } finally {
    await api.dispose();
    await stopCore(core);
  }
});

test("work hub stages: subprojects inherit the parent's stages", async ({ page }, testInfo) => {
  test.setTimeout(120_000);
  const core = await startCore();
  const api = await playwrightRequest.newContext({ baseURL: `${core.origin}/api/v1/`, extraHTTPHeaders: { Authorization: `Bearer ${core.token}` } });
  try {
    await waitForCoreApi(api);
    const parent = await coreMutation(api, "post", "work/import", { source: "sample-tracker", projects: [{ external_id: "portfolio", name: "Client portfolio", status: "active" }] });
    expect(parent.ok(), await parent.text()).toBe(true);
    const parentId = (await parent.json() as { projects: Array<{ engagement_id: string }> }).projects[0].engagement_id;
    const children = await coreMutation(api, "post", "work/import", { source: "sample-tracker", projects: [
      { external_id: "site", name: "Website refresh", status: "active", parent_engagement_id: parentId, items: [
        { external_id: "copy", title: "Home page copy", status: "in_progress", stage: "draft" },
      ] },
      { external_id: "docs", name: "Docs cleanup", status: "active", parent_engagement_id: parentId },
    ] });
    expect(children.ok(), await children.text()).toBe(true);
    const childIds = (await children.json() as { projects: Array<{ engagement_id: string; items: Array<{ item_id: string }> }> }).projects;
    const siteId = childIds[0].engagement_id;
    const copyId = childIds[0].items[0].item_id;

    // Stages are saved once, on the parent.
    await pairBrowser(page, core, "Work stage inheritance browser");
    await page.goto(`${core.origin}/projects/${parentId}/work?stages=edit`);
    const editor = page.getByRole("dialog", { name: "Work stages" });
    await expect(editor).toBeVisible({ timeout: 20_000 });
    for (const [index, name] of ["Draft", "Ship"].entries()) {
      await editor.getByRole("button", { name: "Add stage" }).click();
      await editor.getByRole("textbox", { name: `Stage ${index + 1} name` }).fill(name);
    }
    await editor.getByRole("checkbox").first().check();
    await editor.getByRole("button", { name: "Save stages" }).click();
    await expect(editor).toBeHidden({ timeout: 20_000 });

    await page.goto(`${core.origin}/projects`);
    await page.getByRole("button", { name: "Show subprojects of Client portfolio" }).click();
    const site = page.getByRole("list", { name: "Website refresh stages" });
    await expect(site.getByRole("link", { name: /^Draft: 1 open/ })).toBeVisible({ timeout: 20_000 });
    await expect(page.getByRole("list", { name: "Docs cleanup stages" }).getByRole("link", { name: "Draft: none yet" })).toBeVisible();
    await expect(page.getByRole("list", { name: "Client portfolio stages" })).toHaveCount(0);
    await page.getByRole("button", { name: /Missing a stage/ }).click();
    await expect(page.getByRole("list", { name: "Docs cleanup stages" })).toBeVisible();
    await expect(page.getByRole("list", { name: "Client portfolio stages" })).toHaveCount(0);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1)).toBe(true);
    expect((await new AxeBuilder({ page }).include(".work-projects").analyze()).violations).toEqual([]);

    // The subproject's board uses the parent's stages, and moving an item writes only the item.
    await page.goto(`${core.origin}/projects/${siteId}/work/${copyId}?group=stage`);
    const board = page.getByRole("region", { name: "Project board by stage" });
    await expect(board.getByText("Using Client portfolio's stages.")).toBeVisible({ timeout: 20_000 });
    await page.getByRole("combobox", { name: "Stage" }).selectOption("ship");
    // A project read answers 503 "starting" while its worker restarts; keep reading.
    await expect.poll(async () => (await (await api.get(`engagements/${siteId}/work/${copyId}`)).json() as { stage?: string }).stage, { timeout: 30_000 }).toBe("ship");
    expect((await (await api.get(`engagements/${siteId}`)).json() as { work_stages: unknown[] }).work_stages).toEqual([]);
    expect((await new AxeBuilder({ page }).include(".work-page").analyze()).violations).toEqual([]);

    // The parent's page counts its subprojects per stage and lists which reached one; the URL keeps the choice.
    await page.goto(`${core.origin}/projects/${parentId}/work`);
    const rollup = page.getByRole("list", { name: "Subprojects by stage" });
    await expect(rollup.getByRole("link", { name: "Ship: 1 of 2 subprojects, 1 active" })).toBeVisible({ timeout: 20_000 });
    await expect(rollup.getByRole("link", { name: "Draft: 0 of 2 subprojects" })).toContainText("None yet");
    await rollup.getByRole("link", { name: /^Ship:/ }).click();
    const subprojects = page.getByRole("region", { name: "Subprojects" });
    await expect(subprojects.getByRole("link", { name: /Website refresh/ })).toBeVisible();
    await expect(subprojects.getByRole("link", { name: /Docs cleanup/ })).toHaveCount(0);
    await subprojects.getByRole("button", { name: /^Not yet/ }).click();
    await page.reload();
    await expect(page.getByRole("region", { name: "Subprojects" }).getByRole("link", { name: /Docs cleanup/ })).toBeVisible({ timeout: 20_000 });
    await expect(page.getByRole("region", { name: "Subprojects" }).getByRole("link", { name: /Website refresh/ })).toHaveCount(0);
    expect(new URL(page.url()).searchParams.get("substage")).toBe("ship");
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1)).toBe(true);
    expect((await new AxeBuilder({ page }).include(".work-page").analyze()).violations).toEqual([]);
    await testInfo.attach("work-stage-inheritance", { body: JSON.stringify({ origin: core.origin, build: "production", viewport: page.viewportSize(), parentId, siteId }), contentType: "application/json" });
  } finally {
    await api.dispose();
    await stopCore(core);
  }
});

test("work hub board layout keeps readable columns below the toolbar beside an open item", async ({ page }, testInfo) => {
  test.setTimeout(180_000);
  const core = await startCore();
  const api = await playwrightRequest.newContext({ baseURL: `${core.origin}/api/v1/`, extraHTTPHeaders: { Authorization: `Bearer ${core.token}` } });
  try {
    await waitForCoreApi(api);
    const imported = await coreMutation(api, "post", "work/import", { source: "sample-tracker", projects: [
      { external_id: "bounty", name: "Layout bounty", status: "active", items: [
        { external_id: "lanes", title: "Lanes", status: "ready", stage: "recon" },
        { external_id: "candidates", title: "Candidates", status: "blocked", stage: "triage" },
        { external_id: "recon", title: "Recon", status: "done", stage: "recon" },
      ] },
    ] });
    expect(imported.ok(), await imported.text()).toBe(true);
    const result = await imported.json() as { projects: Array<{ engagement_id: string; items: Array<{ item_id: string }> }> };
    const projectId = result.projects[0].engagement_id;
    const candidatesId = result.projects[0].items[1].item_id;
    const project = await (await api.get(`engagements/${projectId}`)).json() as { revision: number };
    const stages = await coreMutation(api, "patch", `engagements/${projectId}/work/stages`, { expected_revision: project.revision, stages: ["Recon", "Triage", "Report", "Submit", "Paid"].map((label) => ({ key: label.toLowerCase(), label, flag_when_empty: false })) });
    expect(stages.ok(), await stages.text()).toBe(true);

    await pairBrowser(page, core, "Work board layout browser");
    // The narrowest board column must still fit a heading, its count and a short title.
    const minimumColumn = 120;
    type Box = { x: number; y: number; width: number; height: number };
    const measure = async (boardName: string) => page.evaluate((name) => {
      const box = (element: Element | null): Box | null => { if (!element) return null; const rect = element.getBoundingClientRect(); return { x: rect.x, y: rect.y, width: rect.width, height: rect.height }; };
      const board = document.querySelector(`section[aria-label="${name}"]`);
      return {
        tools: box(document.querySelector(".work-board-tools")),
        board: box(board),
        detail: box(document.querySelector(".work-detail")),
        layout: box(document.querySelector(".work-project-layout")),
        columns: [...(board?.querySelectorAll(":scope > .work-column") ?? [])].map((column) => box(column) as Box),
        headings: [...(board?.querySelectorAll(":scope > .work-column h2") ?? [])].map((heading) => heading.scrollWidth <= heading.clientWidth + 1),
        pageFits: document.documentElement.scrollWidth <= window.innerWidth + 1,
      };
    }, boardName);
    const expectReadable = async (boardName: string, label: string) => {
      await expect(page.getByRole("region", { name: boardName })).toBeVisible();
      await expect(page.locator(".work-board-tools")).toBeVisible();
      const geometry = await measure(boardName);
      const viewport = page.viewportSize()!;
      expect(geometry.pageFits, `${label}: page scrolls sideways`).toBe(true);
      expect(geometry.tools && geometry.board && geometry.layout, `${label}: layout rendered`).toBeTruthy();
      // The toolbar spans the layout and the board starts below it, at the layout's left edge.
      expect(Math.abs(geometry.tools!.x - geometry.layout!.x), `${label}: toolbar starts at layout edge`).toBeLessThanOrEqual(1);
      expect(Math.abs(geometry.tools!.width - geometry.layout!.width), `${label}: toolbar spans layout`).toBeLessThanOrEqual(1);
      expect(geometry.board!.y, `${label}: board sits below toolbar`).toBeGreaterThanOrEqual(geometry.tools!.y + geometry.tools!.height);
      expect(Math.abs(geometry.board!.x - geometry.layout!.x), `${label}: board starts at layout edge`).toBeLessThanOrEqual(1);
      for (const column of geometry.columns) expect(column.width, `${label}: column width`).toBeGreaterThanOrEqual(minimumColumn);
      expect(geometry.headings.every(Boolean), `${label}: column headings fit`).toBe(true);
      if (geometry.detail) {
        if (viewport.width > 1100) {
          // Beside the board, aligned with it, not under it.
          expect(geometry.detail.x, `${label}: detail beside board`).toBeGreaterThanOrEqual(geometry.board!.x + geometry.board!.width);
          expect(Math.abs(geometry.detail.y - geometry.board!.y), `${label}: detail aligned with board`).toBeLessThanOrEqual(1);
          expect(geometry.board!.width, `${label}: board keeps most of the width`).toBeGreaterThanOrEqual(geometry.layout!.width - geometry.detail.width - 20);
        } else {
          expect(geometry.detail.y, `${label}: detail below board`).toBeGreaterThanOrEqual(geometry.board!.y + geometry.board!.height);
          expect(Math.abs(geometry.detail.width - geometry.layout!.width), `${label}: detail spans layout`).toBeLessThanOrEqual(1);
        }
      }
    };
    const projectWidth = testInfo.project.use.viewport?.width ?? 1440;
    // Desktop projects also check a wide (2000 px) window, where the regression was reported.
    const widths = projectWidth <= 500 ? [projectWidth] : projectWidth >= 1440 ? [2000, projectWidth] : [projectWidth];
    for (const width of widths) {
      await page.setViewportSize({ width, height: width <= 500 ? 844 : 1000 });
      // WebKit can still be finishing the post-pairing reload; that interrupts the first navigation.
      await expect(async () => { await page.goto(`${core.origin}/projects/${projectId}/work`); }).toPass({ timeout: 20_000 });
      const board = page.getByRole("region", { name: "Project board" });
      await expect(board.getByRole("link", { name: /Candidates/ })).toBeVisible({ timeout: 20_000 });
      await expectReadable("Project board", `status ${width}`);
      await testInfo.attach(`status-board-${width}`, { body: await page.screenshot({ fullPage: true }), contentType: "image/png" });

      await board.getByRole("link", { name: /Candidates/ }).click();
      await expect(page).toHaveURL(new RegExp(`/work/${candidatesId}$`));
      await expect(page.getByRole("heading", { name: "Candidates", level: 2 })).toBeVisible();
      await expectReadable("Project board", `status detail ${width}`);
      await testInfo.attach(`status-detail-${width}`, { body: await page.screenshot({ fullPage: true }), contentType: "image/png" });

      await page.getByRole("button", { name: "Stage", exact: true }).click();
      const stageBoard = page.getByRole("region", { name: "Project board by stage" });
      await expect(stageBoard.getByRole("heading", { name: "Paid" })).toBeVisible();
      await expectReadable("Project board by stage", `stage detail ${width}`);
      await testInfo.attach(`stage-detail-${width}`, { body: await page.screenshot({ fullPage: true }), contentType: "image/png" });

      await page.getByRole("link", { name: "Close item" }).click();
      await expect(page.locator(".work-detail")).toHaveCount(0);
      await expectReadable("Project board by stage", `stage ${width}`);
      await testInfo.attach(`stage-board-${width}`, { body: await page.screenshot({ fullPage: true }), contentType: "image/png" });
    }
    expect((await new AxeBuilder({ page }).include(".work-page").analyze()).violations).toEqual([]);
    await testInfo.attach("work-board-layout", { body: JSON.stringify({ origin: core.origin, build: "production", widths, projectId }), contentType: "application/json" });
  } finally {
    await api.dispose();
    await stopCore(core);
  }
});
