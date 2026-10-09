import AxeBuilder from "@axe-core/playwright";
import { spawn, spawnSync, type ChildProcessWithoutNullStreams } from "node:child_process";
import { existsSync } from "node:fs";
import { mkdtemp, rm } from "node:fs/promises";
import { networkInterfaces, tmpdir } from "node:os";
import path from "node:path";
import { expect, request as playwrightRequest, test, type Page } from "@playwright/test";

interface TestCore {
  process: ChildProcessWithoutNullStreams;
  dataDir: string;
  origin: string;
  token: string;
}

function localNetworkIpv4(): string {
  for (const addresses of Object.values(networkInterfaces())) {
    for (const address of addresses ?? []) {
      if (address.family === "IPv4" && !address.internal) return address.address;
    }
  }
  throw new Error("A non-loopback IPv4 address is required for LAN acceptance.");
}

function coreReady(page: Page) {
  return page.getByRole("button", { name: /^Nebula Core ready$/ })
    .or(page.getByRole("navigation", { name: "Mobile operator navigation" })).first();
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
  const token = "work-import-test-token";
  const child = spawn(binary, [
    "serve", "--host", "0.0.0.0", "--port", "0", "--token", token,
    "--allow-remote", "--allow-insecure-device-pairing", "--allow-browser-diagnostics",
    "--data-dir", dataDir, "--static-dir", path.join(repository, "ui/dist"),
  ], {
    cwd: repository,
    env: { ...process.env, PYTHONUNBUFFERED: "1", PYTHONPATH: [path.join(repository, "src"), process.env.PYTHONPATH].filter(Boolean).join(path.delimiter) },
  });
  let output = "";
  const origin = await new Promise<string>((resolve, reject) => {
    const timeout = setTimeout(() => reject(new Error(`Real Core did not become ready.\n${output}`)), 30_000);
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
  });
  return { process: child, dataDir, origin, token };
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
  await rm(core.dataDir, { recursive: true, force: true });
}

