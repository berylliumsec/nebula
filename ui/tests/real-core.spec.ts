import { createHash } from "node:crypto";
import { spawn, type ChildProcessWithoutNullStreams } from "node:child_process";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { expect, request as playwrightRequest, test } from "@playwright/test";

interface RealCore {
  process: ChildProcessWithoutNullStreams;
  dataDir: string;
  origin: string;
  token: string;
}

async function startRealCore(): Promise<RealCore> {
  const repository = path.resolve(import.meta.dirname, "../..");
  const dataDir = await mkdtemp(path.join(tmpdir(), "nebula-playwright-real-core-"));
  const token = "playwright-real-core-token-2026";
  const child = spawn(
    path.join(repository, ".venv/bin/nebula-core"),
    [
      "serve",
      "--host", "127.0.0.1",
      "--port", "0",
      "--token", token,
      "--data-dir", dataDir,
      "--static-dir", path.join(repository, "ui/dist"),
    ],
    { cwd: repository, env: { ...process.env, PYTHONUNBUFFERED: "1" } },
  );
  let output = "";
  const origin = await new Promise<string>((resolve, reject) => {
    const timeout = setTimeout(() => reject(new Error(`Real Core did not become ready.\n${output}`)), 30_000);
    const inspect = (chunk: Buffer) => {
      output += chunk.toString("utf8");
      const match = output.match(/"url"\s*:\s*"(http:\/\/127\.0\.0\.1:\d+)"/);
      if (match) {
        clearTimeout(timeout);
        resolve(match[1]);
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

async function stopRealCore(core: RealCore): Promise<void> {
  if (core.process.exitCode === null) {
    core.process.kill("SIGTERM");
    await Promise.race([
      new Promise<void>((resolve) => core.process.once("exit", () => resolve())),
      new Promise<void>((resolve) => setTimeout(resolve, 5_000)),
    ]);
    if (core.process.exitCode === null) core.process.kill("SIGKILL");
  }
  if (path.basename(core.dataDir).startsWith("nebula-playwright-real-core-")) {
    await rm(core.dataDir, { recursive: true, force: true });
  }
}

test("clean real Core completes reviewed work and exposes every recovery state", async ({ page }) => {
  test.setTimeout(120_000);
  const core = await startRealCore();
  const api = await playwrightRequest.newContext({
    baseURL: `${core.origin}/api/v1/`,
    extraHTTPHeaders: { Authorization: `Bearer ${core.token}` },
  });
  try {
    const bootstrapEngagementsResponse = await api.get("engagements");
    expect(bootstrapEngagementsResponse.ok()).toBe(true);
    const bootstrapEngagements = await bootstrapEngagementsResponse.json() as Array<{ id: string }>;
    expect(bootstrapEngagements[0]?.id).toBeTruthy();

    // Prepare the shared runtime before opening Workbench. Otherwise its eager
    // starter terminal can legitimately win the preparation request while this
    // acceptance fixture is creating its Project.
    let setupResponse = await api.post("setup/runtime/refresh");
    expect(setupResponse.ok()).toBe(true);
    let setup = await setupResponse.json() as any;
    if (!setup.terminal.runner_profile_id) {
      const candidate = setup.terminal.candidates.find((item: any) => item.healthy && item.candidate_id);
      expect(candidate, setup.terminal.detail).toBeTruthy();
      setupResponse = await api.post("setup/runtime/select", { data: { candidate_id: candidate.candidate_id } });
      expect(setupResponse.ok()).toBe(true);
      setup = (await setupResponse.json()).setup;
    }
    if (setup.terminal.image_preparation.phase !== "ready") {
      const prepare = await api.post("setup/image/prepare", { data: { project_id: bootstrapEngagements[0].id } });
      expect(prepare.ok(), await prepare.text()).toBe(true);
      await expect.poll(async () => {
        const status = await api.get("setup/status");
        setup = await status.json();
        if (setup.terminal.image_preparation.phase === "error") {
          throw new Error(setup.terminal.image_preparation.detail ?? "Kali image preparation failed");
        }
        return setup.terminal.image_preparation.phase;
      }, { timeout: 90_000, intervals: [250, 500, 1_000] }).toBe("ready");
    }

    await page.goto(`${core.origin}/#token=${encodeURIComponent(core.token)}`);
    await expect(page.getByRole("tab", { name: "Terminal", exact: true })).toBeVisible({ timeout: 20_000 });
    await expect(page.getByRole("button", { name: "Nebula Core ready" })).toBeVisible({ timeout: 20_000 });
    await expect(page.getByText("Connected", { exact: true })).toBeVisible({ timeout: 30_000 });

    const projectName = "Real Core Project";
    await page.getByRole("button", { name: "Switch project" }).click();
    const switcher = page.getByRole("dialog", { name: "Project switcher" });
    await switcher.getByRole("button", { name: "New project" }).click();
    await switcher.getByLabel("Name", { exact: true }).fill(projectName);
    await switcher.getByRole("button", { name: "Create" }).click();
    await expect(page.getByRole("button", { name: "Switch project" })).toContainText(projectName);

    const engagementsResponse = await api.get("engagements");
    expect(engagementsResponse.ok()).toBe(true);
    const engagements = await engagementsResponse.json() as Array<{ id: string; name: string; scope_policy_id?: string }>;
    const project = engagements.find((item) => item.name === projectName);
    expect(project?.scope_policy_id).toBeTruthy();
    const projectId = project!.id;
    const scopeResponse = await api.get(`engagements/${projectId}/scope`);
    expect(scopeResponse.ok()).toBe(true);
    expect(await scopeResponse.json()).toMatchObject({
      id: project!.scope_policy_id,
      engagement_id: projectId,
      allowed_cidrs: [],
      allowed_domains: [],
      allowed_urls: [],
      allowed_ports: [],
      local_only: false,
      max_concurrency: 1,
    });

    const source = "sleep 5\nprintf 'real-core-ready\\n'\nprintf 'workspace-result\\n' > /workspace/result.txt\n";
    const sourceSha256 = createHash("sha256").update(source).digest("hex");
    const executionRequest = {
      engagement_id: projectId,
      language: "bash",
      source,
      origin: {
        kind: "selection",
        source_kind: "code",
        source_id: "real-core-playwright",
        source_label: "Real Core acceptance",
        source_sha256: sourceSha256,
      },
      network: { mode: "none", ports: [] },
    };
    const preflightResponse = await api.post("executions/preflight", { data: executionRequest });
    expect(preflightResponse.ok()).toBe(true);
    const preflight = await preflightResponse.json();
    expect(preflight, JSON.stringify(preflight)).toMatchObject({ allowed: true, canonical_language: "bash" });
    const startResponse = await api.post("executions", { data: {
      ...executionRequest,
      preview_token: preflight.preview_token,
      preview_fingerprint: preflight.preview_fingerprint,
      client_idempotency_key: "real-core-playwright-1",
    } });
    expect(startResponse.status()).toBe(202);
    const execution = await startResponse.json() as { id: string };

    const blockedStatus = await api.get(`engagements/${projectId}/workspace/reset-status`);
    expect(await blockedStatus.json()).toMatchObject({
      can_reset: false,
      reason_code: "workspace_busy",
      active_execution_count: 1,
    });
    await page.getByRole("tab", { name: "Workspace files", exact: true }).click();
    await expect(page.getByText("Workspace is in use")).toBeVisible();
    await expect(page.getByRole("button", { name: "Reset workspace" })).toBeDisabled();
    await page.getByRole("button", { name: "View Activity" }).click();

    const executionRow = page.locator('aside[aria-label="Execution records"] button').filter({ hasText: "printf 'real-core-ready" });
    await expect(executionRow).toBeVisible({ timeout: 10_000 });
    await executionRow.click();
    await expect(executionRow).toContainText("completed", { timeout: 20_000 });
    await expect(page.locator(".execution-output-grid pre").first()).toContainText("real-core-ready", { timeout: 10_000 });
    const outputResponse = await api.get(`executions/${execution.id}/output/stdout`);
    expect(await outputResponse.text()).toBe("real-core-ready\n");
    expect(outputResponse.headers()["x-nebula-output-next"]).toBe("16");

    await page.getByRole("tab", { name: "Workspace files", exact: true }).click();
    await expect(page.getByText("result.txt", { exact: true })).toBeVisible();
    const recoveredStatusResponse = await api.get(`engagements/${projectId}/workspace/reset-status`);
    const recoveredStatus = await recoveredStatusResponse.json() as { can_reset: boolean; active_terminal_count: number };
    if (recoveredStatus.active_terminal_count > 0) {
      await expect(page.getByText("Workspace is in use")).toBeVisible();
      await page.getByRole("button", { name: "Open Terminal" }).click();
      await page.getByRole("button", { name: /Close Terminal 1/ }).click();
      const stopDialog = page.getByRole("dialog", { name: "Stop Terminal 1?" });
      await stopDialog.getByRole("button", { name: "Stop and close" }).click();
      await page.getByRole("tab", { name: "Workspace files", exact: true }).click();
    } else {
      expect(recoveredStatus.can_reset).toBe(true);
    }
    await expect(page.getByText("Workspace is in use")).toHaveCount(0, { timeout: 10_000 });
    await page.locator(".workspace-reset input").fill(projectName);
    await page.getByRole("button", { name: "Reset workspace" }).click();
    const resetDialog = page.getByRole("dialog", { name: "Reset the project workspace?" });
    await resetDialog.getByRole("button", { name: "Reset workspace" }).click();
    await expect(page.getByText(/Removed 1 workspace entry/)).toBeVisible();

    const diagnostic = await api.post("diagnostics/events", { data: { events: [{
      schema: "nebula.diagnostic/v1",
      level: "error",
      feature: "interface",
      event_code: "interface.real_core.acceptance",
      message: "The real-Core browser acceptance probe was retained.",
      error_id: "err_real_core_playwright",
    }] } });
    expect(diagnostic.ok()).toBe(true);
    const errors = await api.get("diagnostics/errors");
    expect(JSON.stringify(await errors.json())).toContain("err_real_core_playwright");
    const exported = await api.post("diagnostics/export");
    expect(exported.ok()).toBe(true);
    expect(exported.headers()["content-type"]).toContain("application/zip");

    await page.reload();
    await expect(page.getByText("Browser session expired", { exact: true })).toBeVisible();
    await expect(page.getByText(/relaunch the interface with/)).toContainText("nebula-core ui");
    await expect(page.getByRole("button", { name: "Try again" })).toHaveCount(0);
  } finally {
    await api.dispose();
    await stopRealCore(core);
  }
});
