import { createHash } from "node:crypto";
import { spawn, type ChildProcessWithoutNullStreams } from "node:child_process";
import { mkdtemp, rm } from "node:fs/promises";
import { homedir, tmpdir } from "node:os";
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
      "--allow-insecure-device-pairing",
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

test("a paired browser can revoke itself without a stale authentication error", async ({ page }) => {
  test.setTimeout(60_000);
  const core = await startRealCore();
  const api = await playwrightRequest.newContext({
    baseURL: `${core.origin}/api/v1/`,
    extraHTTPHeaders: { Authorization: `Bearer ${core.token}` },
  });
  try {
    const deviceName = "Real Core paired browser";
    const pairingResponse = await api.post("auth/pairings", { data: { name: deviceName } });
    expect(pairingResponse.ok(), await pairingResponse.text()).toBe(true);
    const pairing = await pairingResponse.json() as { secret: string; confirmation_code: string };

    await page.goto(`${core.origin}/#pair=${encodeURIComponent(pairing.secret)}&code=${encodeURIComponent(pairing.confirmation_code)}`);
    await page.getByLabel("Device name").fill(deviceName);
    await page.getByRole("button", { name: "Pair device" }).click();
    await expect(page.getByRole("button", { name: "Nebula Core ready" })).toBeVisible({ timeout: 20_000 });

    await page.goto(`${core.origin}/settings#identity-security-settings`);
    await expect(page.getByRole("heading", { name: "Paired devices" })).toBeVisible({ timeout: 20_000 });
    await page.getByRole("button", { name: `Unpair ${deviceName}` }).click();
    const confirmation = page.getByRole("dialog", { name: "Unpair this browser?" });
    await expect(confirmation).toContainText("lose access immediately");
    await confirmation.getByRole("button", { name: "Unpair browser" }).click();

    await expect(page.getByText("Browser session expired")).toBeVisible({ timeout: 20_000 });
    await expect(page.getByText("valid bearer token required", { exact: false })).toHaveCount(0);
    const devicesResponse = await api.get("auth/devices");
    expect(devicesResponse.ok()).toBe(true);
    const devices = await devicesResponse.json() as Array<{ name: string }>;
    expect(devices.some((device) => device.name === deviceName)).toBe(false);
  } finally {
    await api.dispose();
    await stopRealCore(core);
  }
});

test("mobile Code keeps its controls readable and saves to authoritative real-Core state", async ({ page }) => {
  test.setTimeout(60_000);
  const core = await startRealCore();
  const api = await playwrightRequest.newContext({
    baseURL: `${core.origin}/api/v1/`,
    extraHTTPHeaders: { Authorization: `Bearer ${core.token}` },
  });
  try {
    await page.setViewportSize({ width: 390, height: 844 });
    const engagementsResponse = await api.get("engagements");
    expect(engagementsResponse.ok()).toBe(true);
    const engagements = await engagementsResponse.json() as Array<{ id: string }>;
    const projectId = engagements[0]?.id;
    expect(projectId).toBeTruthy();

    await page.goto(`${core.origin}/?view=code#token=${encodeURIComponent(core.token)}`);
    await expect(page.getByRole("navigation", { name: "Mobile operator navigation" })).toBeVisible({ timeout: 20_000 });
    await page.getByRole("button", { name: "New file", exact: true }).first().click();
    await page.getByRole("textbox", { name: "File path" }).fill("mobile-proof.txt");
    await page.getByRole("textbox", { name: "Code editor" }).fill("real Core mobile proof\n");

    const sidebar = page.getByRole("complementary", { name: "Editor files" });
    await expect(sidebar).toBeHidden();
    const controls = await page.locator(".code-editor-toolbar input:visible, .code-editor-toolbar button:visible, .code-editor-toolbar .code-editor-dirty:visible").evaluateAll((elements) => elements.map((element) => {
      const box = element.getBoundingClientRect();
      return { label: element.getAttribute("aria-label") || element.textContent?.trim() || element.tagName, left: box.left, right: box.right, top: box.top, bottom: box.bottom };
    }));
    for (let left = 0; left < controls.length; left += 1) {
      for (let right = left + 1; right < controls.length; right += 1) {
        const a = controls[left];
        const b = controls[right];
        expect(a.left < b.right - 1 && a.right > b.left + 1 && a.top < b.bottom - 1 && a.bottom > b.top + 1, `${a.label} overlaps ${b.label}`).toBe(false);
      }
    }

    await page.getByRole("button", { name: "Save", exact: true }).click();
    await expect(page.getByText("Saved /workspace/mobile-proof.txt. Use it from Terminal when you're ready.")).toBeVisible();
    const listingResponse = await api.get(`engagements/${projectId}/workspace?path=&offset=0&limit=100`);
    expect(listingResponse.ok()).toBe(true);
    expect(JSON.stringify(await listingResponse.json())).toContain("mobile-proof.txt");
    const fileResponse = await api.get(`engagements/${projectId}/workspace/download?path=mobile-proof.txt`);
    expect(fileResponse.ok()).toBe(true);
    expect(await fileResponse.text()).toBe("real Core mobile proof\n");
  } finally {
    await api.dispose();
    await stopRealCore(core);
  }
});