test("work hub import finds a project and its saved task after refresh", async ({ page }, testInfo) => {
  test.setTimeout(120_000);
  const core = await startCore();
  const api = await playwrightRequest.newContext({ baseURL: `${core.origin}/api/v1/`, extraHTTPHeaders: { Authorization: `Bearer ${core.token}` } });
  try {
    const projects = Array.from({ length: 80 }, (_, index) => ({
      external_id: `sample-${index + 1}`, name: `Sample project ${index + 1}`,
    }));
    const first = await api.post("work/import", { data: { source: "sample-tracker", projects: projects.slice(0, 50) } });
    expect(first.ok(), await first.text()).toBe(true);
    const lastBatch = { source: "sample-tracker", projects: [
      ...projects.slice(50),
      { external_id: "imported-plan", name: "Imported plan", status: "active", items: [{
        external_id: "review-task", title: "Review the release plan", description: "Initial plan needs review", status: "blocked",
        update: { summary: "Draft is waiting for approval", blocker: "Approval pending", next_step: "Ask the reviewer" },
      }] },
    ] };
    const imported = await api.post("work/import", { data: lastBatch });
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
      "store = NebulaStore(Path(sys.argv[1]) / 'nebula.db')",
      "store.create(ChatSession(id='sample-active-session', engagement_id=sys.argv[2], title='Sample research handoff', model='test-model', provider_profile_id='provider-1'))",
    ].join("\n"), core.dataDir, parentId], { cwd: repository, env: { ...process.env, PYTHONPATH: path.join(repository, "src") }, encoding: "utf8" });
    expect(seeded.status, seeded.stderr || seeded.stdout).toBe(0);
    const linked = await api.patch("work/projects", { data: { project_ids: [importedResult.projects.at(-1)!.engagement_id], parent_engagement_id: parentId } });
    expect(linked.ok(), await linked.text()).toBe(true);
    const repeated = await api.post("work/import", { data: lastBatch });
    expect(repeated.ok(), await repeated.text()).toBe(true);
    expect(await repeated.json()).toEqual(importedResult);

    const pairingApi = await playwrightRequest.newContext({ baseURL: `http://127.0.0.1:${new URL(core.origin).port}/api/v1/`, extraHTTPHeaders: { Authorization: `Bearer ${core.token}` } });
    const pairing = await (await pairingApi.post("auth/pairings", { data: { name: "Work import browser" } })).json() as { secret: string; confirmation_code: string };
    await pairingApi.dispose();
    await page.goto(`${core.origin}/#pair=${encodeURIComponent(pairing.secret)}&code=${encodeURIComponent(pairing.confirmation_code)}`);
    await page.getByLabel("Device name").fill("Work import browser");
    await page.getByRole("button", { name: "Pair device" }).click();
    await expect(coreReady(page)).toBeVisible({ timeout: 20_000 });
    await page.route("**/api/v1/work/agents", (route) => route.fulfill({ json: [{
      session_id: "sample-active-session", engagement_id: parentId,
      title: "Sample research handoff", state: "working", turn_id: "sample-turn",
    }] }));
    const mobileMore = page.getByRole("button", { name: "More workbench views" });
    if (await mobileMore.isVisible()) {
      await mobileMore.click();
      await page.getByRole("dialog", { name: "More" }).getByRole("button", { name: "Projects" }).click();
    } else {
      const sidebar = page.getByRole("button", { name: "Show sidebar" });
      if (await sidebar.isVisible()) await sidebar.click();
      await page.getByRole("link", { name: "Projects", exact: true }).click();
    }
    await page.goto(`${core.origin}/projects`);
    await expect(page.getByRole("heading", { name: "All projects", exact: true })).toBeVisible();
    const parentProject = page.locator(".work-project-list").getByRole("link", { name: /Sample project 1/ }).first();
    await expect(parentProject).toContainText("1 working · 0 waiting · 0 in progress · 1 blocked · 0 review");
    await expect(page.locator(".work-project-list").getByRole("link", { name: /Latest check-in: Review the release plan/ }).first()).toHaveAttribute("href", new RegExp(`/projects/${importedResult.projects.at(-1)!.engagement_id}/work/${importedResult.projects.at(-1)!.items[0].item_id}$`));
    await page.getByRole("button", { name: "Track work" }).click();
    const trackDialog = page.getByRole("dialog", { name: "Track conversation work" });
    await expect(trackDialog).toContainText("Sample project 1 · Sample research handoff");
    await trackDialog.getByRole("button", { name: "Create and link" }).click();
    await expect(trackDialog).toBeHidden();
    const tracked = await (await api.get(`engagements/${parentId}/work`)).json() as Array<{ id: string; assignee_session_id: string | null; source_id: string | null }>;
    const trackedItem = tracked.find((item) => item.assignee_session_id === "sample-active-session");
    expect(trackedItem?.source_id).toBe("sample-active-session");
    await page.reload();
    await page.getByRole("button", { name: "1 Working now" }).click();
    const workingDetails = page.getByRole("region", { name: "Working now details" });
    await expect(workingDetails).toContainText("Sample project 1 · Working · Sample research handoff");
    await expect(workingDetails.getByRole("link", { name: "Work item" })).toHaveAttribute("href", new RegExp(`/projects/${parentId}/work/${trackedItem!.id}$`));
    await expect(workingDetails.getByRole("link", { name: /Sample research handoff/ })).toHaveAttribute("href", new RegExp(`/projects/${parentId}/workbench\\?view=chat&session=sample-active-session$`));
    await workingDetails.getByRole("link", { name: /Sample research handoff/ }).click();
    await expect(page).toHaveURL(new RegExp(`/projects/${parentId}/workbench\\?view=chat&session=sample-active-session$`));
    const desktopChat = page.getByRole("tab", { name: "Analyst chat" });
    const mobileChat = page.getByRole("button", { name: "Chat", exact: true });
    await expect(desktopChat.or(mobileChat).first()).toBeVisible({ timeout: 20_000 });
    if (await desktopChat.isVisible()) await expect(desktopChat).toHaveAttribute("aria-selected", "true");
    else await expect(mobileChat).toHaveAttribute("aria-current", "page");
    await page.goBack();
    await expect(page.getByRole("heading", { name: "All projects", exact: true })).toBeVisible();
    await expect(page.getByText("Showing 80 rows. Search to narrow the list.")).toBeVisible();
    await expect(page.getByRole("button", { name: "Show subprojects of Sample project 1" })).toBeVisible();
    await page.getByRole("searchbox", { name: "Search projects" }).fill("Imported plan");
    await expect(page.locator(".work-project-list").getByRole("link", { name: /Sample project 1/ })).toBeVisible();
    await page.locator(".work-project-list .work-project-primary").filter({ hasText: "Imported plan" }).click();
    await expect(page.getByRole("link", { name: "Sample project 1" })).toBeVisible({ timeout: 20_000 });
    await page.getByRole("link", { name: "Sample project 1" }).click();
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
    await expect(page.getByRole("heading", { name: "Prepare follow-up" })).toBeVisible();
    await page.locator(".work-card").filter({ hasText: "Review the release plan" }).click();
    await expect(page.getByRole("heading", { name: "Review the release plan" })).toBeVisible();
    await expect(page.getByRole("region", { name: "Current progress" })).toContainText("Draft is waiting for approval");
    await expect(page.getByText("Initial plan needs review")).toBeHidden();
    await page.reload();
    await expect(page.getByRole("heading", { name: "Review the release plan" })).toBeVisible();
    await expect(page.getByRole("region", { name: "Current progress" })).toContainText("Approval pending");
    const selected = importedResult.projects.at(-1)!;
    await expect(page.locator(".work-live-state")).toHaveText("Live");
    const liveUpdate = await api.post(`engagements/${selected.engagement_id}/work/${selected.items[0].item_id}/updates`, { data: { summary: "Reviewer approved the plan", status: "done" } });
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
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1)).toBe(true);
    const accessibility = await new AxeBuilder({ page }).include(".work-page").analyze();
    expect(accessibility.violations).toEqual([]);
    await testInfo.attach("work-import", { body: JSON.stringify({ origin: core.origin, build: "production", viewport: page.viewportSize(), projectId: selected.engagement_id }), contentType: "application/json" });
  } finally {
    await api.dispose();
    await stopCore(core);
  }
});
