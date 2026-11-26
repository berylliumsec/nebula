import AxeBuilder from "@axe-core/playwright";
import { createHash } from "node:crypto";
import { spawn, spawnSync, type ChildProcessWithoutNullStreams } from "node:child_process";
import { existsSync } from "node:fs";
import { mkdir, mkdtemp, readFile, readdir, writeFile } from "node:fs/promises";
import { createServer, type Server } from "node:http";
import type { AddressInfo } from "node:net";
import { networkInterfaces, tmpdir } from "node:os";
import path from "node:path";
import { expect, request as playwrightRequest, test, type Page } from "@playwright/test";
import { coreMutation, removeCoreData, waitForCoreApi } from "./fixtures/core-api";
import { removeCoreSockets, stopCoreWorkers } from "./fixtures/core-harness";
import { withOwnedCleanup } from "./fixtures/owned-cleanup";

/*
 * Butler's message queue on a production-built LAN Core. While Butler answers,
 * the panel queues further messages in Core's follow-up queue for Butler's
 * chat. The queue survives a reload; Core sends each message when Butler is
 * free, and the open panel follows that response so its browser actions still
 * apply. This journey lives outside real-core.spec.ts, which is above the
 * public commit guard's per-file size limit.
 */

interface TestCore {
  process: ChildProcessWithoutNullStreams;
  dataDir: string;
  origin: string;
  token: string;
  output?: () => string;
}

