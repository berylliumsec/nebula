import { expect, test as base, type Page } from "@playwright/test";
import { type ChildProcessWithoutNullStreams, spawn } from "node:child_process";
import { mkdir, mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";

export interface UsageCore {
  process: ChildProcessWithoutNullStreams;
  dataDir: string;
  origin: string;
  token: string;
}

interface CoreRequest {
  method?: "GET" | "POST" | "PATCH" | "DELETE";
  body?: unknown;
}

interface WorkerFixtures {
  core: UsageCore;
}

const repository = path.resolve(import.meta.dirname, "../../..");
const videoDirectory = path.join(repository, "ui", "usage-videos");

async function startCore(): Promise<UsageCore> {
  const dataDir = await mkdtemp(path.join(tmpdir(), "nebula-usage-videos-"));
  const token = "nebula-playwright-usage-scenarios";
  const child = spawn(
    path.join(repository, ".venv", "bin", "nebula-core"),
    [
      "serve",
      "--host", "127.0.0.1",
      "--port", "0",
      "--token", token,
      "--data-dir", dataDir,
      "--static-dir", path.join(repository, "ui", "dist"),
    ],
    { cwd: repository, env: { ...process.env, PYTHONUNBUFFERED: "1" } },
  );
  let output = "";
  const origin = await new Promise<string>((resolve, reject) => {
    const timeout = setTimeout(() => reject(new Error(`Nebula Core did not become ready.\n${output}`)), 30_000);
    const inspect = (chunk: Buffer) => {
      output += chunk.toString("utf8");
      const match = output.match(/"url"\s*:\s*"(http:\/\/127\.0\.0\.1:\d+)"/);
      if (!match) return;
      clearTimeout(timeout);
      resolve(match[1]);
    };
    child.stdout.on("data", inspect);
    child.stderr.on("data", (chunk) => { output += chunk.toString("utf8"); });
    child.once("exit", (code) => {
      clearTimeout(timeout);
      reject(new Error(`Nebula Core exited with ${code}.\n${output}`));
    });
  });
  return { process: child, dataDir, origin, token };
}

async function stopCore(core: UsageCore): Promise<void> {
  if (core.process.exitCode === null) {
    core.process.kill("SIGTERM");
    await Promise.race([
      new Promise<void>((resolve) => core.process.once("exit", () => resolve())),
      new Promise<void>((resolve) => setTimeout(resolve, 5_000)),
    ]);
    if (core.process.exitCode === null) core.process.kill("SIGKILL");
  }
  if (path.basename(core.dataDir).startsWith("nebula-usage-videos-")) {
    await rm(core.dataDir, { recursive: true, force: true });
  }
}

function videoName(title: string): string {
  return `${title.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "")}.webm`;
}

export const test = base.extend<Record<string, never>, WorkerFixtures>({
  core: [async ({}, use) => {
    const core = await startCore();
    try {
      await use(core);
    } finally {
      await stopCore(core);
    }
  }, { scope: "worker" }],
  page: async ({ browser }, use, testInfo) => {
    const rawVideoDirectory = testInfo.outputPath("raw-video");
    await mkdir(rawVideoDirectory, { recursive: true });
    await mkdir(videoDirectory, { recursive: true });
    const context = await browser.newContext({
      acceptDownloads: true,
      colorScheme: "dark",
      locale: "en-US",
      reducedMotion: "reduce",
      recordVideo: {
        dir: rawVideoDirectory,
        size: { width: 1440, height: 900 },
      },
      timezoneId: "America/New_York",
      viewport: { width: 1440, height: 900 },
    });
    const page = await context.newPage();
    await page.addInitScript(() => {
      localStorage.setItem("nebula.theme", "zero");
      localStorage.setItem("nebula.conversations.expanded", "true");
    });
    try {
      await use(page);
    } finally {
      const video = page.video();
      await context.close();
      if (video) await video.saveAs(path.join(videoDirectory, videoName(testInfo.title)));
    }
  },
});

export { expect };

export async function coreJson<T = Record<string, unknown>>(
  core: UsageCore,
  endpoint: string,
  request: CoreRequest = {},
): Promise<T> {
  const response = await fetch(`${core.origin}/api/v1/${endpoint.replace(/^\//, "")}`, {
    method: request.method ?? "GET",
    headers: {
      Authorization: `Bearer ${core.token}`,
      ...(request.body === undefined ? {} : { "Content-Type": "application/json" }),
    },
    body: request.body === undefined ? undefined : JSON.stringify(request.body),
  });
  if (!response.ok) {
    throw new Error(`${request.method ?? "GET"} ${endpoint} returned ${response.status}: ${await response.text()}`);
  }
  if (response.status === 204) return undefined as T;
  return await response.json() as T;
}

export async function createScenarioProject(
  core: UsageCore,
  suffix: string,
): Promise<Record<string, unknown> & { id: string; name: string }> {
  return coreJson(core, "engagements", {
    method: "POST",
    body: {
      name: `Northstar Commerce ${suffix}`,
      client_name: "Northstar Retail",
      description: "Authorized assessment of the Northstar Commerce test API.",
      status: "active",
      tags: ["authorized", "api", "test-environment"],
      metadata: { scenario: "playwright-usage-video" },
    },
  });
}

export async function openProject(
  page: Page,
  core: UsageCore,
  project: { id: string; name: string },
  route = "/",
): Promise<void> {
  await page.addInitScript((projectId) => localStorage.setItem("nebula.engagement", projectId), project.id);
  await page.goto(`${core.origin}${route}#token=${encodeURIComponent(core.token)}`);
  await expect(page.getByRole("button", { name: "Switch project" })).toContainText(project.name, { timeout: 20_000 });
  await expect(page.getByRole("button", { name: "Nebula Core ready" })).toBeVisible({ timeout: 20_000 });
  await page.evaluate(() => document.fonts.ready);
}

export async function beat(page: Page, milliseconds = 650): Promise<void> {
  await page.waitForTimeout(milliseconds);
}

export async function seedAsset(core: UsageCore, projectId: string) {
  return coreJson<Record<string, unknown> & { id: string }>(core, "assets", {
    method: "POST",
    body: {
      engagement_id: projectId,
      asset_type: "domain",
      name: "api.northstar.test",
      hostname: "api.northstar.test",
      address: null,
      criticality: "critical",
      exposed: true,
      tags: ["api", "staging", "authorized"],
      metadata: { service_count: 2, last_seen_at: "2026-07-18T14:15:00Z" },
    },
  });
}

export async function seedEvidence(core: UsageCore, projectId: string, assetId: string) {
  const content = JSON.stringify({
    target: "api.northstar.test",
    observed_at: "2026-07-18T14:15:00Z",
    tls_versions: ["TLSv1.1", "TLSv1.2", "TLSv1.3"],
    certificate_days_remaining: 41,
  }, null, 2);
  return coreJson<Record<string, unknown> & { id: string }>(core, "evidence/upload", {
    method: "POST",
    body: {
      engagement_id: projectId,
      filename: "northstar-tls-observation.json",
      title: "TLS protocol observation",
      evidence_type: "scanner_output",
      content_base64: Buffer.from(content).toString("base64"),
      media_type: "application/json",
      description: "Repeatable protocol inventory captured from the authorized test endpoint.",
      source: "operator_upload",
      asset_ids: [assetId],
      source_context: { target: "api.northstar.test", environment: "test" },
      metadata: { scenario: "playwright-usage-video" },
    },
  });
}

export async function seedFinding(
  core: UsageCore,
  projectId: string,
  assetId: string,
  evidenceId?: string,
) {
  const finding = await coreJson<Record<string, unknown> & { id: string; revision: number }>(core, "findings", {
    method: "POST",
    body: {
      engagement_id: projectId,
      title: "Deprecated TLS protocol remains enabled",
      description: "The authorized test endpoint still negotiates TLS 1.1.",
      status: "candidate",
      severity: "medium",
      severity_rationale: "Legacy protocol support weakens transport security for external clients.",
      asset_ids: [assetId],
      cve_ids: [],
      cwe_ids: ["CWE-326"],
      metadata: { origin: "manual_operator_entry" },
    },
  });
  if (!evidenceId) return finding;
  return coreJson<Record<string, unknown> & { id: string; revision: number }>(core, `findings/${finding.id}`, {
    method: "PATCH",
    body: {
      expected_revision: finding.revision,
      changes: { evidence_ids: [evidenceId], status: "validated" },
    },
  });
}

export async function seedNote(core: UsageCore, projectId: string, assetId: string, evidenceId: string) {
  return coreJson<Record<string, unknown> & { id: string }>(core, "observations", {
    method: "POST",
    body: {
      engagement_id: projectId,
      observation_type: "note",
      title: "Transport review notes",
      body: "TLS 1.1 was reproducibly negotiated on the test endpoint. Confirm the retirement plan with the platform owner.",
      asset_ids: [assetId],
      service_ids: [],
      evidence_ids: [evidenceId],
      source: "operator-note",
      confidence: 0.95,
      metadata: { scenario: "playwright-usage-video" },
    },
  });
}
