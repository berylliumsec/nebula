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
 * Butler subagents on a production-built LAN Core. Butler delegates a longer
 * request to a Butler-mode subagent and replies without waiting, so a second
 * request is answered while the child still works. The child's report then
 * appears in the open Butler panel as Core posts it. This journey lives
 * outside real-core.spec.ts, which is above the public commit guard's
 * per-file size limit.
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
  const dataDir = await mkdtemp(path.join(tmpdir(), "nebula-butler-subagents-core-"));
  const token = "butler-subagents-test-administration-token";
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
    {name: "failed Butler subagents fixture Core", dispose: () => stopCore({process: child, dataDir, origin: "", token, output: () => output})},
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

const CHILD_MARKER = "You are a subagent.";
const PROMPTS = {
  delegate: "Pause the synthetic goals and report back.",
  meanwhile: "Which project is this?",
};

function messageText(body: Record<string, unknown>): string[] {
  const messages = Array.isArray(body.messages) ? body.messages as Array<{role?: string; content?: unknown}> : [];
  return messages.map(message => typeof message.content === "string" ? message.content : JSON.stringify(message.content ?? ""));
}

/**
 * OpenAI-compatible local model. Butler delegates the first prompt with
 * start_subagent and answers the second directly; the child's answer waits
 * on `release` so the test can act while it works.
 */