interface ModelStub {
  origin: string;
  server: Server;
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
  const dataDir = await mkdtemp(path.join(tmpdir(), "nebula-butler-queue-core-"));
  const token = "butler-queue-test-administration-token";
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
    {name: "failed Butler queue fixture Core", dispose: () => stopCore({process: child, dataDir, origin: "", token, output: () => output})},
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

const PROMPTS = {
  first: "Summarize the synthetic findings.",
  open: "Handle follow-up item seven.",
  dropped: "Handle follow-up item eight.",
  held: "Keep reviewing the long synthetic backlog.",
  urgent: "Handle the urgent synthetic item now.",
  threadHeld: "Walk through every synthetic project slowly.",
  threadUrgent: "Only check the synthetic auth project instead.",
};

/**
 * OpenAI-compatible local model. Butler's answer to the first prompt waits on
 * `release`, so the test can queue messages meanwhile. The queued prompt asks
 * Butler to open Appearance through its settings tool.
 */
async function startModelStub(release: Promise<void>): Promise<ModelStub & {prompts: () => string[]; openCalls: () => number; offered: () => string[]}> {
  const prompts: string[] = [];
  let openCalls = 0;
  let offered: string[] = [];
  const completion = (message: Record<string, unknown>, finish: string) => JSON.stringify({
    id: "chatcmpl-butler-queue", object: "chat.completion", created: 1, model: "security-model",
    choices: [{index: 0, message: {role: "assistant", ...message}, finish_reason: finish}],
    usage: {prompt_tokens: 12, completion_tokens: 4, total_tokens: 16},
  });
  const call = (name: string, args: Record<string, unknown>) => completion({content: null, tool_calls: [{
    id: `call-${name}-${Date.now()}`, type: "function", function: {name, arguments: JSON.stringify(args)},
  }]}, "tool_calls");
  const server = createServer(async (request, response) => {
    response.setHeader("Content-Type", "application/json");
    if (request.method === "GET" && request.url === "/v1/models") {
      response.end(JSON.stringify({object: "list", data: [{id: "security-model", object: "model", created: 1, owned_by: "local-acceptance"}]}));
      return;
    }
    if (request.method !== "POST" || request.url !== "/v1/chat/completions") {
      response.statusCode = 404;
      response.end(JSON.stringify({error: "not found"}));
      return;
    }
    const chunks: Buffer[] = [];
    for await (const chunk of request) chunks.push(Buffer.from(chunk));
    const body = JSON.parse(Buffer.concat(chunks).toString("utf8")) as Record<string, unknown>;
    const tools = Array.isArray(body.tools) ? body.tools as Array<{function?: {name?: string; parameters?: {properties?: {nonce?: {enum?: string[]}}}}}> : [];
    const toolNamed = (prefix: string) => tools.map(tool => tool.function?.name ?? "").find(name => name.startsWith(prefix));
    const nonce = tools.find(tool => tool.function?.name === "nebula_capability_probe")?.function?.parameters?.properties?.nonce?.enum?.[0];
    if (nonce) {
      response.end(call("nebula_capability_probe", {nonce}));
      return;
    }
    const messages = Array.isArray(body.messages) ? body.messages as Array<{role?: string; content?: unknown}> : [];
    const texts = messages.map(message => typeof message.content === "string" ? message.content : JSON.stringify(message.content ?? ""));
    if (texts.some(text => text.includes("Name this conversation from its first exchange"))) {
      response.end(completion({content: "Butler queue"}, "stop"));
      return;
    }
    const synthesis = body.tool_choice === "none";
    const lastUser = messages.map(message => message.role).lastIndexOf("user");
    const prompt = String(messages[lastUser]?.content ?? "");
    const toolReplies = messages.slice(lastUser + 1).filter(message => message.role === "tool").length;
    if (!synthesis && toolReplies === 0) prompts.push(prompt);
    if (prompt.includes(PROMPTS.first) || prompt.includes(PROMPTS.held) || prompt.includes(PROMPTS.threadHeld)) await release;
    if (prompt.includes(PROMPTS.open) && !synthesis) offered = tools.map(tool => tool.function?.name ?? "");
    // Butler reaches browser actions through its capability tools: describe, then change.
    const describe = toolNamed("nebula_capabilities_describe");
    const change = toolNamed("nebula_capabilities_change");
    if (!synthesis && prompt.includes(PROMPTS.open) && describe && change && toolReplies < 2) {
      if (toolReplies === 1) openCalls += 1;
      response.end(toolReplies === 0
        ? call(describe, {operation: "nebula.settings.open"})
        : call(change, {operation: "nebula.settings.open", arguments: {setting_id: "appearance"}}));
      return;
    }
    const finish = toolNamed("finish_response");
    if (!synthesis && finish) {
      response.end(call(finish, {}));
      return;
    }
    response.end(completion({content: prompt.includes(PROMPTS.urgent) ? "Urgent synthetic item handled."
      : prompt.includes(PROMPTS.threadUrgent) ? "Synthetic auth project checked."
      : prompt.includes(PROMPTS.first)
      ? "Synthetic findings: two confirmed, one duplicate."
      : prompt.includes(PROMPTS.open) ? "Opened Appearance settings on your device." : "Done."}, "stop"));
  });
  await new Promise<void>((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", () => resolve());
  });
  return {origin: `http://127.0.0.1:${(server.address() as AddressInfo).port}`, server, prompts: () => prompts, openCalls: () => openCalls, offered: () => offered};
}

async function stopModelStub(stub: ModelStub): Promise<void> {
  stub.server.closeAllConnections();
  await new Promise<void>((resolve, reject) => stub.server.close(error => error ? reject(error) : resolve()));
}

async function pairBrowser(page: Page, core: TestCore, name: string): Promise<void> {
  const pairingApi = await playwrightRequest.newContext({baseURL: `http://127.0.0.1:${new URL(core.origin).port}/api/v1/`, extraHTTPHeaders: {Authorization: `Bearer ${core.token}`}});
  const pairingResponse = await coreMutation(pairingApi, "post", "auth/pairings", {name});
  expect(pairingResponse.ok(), await pairingResponse.text()).toBe(true);
  const pairing = await pairingResponse.json() as {secret: string; confirmation_code: string};
  await pairingApi.dispose();
  await page.goto(`${core.origin}/#pair=${encodeURIComponent(pairing.secret)}&code=${encodeURIComponent(pairing.confirmation_code)}`);
  await page.getByLabel("Device name").fill(name);
  // Pairing clears the fragment before submit; await the replacement document.
  await page.getByRole("button", {name: "Pair device"}).click();
  await expect(page).toHaveURL(`${core.origin}/`, {timeout: 20_000});
  await expect(page.locator(".app-shell")).toBeVisible({timeout: 20_000});
}

async function openButler(page: Page): Promise<void> {
  await expect(page.locator(".app-shell")).toBeVisible();
  const assistant = page.getByRole("dialog", {name: "Butler", exact: true});
  if (await assistant.isVisible()) return;
  const launcherName = /^(Open|Show) Butler(?:;|$)/;
  const mobileLauncher = page.getByRole("navigation", {name: "Mobile operator navigation"}).getByRole("button", {name: launcherName});
  const launcher = page.getByRole("button", {name: launcherName}).first();
  const topMenu = page.getByRole("button", {name: "Open top menu"});
  await expect.poll(async () => await assistant.isVisible() || await mobileLauncher.isVisible()
    || await launcher.isVisible() || await topMenu.isVisible()).toBe(true);
  if (await assistant.isVisible()) return;
  if (await mobileLauncher.isVisible()) {
    await mobileLauncher.click();
    return;
  }
  if (!await launcher.isVisible()) {
    await topMenu.focus();
    await expect(launcher).toBeVisible();
  }
  // Quiet Lens gives its contour pointer events while it has keyboard focus.
  await launcher.focus();
  await launcher.click();
}

test("assistant upgrade Butler queues messages while it works and follows them on production LAN Core", async ({page}, testInfo) => {
  test.setTimeout(360_000);
  let releaseFirst!: () => void;
  const release = new Promise<void>(resolve => { releaseFirst = resolve; });
  const stub = await startModelStub(release);
  let core: TestCore | undefined;
  await withOwnedCleanup(async () => {
    core = await startCore();
    const api = await playwrightRequest.newContext({baseURL: `${core.origin}/api/v1/`, extraHTTPHeaders: {Authorization: `Bearer ${core.token}`}});
    try {
      await waitForCoreApi(api, 90_000);
      const projectResponse = await coreMutation(api, "post", "engagements", {name: "Butler queue fixture", status: "active", metadata: {}});
      expect(projectResponse.ok(), await projectResponse.text()).toBe(true);
      const projectId = (await projectResponse.json() as {id: string}).id;
      const modelResponse = await coreMutation(api, "post", "providers", {name: "Butler queue model", provider_type: "vllm", endpoint: `${stub.origin}/v1`, enabled: true,
        is_local: true, model_allowlist: ["security-model"], privacy: {local_only: true, residency: [], permits_sensitive_data: false},
        metadata: {default_model: "security-model", options: {context_window: 32768}}});
      expect(modelResponse.ok(), await modelResponse.text()).toBe(true);
      const model = await modelResponse.json() as {id: string; revision: number};
      const verified = await coreMutation(api, "post", `providers/${model.id}/capabilities/verify`, {model: "security-model", expected_revision: model.revision});
      expect(verified.ok(), await verified.text()).toBe(true);
      const build = await (await page.request.get(`${core.origin}/web-build.json`)).json() as {assets: Record<string, string>};
      const entry = Object.keys(build.assets).find(asset => asset.startsWith("assets/index-") && asset.endsWith(".js"));
      expect(entry).toBeTruthy();
      expect(createHash("sha256").update(await (await page.request.get(`${core.origin}/${entry}`)).body()).digest("hex")).toBe(build.assets[entry!]);

      await page.addInitScript(id => localStorage.setItem("nebula.engagement", id), projectId);
      await pairBrowser(page, core, "Butler queue LAN browser");
      await page.goto(`${core.origin}/projects/${projectId}/workbench?view=chat`);
      await openButler(page);
      const assistant = page.getByRole("dialog", {name: "Butler", exact: true});
      const transcript = assistant.getByRole("log", {name: "Butler conversation"});
      const composer = assistant.getByRole("textbox", {name: "Ask Butler a question"});
      const queueList = assistant.getByRole("region", {name: "Butler queue"});

      // Butler settings load once the project's worker answers; start from there.
      await assistant.getByRole("button", {name: "Butler settings"}).click();
      await expect(assistant.getByRole("checkbox", {name: /Delegate longer work to subagents/})).toBeVisible({timeout: 90_000});
      await assistant.getByRole("button", {name: "Butler settings"}).click();
      const chatTab = assistant.getByRole("button", {name: "Chat", exact: true});
      if (await chatTab.isVisible()) await chatTab.click();
      await composer.fill(PROMPTS.first);
      await assistant.getByRole("button", {name: "Send to Butler"}).click();
      await expect.poll(stub.prompts, {timeout: 60_000}).toContain(PROMPTS.first);
      // Butler is answering: the composer queues instead of sending.
      await expect(composer).toHaveAttribute("placeholder", /^Queue a message/, {timeout: 30_000});
      await expect(composer).toBeEnabled();
      await composer.fill(PROMPTS.open);
      await assistant.getByRole("button", {name: "Queue message for Butler"}).click();
      await composer.fill(PROMPTS.dropped);
      await composer.press("Enter");
      await expect(queueList.getByRole("status")).toHaveText("2 queued · sent in order when Butler is free", {timeout: 30_000});

      const butler = await (await api.get(`chat/control-assistant/session?engagement_id=${projectId}`)).json() as {id: string};
      type Queue = {paused: boolean; items: Array<{id: string; status: string; request?: {messages: Array<{content: string}>}}>};
      const readQueue = async () => await (await api.get(`chat/sessions/${butler.id}/queue`)).json() as Queue;
      expect((await readQueue()).items.map(item => item.request?.messages[0]?.content)).toEqual([PROMPTS.open, PROMPTS.dropped]);

      // The queue is Core's: it survives a reload of this tab.
      await page.reload();
      await openButler(page);
      await expect(queueList.getByText(PROMPTS.dropped)).toBeVisible({timeout: 60_000});
      await queueList.getByRole("button", {name: "Remove queued message 2"}).click();
      await expect(queueList.getByRole("status")).toHaveText("1 queued · sent in order when Butler is free");
      expect((await readQueue()).items.filter(item => item.status === "queued").map(item => item.request?.messages[0]?.content)).toEqual([PROMPTS.open]);

      releaseFirst();
      // Core sends the queued message next; the panel follows it and applies
      // its browser action on this device.
      await expect.poll(stub.openCalls, {timeout: 90_000, message: `settings tool not called; offered: ${stub.offered().join(", ")}`}).toBe(1);
      const appearance = page.getByRole("dialog", {name: "Appearance and theme"});
      await expect(appearance).toBeVisible({timeout: 90_000});
      await page.keyboard.press("Escape");
      await expect(appearance).toHaveCount(0);
      await openButler(page);
      // Butler's saved transcript holds both answers in order.
      await expect(transcript.getByText("Synthetic findings: two confirmed, one duplicate.")).toBeVisible({timeout: 90_000});
      await expect(transcript.getByText("Opened Appearance settings on your device.")).toBeVisible({timeout: 90_000});
      await expect(transcript.getByText(PROMPTS.open)).toBeVisible();
      await expect(queueList).toHaveCount(0, {timeout: 30_000});
      expect(stub.prompts().filter(prompt => prompt.includes(PROMPTS.dropped))).toEqual([]);
      // Core settles the sent item shortly after its turn ends.
      await expect.poll(async () => (await readQueue()).items.filter(item => !["complete", "cancelled"].includes(item.status)).length, {timeout: 30_000}).toBe(0);

      expect((await new AxeBuilder({page}).include('[role="dialog"][aria-label="Butler"]').analyze()).violations).toEqual([]);
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1)).toBe(true);
      await testInfo.attach("butler-queue-lan", {body: JSON.stringify({origin: core.origin, build: "production", project: testInfo.project.name,
        viewport: page.viewportSize(), asset: entry, assetSha256: build.assets[entry!],
        result: "While Butler answered, the panel queued two messages in Core; the queue survived a reload; a removed message was never sent; Core sent the remaining message after Butler's reply, and the panel followed it, applied its open-setting browser action and showed the answer."}), contentType: "application/json"});
    } finally {
      await api.dispose();
    }
  }, [
    {name: "release held Butler answer", dispose: async () => { releaseFirst(); }},
    {name: "model stub", dispose: () => stopModelStub(stub)},
    {name: "Butler queue fixture Core", dispose: async () => { if (core) await stopCore(core); }},
  ]);
});

