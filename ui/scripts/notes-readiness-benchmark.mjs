import { chromium } from "@playwright/test";
import { execFileSync, spawn } from "node:child_process";
import { existsSync } from "node:fs";
import { mkdtemp, rm } from "node:fs/promises";
import { networkInterfaces, tmpdir } from "node:os";
import path from "node:path";

const repository = path.resolve(import.meta.dirname, "../..");
const count = Number(process.env.NOTES_BENCH_COUNT ?? 300);
const projectCount = Number(process.env.NOTES_BENCH_PROJECT_COUNT ?? 1);
const evidenceDelayMs = Number(process.env.NOTES_BENCH_DELAY_EVIDENCE_MS ?? 0);
const fakeEvidenceCount = Number(process.env.NOTES_BENCH_FAKE_EVIDENCE_COUNT ?? 0);
const settleMs = Number(process.env.NOTES_BENCH_SETTLE_MS ?? 0);
const host = Object.values(networkInterfaces()).flat().find((address) => address?.family === "IPv4" && !address.internal)?.address;
if (!host) throw new Error("A non-loopback IPv4 address is required");
const token = "disposable-notes-readiness-token";
const commonGitDir = path.resolve(repository, execFileSync("git", ["rev-parse", "--git-common-dir"], { cwd: repository, encoding: "utf8" }).trim());
const coreBinary = [
  process.env.NEBULA_TEST_CORE_BIN,
  path.join(repository, ".venv/bin/nebula-core"),
  path.join(path.dirname(commonGitDir), ".venv/bin/nebula-core"),
].find((candidate) => candidate && existsSync(candidate));
if (!coreBinary) throw new Error("A Nebula Core binary is required; set NEBULA_TEST_CORE_BIN");
const dataDir = await mkdtemp(path.join(tmpdir(), "nebula-notes-readiness-"));
const core = spawn(coreBinary, [
  "serve", "--host", "0.0.0.0", "--port", "0", "--token", token,
  "--allow-remote", "--allow-browser-diagnostics", "--allow-insecure-device-pairing",
  "--data-dir", dataDir, "--static-dir", path.join(repository, "ui/dist"),
], {
  cwd: repository,
  env: { ...process.env, PYTHONUNBUFFERED: "1", PYTHONPATH: path.join(repository, "src") },
});
let browser;
try {
  const port = await new Promise((resolve, reject) => {
    let output = "";
    const timeout = setTimeout(() => reject(new Error(`Core startup timed out: ${output}`)), 30_000);
    const read = (chunk) => {
      output += chunk.toString();
      const match = output.match(/"url"\s*:\s*"http:\/\/[^:\"]+:(\d+)"/);
      if (match) { clearTimeout(timeout); resolve(match[1]); }
    };
    core.stdout.on("data", read);
    core.stderr.on("data", read);
    core.once("exit", (code) => { clearTimeout(timeout); reject(new Error(`Core exited ${code}: ${output}`)); });
  });
  const origin = `http://${host}:${port}`;
  const headers = { Authorization: `Bearer ${token}` };
  const api = async (method, route, body) => {
    const response = await fetch(`http://127.0.0.1:${port}/api/v1/${route}`, {
      method, headers: { ...headers, ...(body ? { "Content-Type": "application/json" } : {}) },
      ...(body ? { body: JSON.stringify(body) } : {}),
    });
    if (!response.ok) throw new Error(`${method} ${route}: ${response.status} ${await response.text()}`);
    return response.json();
  };
  const projects = await api("GET", "engagements");
  const projectId = projects[0].id;
  const seededAt = Date.now();
  for (let start = 1; start < projectCount; start += 12) {
    await Promise.all(Array.from({ length: Math.min(12, projectCount - start) }, (_, offset) =>
      api("POST", "engagements", { name: `Disposable project ${String(start + offset).padStart(4, "0")}` }),
    ));
  }
  for (let start = 0; start < count; start += 12) {
    await Promise.all(Array.from({ length: Math.min(12, count - start) }, (_, offset) => {
      const number = start + offset;
      return api("POST", "observations", {
        engagement_id: projectId, observation_type: "note", title: `Bench note ${String(number).padStart(4, "0")}`,
        body: `Disposable note ${number}\n`.repeat(4), source: "operator-note", confidence: 1,
      });
    }));
  }
  browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 }, reducedMotion: "reduce" });
  const browserErrors = [];
  page.on("pageerror", (error) => browserErrors.push(error.message));
  page.on("requestfailed", (request) => browserErrors.push(`${request.method()} ${request.url()}: ${request.failure()?.errorText}`));
  if (evidenceDelayMs || fakeEvidenceCount) {
    const now = new Date().toISOString();
    const fakeEvidence = Array.from({ length: fakeEvidenceCount }, (_, index) => ({
      id: `fake-evidence-${index}`, engagement_id: projectId, evidence_type: "file", title: `Evidence ${index}`,
      captured_at: now, created_at: now, updated_at: now, revision: 1,
    }));
    await page.route("**/api/v1/evidence?*", async (route) => {
      const offset = Number(new URL(route.request().url()).searchParams.get("offset") ?? 0);
      if (evidenceDelayMs && offset === 0) await new Promise((resolve) => setTimeout(resolve, evidenceDelayMs));
      if (fakeEvidenceCount) await route.fulfill({ json: offset === 0 ? fakeEvidence : [] });
      else await route.continue();
    });
  }
  await page.addInitScript((id) => {
    localStorage.setItem("nebula.engagement", id);
    window.__startupBench = { ticks: [], last: performance.now() };
    window.__startupBench.timer = setInterval(() => {
      const now = performance.now();
      window.__startupBench.ticks.push(now - window.__startupBench.last);
      window.__startupBench.last = now;
    }, 16);
  }, projectId);
  const requests = [];
  page.on("requestfinished", (request) => {
    if (new URL(request.url()).pathname.endsWith("/observations")) requests.push({ url: request.url(), at: Date.now() });
  });
  const loadedAt = Date.now();
  await page.goto(`${origin}/?view=chat#token=${encodeURIComponent(token)}`);
  const notesTab = page.getByRole("tab", { name: "Project notes" });
  if (!await notesTab.isVisible()) await page.getByRole("button", { name: "Open top menu" }).click();
  try { await notesTab.waitFor({ state: "visible", timeout: 30_000 }); }
  catch (error) {
    const controls = await page.locator("button, [role=tab]").evaluateAll((elements) => elements.slice(0, 70).map((element) => ({
      text: element.textContent?.trim().slice(0, 70), aria: element.getAttribute("aria-label"),
      title: element.getAttribute("title"), visible: Boolean(element.getClientRects().length),
    })));
    console.error(JSON.stringify({ origin, pageUrl: page.url(), title: await page.title(), body: (await page.locator("body").innerText()).slice(0, 2500), controls, browserErrors }, null, 2));
    throw error;
  }
  const workbenchReadyMs = Date.now() - loadedAt;
  const switches = [];
  for (let index = 0; index < 4; index++) {
    if (index > 0) await page.getByRole("tab", { name: "Analyst chat" }).click();
    await page.evaluate(() => {
      window.__notesBench = { ticks: [], last: performance.now() };
      window.__notesBench.timer = setInterval(() => {
        const now = performance.now();
        window.__notesBench.ticks.push(now - window.__notesBench.last);
        window.__notesBench.last = now;
      }, 16);
    });
    const started = Date.now();
    await notesTab.click();
    await page.getByRole("textbox", { name: "Note body" }).waitFor({ state: "visible", timeout: 30_000 });
    await page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))));
    const ready = Date.now();
    const pageMetrics = await page.evaluate(() => {
      clearInterval(window.__notesBench.timer);
      return {
        maxEventLoopIntervalMs: Math.max(0, ...window.__notesBench.ticks),
        renderedRows: document.querySelectorAll(".notes-list > button").length,
        saveStatus: document.querySelector(".note-save-status")?.textContent,
      };
    });
    switches.push({ elapsedMs: ready - started, ...pageMetrics });
    console.log(`switch ${index + 1}: ${JSON.stringify(switches.at(-1))}`);
  }
  if (process.env.NOTES_BENCH_SCREENSHOT) await page.screenshot({ path: process.env.NOTES_BENCH_SCREENSHOT });
  if (settleMs) await page.waitForTimeout(settleMs);
  const startup = await page.evaluate(() => {
    clearInterval(window.__startupBench.timer);
    return { maxEventLoopIntervalMs: Math.max(0, ...window.__startupBench.ticks), renderedLinkCheckboxes: document.querySelectorAll(".note-editor details input[type=checkbox]").length };
  });
  const build = await (await fetch(`${origin}/web-build.json`)).json();
  console.log(JSON.stringify({ origin, buildCommit: build.commit, builtAt: build.builtAt, noteCount: count, projectCount, evidenceDelayMs, fakeEvidenceCount, seedMs: loadedAt - seededAt, workbenchReadyMs, startup, requests: requests.map((item) => new URL(item.url).search), switches }, null, 2));
} finally {
  if (browser) await browser.close();
  if (core.exitCode === null && core.signalCode === null) {
    const exited = new Promise((resolve) => core.once("exit", resolve));
    core.kill("SIGTERM");
    await Promise.race([exited, new Promise((resolve) => setTimeout(resolve, 5000))]);
    if (core.exitCode === null && core.signalCode === null) core.kill("SIGKILL");
  }
  await rm(dataDir, { recursive: true, force: true });
}