test("real Core persists a project folder chosen through the host browser", async ({ page }) => {
  test.setTimeout(60_000);
  const core = await startRealCore();
  const selectedFolder = await mkdtemp(path.join(homedir(), ".nebula-folder-picker-"));
  const api = await playwrightRequest.newContext({
    baseURL: `${core.origin}/api/v1/`,
    extraHTTPHeaders: { Authorization: `Bearer ${core.token}` },
  });
  try {
    await page.goto(`${core.origin}/settings#token=${encodeURIComponent(core.token)}`);
    await expect(page.getByRole("heading", { name: "Settings", exact: true })).toBeVisible({ timeout: 20_000 });
    await page.getByRole("button", { name: "Switch project" }).click();
    const switcher = page.getByRole("dialog", { name: "Project switcher" });
    await switcher.getByRole("button", { name: "New project" }).click();
    await switcher.getByLabel("Name", { exact: true }).fill("Linked Folder Acceptance");
    await switcher.getByRole("button", { name: "Browse folders" }).click();

    const browser = page.getByRole("dialog", { name: "Choose project folder" });
    await expect(browser).toBeVisible();
    await browser.getByRole("button", { name: path.basename(selectedFolder), exact: true }).click();
    await expect(browser.getByText(selectedFolder, { exact: true })).toBeVisible();
    await browser.getByRole("button", { name: "Select folder" }).click();
    await expect(switcher.getByLabel("Project folder", { exact: true })).toHaveValue(selectedFolder);
    await switcher.getByRole("button", { name: "Create" }).click();
    await expect(page.getByRole("button", { name: "Switch project" })).toContainText("Linked Folder Acceptance");

    const response = await api.get("engagements");
    expect(response.ok()).toBe(true);
    const projects = await response.json() as Array<{ name: string; workspace_path?: string }>;
    expect(projects.find((project) => project.name === "Linked Folder Acceptance")).toMatchObject({
      workspace_path: selectedFolder,
    });

    await page.goto(`${core.origin}/settings#token=${encodeURIComponent(core.token)}`);
    await expect(page.getByRole("button", { name: "Switch project" })).toContainText("Linked Folder Acceptance", { timeout: 20_000 });
  } finally {
    await api.dispose();
    await stopRealCore(core);
    if (path.basename(selectedFolder).startsWith(".nebula-folder-picker-")) {
      await rm(selectedFolder, { recursive: true, force: true });
    }
  }
});

test("clean real Core completes reviewed work and exposes every recovery state", async ({ page }) => {
  const coldImagePreparationTimeout = 60 * 60_000;
  test.setTimeout(coldImagePreparationTimeout + 120_000);
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

    const configuredRuntime = process.env.NEBULA_TEST_CONTAINER_RUNTIME;
    const configuredSocket = process.env.NEBULA_TEST_CONTAINER_SOCKET;
    if (configuredRuntime && configuredSocket) {
      const configuredProfile = await api.put("runner-profiles/local", { data: {
        name: "CI rootless Podman",
        runtime: "podman",
        executable: configuredRuntime,
        context: null,
        socket: configuredSocket,
        platform: process.arch === "arm64" ? "linux/arm64" : "linux/amd64",
        isolation: "rootless",
        enabled: true,
        seccomp_profile: null,
      } });
      expect(configuredProfile.ok(), await configuredProfile.text()).toBe(true);
    }

    // Prepare the shared runtime before opening Workbench. Otherwise its eager
    // starter terminal can legitimately win the preparation request while this
    // acceptance fixture is creating its Project.
    let setupResponse = await api.post("setup/runtime/refresh");
    expect(setupResponse.ok()).toBe(true);
    let setup = await setupResponse.json() as any;
    if (!setup.terminal.runner_profile_id) {
      const candidate = setup.terminal.candidates.find((item: any) =>
        item.healthy && item.candidate_id && (!configuredRuntime || item.executable === configuredRuntime));
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
      }, { timeout: coldImagePreparationTimeout, intervals: [250, 500, 1_000] }).toBe("ready");
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
    await expect.poll(async () => {
      const status = await api.get(`engagements/${projectId}/workspace/reset-status`);
      return (await status.json() as { can_reset: boolean }).can_reset;
    }, { timeout: 30_000, intervals: [250, 500, 1_000] }).toBe(true);
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