async function startModelStub(release: Promise<void>): Promise<ModelStub & {childRequests: () => number}> {
  let childRequests = 0;
  const completion = (message: Record<string, unknown>, finish: string) => JSON.stringify({
    id: "chatcmpl-butler-subagents", object: "chat.completion", created: 1, model: "security-model",
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
    const offered = (name: string) => tools.some(tool => tool.function?.name === name);
    const nonce = tools.find(tool => tool.function?.name === "nebula_capability_probe")?.function?.parameters?.properties?.nonce?.enum?.[0];
    if (nonce) {
      response.end(call("nebula_capability_probe", {nonce}));
      return;
    }
    const texts = messageText(body);
    if (texts.some(text => text.includes("Name this conversation from its first exchange"))) {
      response.end(completion({content: "Butler subagents"}, "stop"));
      return;
    }
    const synthesis = body.tool_choice === "none";
    if (texts.some(text => text.includes(CHILD_MARKER))) {
      childRequests += 1;
      await release;
      response.end(synthesis || !offered("finish_response")
        ? completion({content: "Paused 3 synthetic goals; Core read-back shows each paused."}, "stop")
        : call("finish_response", {}));
      return;
    }
    const messages = Array.isArray(body.messages) ? body.messages as Array<{role?: string; content?: unknown}> : [];
    const lastUser = messages.map(message => message.role).lastIndexOf("user");
    const prompt = String(messages[lastUser]?.content ?? "");
    const toolReplies = messages.slice(lastUser + 1).filter(message => message.role === "tool").length;
    if (!synthesis && prompt.includes(PROMPTS.delegate) && toolReplies === 0 && offered("start_subagent")) {
      response.end(call("start_subagent", {task: "Pause every running synthetic goal and confirm each with Core read-back.", name: "Pause synthetic goals"}));
      return;
    }
    if (!synthesis && offered("finish_response")) {
      response.end(call("finish_response", {}));
      return;
    }
    response.end(completion({content: prompt.includes(PROMPTS.delegate)
      ? "Started a subagent to pause the synthetic goals. Its report will appear here."
      : prompt.includes(PROMPTS.meanwhile) ? "This is the Butler subagents fixture project." : "Done."}, "stop"));
  });
  await new Promise<void>((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", () => resolve());
  });
  return {origin: `http://127.0.0.1:${(server.address() as AddressInfo).port}`, server, childRequests: () => childRequests};
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

test("assistant upgrade Butler delegates to a subagent, stays responsive and shows its report on production LAN Core", async ({page}, testInfo) => {
  test.setTimeout(360_000);
  let releaseChild!: () => void;
  const release = new Promise<void>(resolve => { releaseChild = resolve; });
  const stub = await startModelStub(release);
  let core: TestCore | undefined;
  await withOwnedCleanup(async () => {
    core = await startCore();
    const api = await playwrightRequest.newContext({baseURL: `${core.origin}/api/v1/`, extraHTTPHeaders: {Authorization: `Bearer ${core.token}`}});
    try {
      await waitForCoreApi(api, 90_000);
      const projectResponse = await coreMutation(api, "post", "engagements", {name: "Butler subagents fixture", status: "active", metadata: {}});
      expect(projectResponse.ok(), await projectResponse.text()).toBe(true);
      const projectId = (await projectResponse.json() as {id: string}).id;
      const modelResponse = await coreMutation(api, "post", "providers", {name: "Butler subagents model", provider_type: "vllm", endpoint: `${stub.origin}/v1`, enabled: true,
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
      await pairBrowser(page, core, "Butler subagents LAN browser");
      await page.goto(`${core.origin}/projects/${projectId}/workbench?view=chat`);
      await openButler(page);
      const assistant = page.getByRole("dialog", {name: "Butler", exact: true});
      const transcript = assistant.getByRole("log", {name: "Butler conversation"});
      const composer = assistant.getByRole("textbox", {name: "Ask Butler a question"});
      const send = async (prompt: string) => {
        await composer.fill(prompt);
        await assistant.getByRole("button", {name: "Send to Butler"}).click();
      };

      // Delegation is on by default for a provider Butler.
      await assistant.getByRole("button", {name: "Butler settings"}).click();
      await expect(assistant.getByRole("checkbox", {name: /Delegate longer work to subagents/})).toBeChecked({timeout: 60_000});
      await assistant.getByRole("button", {name: "Butler settings"}).click();
      const chatTab = assistant.getByRole("tab", {name: /Chat/});
      if (await chatTab.isVisible()) await chatTab.click();

      await send(PROMPTS.delegate);
      await expect(transcript.getByText("Started a subagent to pause the synthetic goals.", {exact: false})).toBeVisible({timeout: 90_000});
      const strip = assistant.getByRole("region", {name: "Butler subagents"});
      await expect(strip.getByRole("status")).toHaveText("1 subagent working", {timeout: 30_000});
      await expect(strip.getByText("Pause synthetic goals")).toBeVisible();
      await expect.poll(stub.childRequests, {timeout: 30_000}).toBeGreaterThan(0);

      // Butler is free while its child works: a second request is answered now.
      await expect(composer).toBeEnabled();
      await send(PROMPTS.meanwhile);
      await expect(transcript.getByText("This is the Butler subagents fixture project.")).toBeVisible({timeout: 90_000});
      await expect(strip.getByRole("status")).toHaveText("1 subagent working");

      const butler = await (await api.get(`chat/control-assistant/session?engagement_id=${projectId}`)).json() as {id: string};
      const running = await (await api.get(`chat/sessions/${butler.id}/subagents`)).json() as {subagents: Array<{status: string; child_session_id: string}>};
      expect(running.subagents.map(item => item.status)).toEqual(["running"]);
      const child = await (await api.get(`chat-sessions/${running.subagents[0].child_session_id}`)).json() as {metadata: Record<string, unknown>; parent_session_id?: string};
      expect(child.metadata.control_assistant).toBe(true);

      releaseChild();
      await expect(transcript.getByText(/Paused 3 synthetic goals/)).toBeVisible({timeout: 90_000});
      await expect(strip.getByRole("status")).toHaveText("Subagent finished");
      // The reservation still returns Butler's own chat, not the newer child.
      expect((await (await api.get(`chat/control-assistant/session?engagement_id=${projectId}`)).json() as {id: string}).id).toBe(butler.id);
      const done = await (await api.get(`chat/sessions/${butler.id}/subagents`)).json() as {subagents: Array<{status: string; result_message_id?: string}>};
      expect(done.subagents[0].status).toBe("completed");
      expect(done.subagents[0].result_message_id).toBeTruthy();

      expect((await new AxeBuilder({page}).include('[role="dialog"][aria-label="Butler"]').analyze()).violations).toEqual([]);
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1)).toBe(true);

      // The report is durable: a reload restores it from Core.
      await page.reload();
      await openButler(page);
      await expect(page.getByRole("dialog", {name: "Butler", exact: true}).getByRole("log", {name: "Butler conversation"}).getByText(/Paused 3 synthetic goals/)).toBeVisible({timeout: 60_000});

      await send(PROMPTS.meanwhile);
      await expect(strip).toHaveCount(0, {timeout: 30_000});
      await testInfo.attach("butler-subagents-lan", {body: JSON.stringify({origin: core.origin, build: "production", project: testInfo.project.name,
        viewport: page.viewportSize(), asset: entry, assetSha256: build.assets[entry!],
        result: "Provider Butler delegated by default, replied before its Butler-mode child finished, answered a second request while the child ran, showed the report live, kept its own chat as the reservation, restored the report after reload, and cleared finished rows on the next request."}), contentType: "application/json"});
    } finally {
      await api.dispose();
    }
  }, [
    {name: "release held subagent answer", dispose: async () => { releaseChild(); }},
    {name: "model stub", dispose: () => stopModelStub(stub)},
    {name: "Butler subagents fixture Core", dispose: async () => { if (core) await stopCore(core); }},
  ]);
});