test("assistant upgrade Send now interrupts Butler in a Butler mode thread on production LAN Core", async ({page}, testInfo) => {
  test.setTimeout(360_000);
  let releaseHeld!: () => void;
  const release = new Promise<void>(resolve => { releaseHeld = resolve; });
  const stub = await startModelStub(release);
  let core: TestCore | undefined;
  await withOwnedCleanup(async () => {
    core = await startCore();
    const api = await playwrightRequest.newContext({baseURL: `${core.origin}/api/v1/`, extraHTTPHeaders: {Authorization: `Bearer ${core.token}`}});
    try {
      await waitForCoreApi(api, 90_000);
      const projectResponse = await coreMutation(api, "post", "engagements", {name: "Send now fixture", status: "active", metadata: {}});
      expect(projectResponse.ok(), await projectResponse.text()).toBe(true);
      const projectId = (await projectResponse.json() as {id: string}).id;
      const modelResponse = await coreMutation(api, "post", "providers", {name: "Send now model", provider_type: "vllm", endpoint: `${stub.origin}/v1`, enabled: true,
        is_local: true, model_allowlist: ["security-model"], privacy: {local_only: true, residency: [], permits_sensitive_data: false},
        metadata: {default_model: "security-model", options: {context_window: 32768}}});
      expect(modelResponse.ok(), await modelResponse.text()).toBe(true);
      const model = await modelResponse.json() as {id: string; revision: number};
      const verified = await coreMutation(api, "post", `providers/${model.id}/capabilities/verify`, {model: "security-model", expected_revision: model.revision});
      expect(verified.ok(), await verified.text()).toBe(true);
      // The project's Butler, set up as an operator does once from the Butler panel.
      const butler = await coreMutation(api, "post", "chat/control-assistant/session", {engagement_id: projectId, backend: "provider", provider_id: model.id, model: "security-model"});
      expect(butler.ok(), await butler.text()).toBe(true);
      type Queue = {paused: boolean; items: Array<{id: string; status: string; request?: {messages: Array<{content: string}>}}>};
      const readQueue = async (sessionId: string) => await (await api.get(`chat/sessions/${sessionId}/queue`)).json() as Queue;

      await page.addInitScript(id => localStorage.setItem("nebula.engagement", id), projectId);
      await pairBrowser(page, core, "Send now LAN browser");

      // Butler mode: a reply to a thread Butler is still answering queues, then Send now interrupts.
      await page.goto(`${core.origin}/projects/${projectId}/workbench?view=chat`);
      // Entry: the Butler launcher opens Butler mode.
      await openButler(page);
      await expect(page).toHaveURL(`${core.origin}/butler?project=${projectId}`);
      const butlerComposer = page.getByRole("textbox", {name: "Message Butler"});
      // On a phone the board and threads are tabs.
      const threadsTab = page.getByRole("tab", {name: /^Threads/});
      await expect(threadsTab.or(butlerComposer).first()).toBeVisible({timeout: 30_000});
      if (await threadsTab.isVisible()) await threadsTab.click();
      await expect(butlerComposer).toBeEditable({timeout: 60_000});
      await butlerComposer.fill(PROMPTS.threadHeld);
      await butlerComposer.press("Enter");
      await expect.poll(stub.prompts, {timeout: 120_000}).toContainEqual(expect.stringContaining(PROMPTS.threadHeld));
      const thread = new URL(page.url()).searchParams.get("thread");
      expect(thread).toBeTruthy();
      // A new thread opens with the composer set to start another; reply in this one.
      await page.getByRole("button", {name: "Reply in this thread"}).click();
      await expect(page.getByRole("button", {name: "Stop response"})).toBeVisible({timeout: 30_000});
      await butlerComposer.fill(PROMPTS.threadUrgent);
      await page.getByRole("button", {name: "Queue reply"}).click();
      const threadQueue = page.getByRole("region", {name: "Thread queue"});
      await expect(threadQueue.getByText(PROMPTS.threadUrgent)).toBeVisible({timeout: 30_000});
      expect((await readQueue(thread!)).items.map(item => item.request?.messages[0]?.content.includes(PROMPTS.threadUrgent))).toEqual([true]);
      await threadQueue.getByRole("button", {name: "Send queued message 1 now"}).click();
      await expect.poll(stub.prompts, {timeout: 90_000}).toContainEqual(expect.stringContaining(PROMPTS.threadUrgent));
      await expect(page.getByRole("log", {name: "Butler thread messages"}).getByText("Synthetic auth project checked.")).toBeVisible({timeout: 90_000});
      await expect(threadQueue).toHaveCount(0, {timeout: 30_000});
      expect((await readQueue(thread!)).paused).toBe(false);

      expect((await new AxeBuilder({page}).include('section[aria-label^="Thread:"]').analyze()).violations).toEqual([]);
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1)).toBe(true);
      await testInfo.attach("butler-send-now-lan", {body: JSON.stringify({origin: core.origin, build: "production", project: testInfo.project.name, viewport: page.viewportSize(),
        result: "In Butler mode a reply to a thread Butler was still answering queued in Core; Send now on the queued reply stopped the answer, and Core sent the reply next without pausing the queue."}), contentType: "application/json"});
    } finally {
      await api.dispose();
    }
  }, [
    {name: "release held Butler answers", dispose: async () => { releaseHeld(); }},
    {name: "model stub", dispose: () => stopModelStub(stub)},
    {name: "Send now fixture Core", dispose: async () => { if (core) await stopCore(core); }},
  ]);
});
