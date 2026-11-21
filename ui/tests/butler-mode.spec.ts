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
 * Butler mode on a production-built LAN Core: a status board of every
 * project's goal chats, in Active, Idle and Completed sections, beside
 * parallel Butler threads. Two threads answer at the same time; a thread
 * survives a reload through its URL; "Ask Butler about this" starts a thread
 * in the card's project; Stop on a card ends the chat's response, which
 * pauses its goal, and the board moves it to Idle once Core reports it.
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
  const dataDir = await mkdtemp(path.join(tmpdir(), "nebula-butler-mode-core-"));
  const token = "butler-mode-test-administration-token";
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
    {name: "failed Butler mode fixture Core", dispose: () => stopCore({process: child, dataDir, origin: "", token, output: () => output})},
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
  held: "THREAD-A Which chats need me right now?",
  quick: "THREAD-B Summarize this week's results.",
  about: "Is it stuck?",
  work: "HOLD-WORK Fuzz the parser.",
};

/** A long first message: a long transcript once pushed the popup off-screen. */
const WORK_LOG = Array.from({length: 120}, (_, index) => `Crash ${index + 1}: parser rejected input at offset ${index * 64}.`).join("\n");

const ANSWERS = {
  held: "Nothing needs you right now.",
  quick: "Two confirmed results this week.",
  about: "Fuzz parser is still running.",
};

/**
 * OpenAI-compatible local model. The held thread and the ordinary chat wait
 * for their release; every other prompt answers at once.
 */
async function startModelStub(holdThread: Promise<void>, holdWork: Promise<void>): Promise<ModelStub & {prompts: () => string[]}> {
  const prompts: string[] = [];
  const completion = (message: Record<string, unknown>, finish: string) => JSON.stringify({
    id: "chatcmpl-butler-mode", object: "chat.completion", created: 1, model: "security-model",
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
    const nonce = tools.find(tool => tool.function?.name === "nebula_capability_probe")?.function?.parameters?.properties?.nonce?.enum?.[0];
    if (nonce) {
      response.end(call("nebula_capability_probe", {nonce}));
      return;
    }
    const messages = Array.isArray(body.messages) ? body.messages as Array<{role?: string; content?: unknown}> : [];
    const texts = messages.map(message => typeof message.content === "string" ? message.content : JSON.stringify(message.content ?? ""));
    if (texts.some(text => text.includes("Name this conversation from its first exchange"))) {
      response.end(completion({content: texts.some(text => text.includes(PROMPTS.work)) ? "Fuzz parser" : "Synthetic chat"}, "stop"));
      return;
    }
    const synthesis = body.tool_choice === "none";
    const lastUser = messages.map(message => message.role).lastIndexOf("user");
    const prompt = String(messages[lastUser]?.content ?? "");
    const toolReplies = messages.slice(lastUser + 1).filter(message => message.role === "tool").length;
    if (!synthesis && toolReplies === 0) prompts.push(prompt);
    if (prompt.includes(PROMPTS.held)) await holdThread;
    if (prompt.includes(PROMPTS.work)) await holdWork;
    const finish = tools.map(tool => tool.function?.name ?? "").find(name => name.startsWith("finish_response"));
    if (!synthesis && finish) {
      response.end(call(finish, {}));
      return;
    }
    response.end(completion({content: prompt.includes(PROMPTS.held) ? ANSWERS.held
      : prompt.includes(PROMPTS.quick) ? ANSWERS.quick
      : prompt.includes(PROMPTS.about) ? ANSWERS.about : "Done."}, "stop"));
  });
  await new Promise<void>((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", () => resolve());
  });
  return {origin: `http://127.0.0.1:${(server.address() as AddressInfo).port}`, server, prompts: () => prompts};
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


/** On a phone the board and threads are tabs; on a desktop both are visible. */
async function showThreads(page: Page): Promise<void> {
  const tab = page.getByRole("tab", {name: /^Threads/});
  if (await tab.isVisible()) await tab.click();
}

async function showBoard(page: Page): Promise<void> {
  const tab = page.getByRole("tab", {name: /^Board/});
  if (await tab.isVisible()) await tab.click();
}

async function startThread(page: Page, prompt: string): Promise<void> {
  await showThreads(page);
  const rail = page.getByRole("navigation", {name: "Butler threads"});
  const fresh = rail.getByRole("button", {name: "New", exact: true});
  if (await fresh.isVisible()) await fresh.click();
  const composer = page.getByRole("textbox", {name: "Message Butler"});
  await composer.fill(prompt);
  await composer.press("End");
  await page.getByRole("button", {name: "Start thread"}).click();
  // The message shows at once; Core reserves the thread behind it.
  await expect(composer).toHaveValue("", {timeout: 5_000});
  await expect(page.getByRole("log", {name: "Butler thread messages"}).getByText(prompt, {exact: true})).toBeVisible({timeout: 5_000});
}

test("assistant upgrade Butler mode runs parallel threads beside every project's chats on production LAN Core", async ({page}, testInfo) => {
  test.setTimeout(420_000);
  let releaseThread!: () => void;
  let releaseWork!: () => void;
  const holdThread = new Promise<void>(resolve => { releaseThread = resolve; });
  const holdWork = new Promise<void>(resolve => { releaseWork = resolve; });
  const stub = await startModelStub(holdThread, holdWork);
  let core: TestCore | undefined;
  let running: Promise<unknown> | undefined;
  await withOwnedCleanup(async () => {
    core = await startCore();
    const api = await playwrightRequest.newContext({baseURL: `${core.origin}/api/v1/`, extraHTTPHeaders: {Authorization: `Bearer ${core.token}`}});
    try {
      await waitForCoreApi(api, 90_000);
      const project = async (name: string) => {
        const response = await coreMutation(api, "post", "engagements", {name, status: "active", metadata: {}});
        expect(response.ok(), await response.text()).toBe(true);
        return (await response.json() as {id: string}).id;
      };
      const research = await project("Research fixture");
      const bounty = await project("Bounty fixture");
      const modelResponse = await coreMutation(api, "post", "providers", {name: "Butler mode model", provider_type: "vllm", endpoint: `${stub.origin}/v1`, enabled: true,
        is_local: true, model_allowlist: ["security-model"], privacy: {local_only: true, residency: [], permits_sensitive_data: false},
        metadata: {default_model: "security-model", options: {context_window: 32768}}});
      expect(modelResponse.ok(), await modelResponse.text()).toBe(true);
      const model = await modelResponse.json() as {id: string; revision: number};
      const verified = await coreMutation(api, "post", `providers/${model.id}/capabilities/verify`, {model: "security-model", expected_revision: model.revision});
      expect(verified.ok(), await verified.text()).toBe(true);
      // Each project's Butler conversation holds the settings its threads reuse.
      for (const projectId of [research, bounty]) {
        const butler = await coreMutation(api, "post", "chat/control-assistant/session", {engagement_id: projectId, backend: "provider", provider_id: model.id, model: "security-model"});
        expect(butler.ok(), await butler.text()).toBe(true);
      }
      // The board lists goal chats only. In the other project: a goal chat
      // that keeps working until released, a goal Butler proposed (Idle), a
      // cancelled goal (Completed), and a plain chat without a goal.
      const goalChat = async (title: string, extra: Record<string, unknown> = {}) => {
        const created = await coreMutation(api, "post", "chat/goal-conversations", {engagement_id: bounty, backend: "provider", provider_id: model.id,
          model: "security-model", title, objective: `${title}: find a reproducible crash`, completion_criteria: ["A reproducible crash"], ...extra});
        expect(created.ok(), await created.text()).toBe(true);
        return (await created.json() as {session: {id: string}}).session.id;
      };
      const goalAction = async (sessionId: string, action: string) => {
        const goal = await (await api.get(`chat/sessions/${sessionId}/goal`)).json() as {revision: number};
        const written = await coreMutation(api, "post", `chat/sessions/${sessionId}/goal/actions`, {expected_revision: goal.revision, action});
        expect(written.ok(), await written.text()).toBe(true);
      };
      const workId = await goalChat("Fuzz parser");
      await goalChat("Triage crash logs", {proposed_by_butler: true});
      await goalAction(await goalChat("Dropped idea"), "cancel");
      const plain = await api.post("chat/completions", {headers: {"Idempotency-Key": `butler-mode-plain-${Date.now()}`}, timeout: 120_000, data: {
        engagement_id: bounty, provider_id: model.id, model: "security-model", tools_enabled: false, include_knowledge: false,
        allow_agent_messaging: false, messages: [{role: "user", content: "PLAIN chat without a goal"}], stream: false}});
      expect(plain.ok(), await plain.text()).toBe(true);
      const plainId = (await plain.json() as {session_id: string}).session_id;
      expect(plainId).toBeTruthy();
      running = api.post("chat/completions", {headers: {"Idempotency-Key": `butler-mode-work-${Date.now()}`}, timeout: 300_000, data: {
        engagement_id: bounty, session_id: workId, provider_id: model.id, model: "security-model", tools_enabled: false, include_knowledge: false,
        allow_agent_messaging: false, messages: [{role: "user", content: `${PROMPTS.work}\n${WORK_LOG}`}], stream: false}}).catch(error => error);
      type Activity = Array<{session_id: string; state: string; turn_id?: string | null}>;
      await expect.poll(async () => {
        const activity = await (await api.get(`chat/session-activity?engagement_id=${bounty}`)).json() as Activity;
        return activity.find(item => item.session_id === workId)?.state ?? "";
      }, {timeout: 90_000}).toBe("working");
      // The goal starts while its first response runs; Core adds no turn of its own.
      await goalAction(workId, "start");
      // The goal chat keeps the title it was created with.
      let workTitle = "";
      await expect.poll(async () => {
        workTitle = (await (await api.get(`chat-sessions/${workId}`)).json() as {title: string}).title;
        return workTitle;
      }, {timeout: 60_000}).toBe("Fuzz parser");
      const titlePattern = workTitle.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

      const build = await (await page.request.get(`${core.origin}/web-build.json`)).json() as {assets: Record<string, string>};
      const entry = Object.keys(build.assets).find(asset => asset.startsWith("assets/index-") && asset.endsWith(".js"));
      expect(entry).toBeTruthy();
      expect(createHash("sha256").update(await (await page.request.get(`${core.origin}/${entry}`)).body()).digest("hex")).toBe(build.assets[entry!]);

      await page.addInitScript(id => localStorage.setItem("nebula.engagement", id), research);
      await pairBrowser(page, core, "Butler mode LAN browser");
      await page.goto(`${core.origin}/projects/${research}/workbench?view=chat`);
      // Entry: the Butler launcher opens Butler mode.
      await openButler(page);
      await expect(page).toHaveURL(`${core.origin}/butler?project=${research}`);
      await expect(page.getByRole("heading", {name: "Butler", exact: true})).toBeVisible();
      // Every top-bar control stays inside the viewport, down to 320 px.
      const width = page.viewportSize()!.width;
      for (const control of [page.getByRole("button", {name: "Exit Butler mode"})]) {
        const box = await control.boundingBox();
        expect(box && box.x >= 0 && box.x + box.width <= width + 1, `${await control.getAttribute("aria-label")} fits ${width}px`).toBe(true);
      }

      // The board, grouped by project, shows the other project's goal chats in
      // Active, Idle and a collapsed Completed; the plain chat is not listed.
      await showBoard(page);
      const board = page.getByRole("region", {name: "Status board"});
      const card = board.getByRole("article", {name: `${workTitle}: Working, Active`});
      await expect(card).toBeVisible({timeout: 60_000});
      await expect(card).toContainText("Working");
      const bountyGroup = board.locator(`[data-project="${bounty}"]`);
      await expect(bountyGroup.getByRole("article", {name: `${workTitle}: Working, Active`})).toBeVisible();
      await expect(board.locator(`[data-project="${research}"]`)).toHaveCount(0);
      // Only the two open sections' goal chats show; the plain chat is not on the board.
      await expect(bountyGroup.getByRole("article")).toHaveCount(2);
      type Board = {counts: Record<string, number>; items: Array<{session_id: string; section: string}>};
      const boardRead = await (await api.get("butler/board")).json() as Board;
      expect(boardRead.counts).toEqual({active: 1, idle: 1, completed: 1});
      expect(boardRead.items.map(item => item.session_id)).not.toContain(plainId);
      const projectHead = bountyGroup.getByRole("button", {name: /^Bounty fixture/});
      for (const text of ["3 goals", "1 active", "1 idle", "1 completed"]) await expect(projectHead).toContainText(text);
      const sectionHead = (name: string) => bountyGroup.locator(`div[data-section="${name}"] > button`);
      await expect(sectionHead("active")).toHaveAttribute("aria-expanded", "true");
      await expect(sectionHead("active")).toContainText("Active1");
      await expect(sectionHead("idle")).toHaveAttribute("aria-expanded", "true");
      await expect(sectionHead("completed")).toHaveAttribute("aria-expanded", "false");
      await expect(sectionHead("completed")).toContainText("Completed1");
      const proposal = bountyGroup.getByRole("article", {name: "Triage crash logs: Proposed, Idle"});
      await expect(proposal).toContainText("Butler proposed this goal chat");
      const dropped = board.getByRole("article", {name: "Dropped idea: Cancelled, Completed"});
      await expect(dropped).toHaveCount(0);
      // Each section has its own dot: solid blue, a hollow amber ring, solid green (grey when cancelled).
      const dotStyle = (locator: ReturnType<Page["locator"]>) => locator.evaluate(element => {
        const style = getComputedStyle(element);
        return {section: element.getAttribute("data-section"), background: style.backgroundColor, border: style.borderTopWidth};
      });
      const activeDot = await dotStyle(card.locator("[data-section]").first());
      const idleDot = await dotStyle(proposal.locator("[data-section]").first());
      expect(activeDot.section).toBe("active");
      expect(idleDot.section).toBe("idle");
      expect(idleDot.border).toBe("2px");
      expect(idleDot.background).toBe("rgba(0, 0, 0, 0)");
      expect(activeDot.background).not.toBe(idleDot.background);
      // Opening Completed reads it from Core; this device remembers it across a reload.
      await sectionHead("completed").click();
      await expect(dropped).toBeVisible({timeout: 30_000});
      const cancelledDot = await dotStyle(dropped.locator("[data-section]").first());
      expect(cancelledDot.section).toBe("completed");
      expect(cancelledDot.background).not.toBe(activeDot.background);
      await page.reload();
      await showBoard(page);
      await expect(dropped).toBeVisible({timeout: 60_000});
      await expect(sectionHead("completed")).toHaveAttribute("aria-expanded", "true");
      await sectionHead("completed").click();
      await expect(dropped).toHaveCount(0);
      // The board's search sits beside Group and filters as the operator types.
      const boardSearch = board.getByRole("searchbox", {name: "Search projects and chats"});
      const searchBox = await boardSearch.boundingBox();
      expect(searchBox && searchBox.x >= 0 && searchBox.x + searchBox.width <= width + 1, `board search fits ${width}px`).toBe(true);
      await boardSearch.fill("bounty fix");
      await expect(card).toBeVisible();
      await expect(board.getByRole("status")).toContainText("in 1 project match");
      await boardSearch.fill("no such chat anywhere");
      await expect(card).toHaveCount(0);
      await expect(board.getByText("No goal chats match “no such chat anywhere”.")).toBeVisible();
      await board.getByRole("button", {name: "Clear search"}).click();
      await expect(card).toBeVisible();

      await showThreads(page);
      await expect(page.getByRole("combobox", {name: "Project for the new thread"})).toHaveValue(research);
      // Two threads: the first is still being answered when the second finishes.
      await startThread(page, PROMPTS.held);
      await expect.poll(stub.prompts, {timeout: 60_000}).toContain(PROMPTS.held);
      // The working thread shows the same Activity card as a normal chat.
      await expect(page.getByRole("region", {name: "Butler's steps"})).toBeVisible();
      await startThread(page, PROMPTS.quick);
      const conversation = page.getByRole("log", {name: "Butler thread messages"});
      await expect(conversation.getByText(ANSWERS.quick)).toBeVisible({timeout: 90_000});
      await page.screenshot({path: testInfo.outputPath("butler-mode-threads.png")});
      // Threads are Core's: the open thread (in the URL) and its answer survive a reload.
      expect(new URL(page.url()).searchParams.get("thread")).toBeTruthy();
      await page.reload();
      await expect(page.getByRole("log", {name: "Butler thread messages"}).getByText(ANSWERS.quick)).toBeVisible({timeout: 60_000});
      await showThreads(page);
      const rail = page.getByRole("navigation", {name: "Butler threads"});
      if (!await rail.isVisible()) await page.getByRole("button", {name: "Back to threads"}).click();
      await expect(rail.getByRole("button", {name: /THREAD-A.*Working/})).toBeVisible({timeout: 30_000});
      expect(stub.prompts().filter(prompt => prompt.includes(PROMPTS.held))).toHaveLength(1);
      type Threads = {items: Array<{session_id: string; title: string; state: string; project: {id: string}}>};
      const threads = await (await api.get("butler/threads")).json() as Threads;
      expect(threads.items.map(item => item.title).sort()).toEqual([PROMPTS.held, PROMPTS.quick].sort());
      expect(threads.items.find(item => item.title === PROMPTS.held)?.state).toBe("working");
      // The panel's own Butler conversation is not replaced by a thread.
      const current = await (await api.get(`chat/control-assistant/session?engagement_id=${research}`)).json() as {id: string; metadata: Record<string, unknown>};
      expect(current.metadata.butler_thread).toBeUndefined();

      // A card opens the chat's brief; its transcript opens over Butler mode and closes with Esc.
      await showBoard(page);
      await card.getByRole("button", {name: new RegExp(titlePattern)}).click();
      const brief = page.getByRole("region", {name: `Chat brief: ${workTitle}`});
      await expect(brief).toBeVisible({timeout: 60_000});
      expect(new URL(page.url()).searchParams.get("chat")).toBe(workId);
      await brief.getByRole("button", {name: "Open full chat"}).click();
      const popup = page.getByRole("dialog", {name: `Chat: ${workTitle}`});
      await expect(popup.getByRole("log")).toBeVisible();
      // The transcript loads (Core pages history only with replaced messages
      // included) and the popup sits inside the viewport, centred, even though
      // the transcript is taller than the viewport.
      await expect(popup.getByRole("log").getByText(PROMPTS.work)).toBeVisible({timeout: 30_000});
      await expect(popup.getByRole("alert")).toHaveCount(0);
      const box = await popup.boundingBox();
      const viewport = page.viewportSize();
      expect(box && viewport).toBeTruthy();
      expect(box!.x).toBeGreaterThanOrEqual(0);
      expect(box!.x + box!.width).toBeLessThanOrEqual(viewport!.width + 1);
      expect(box!.y).toBeGreaterThanOrEqual(0);
      expect(box!.y + box!.height).toBeLessThanOrEqual(viewport!.height + 1);
      await expect(popup.getByRole("button", {name: "Open in workbench"})).toBeInViewport();
      if (viewport!.width > 760) expect(Math.abs(box!.x + box!.width / 2 - viewport!.width / 2)).toBeLessThanOrEqual(2);
      await page.keyboard.press("Escape");
      await expect(popup).toHaveCount(0);
      await expect(brief).toBeVisible();
      await page.screenshot({path: testInfo.outputPath("butler-mode-brief.png")});

      // Ask Butler about a card: the thread runs in the card's project.
      await brief.getByRole("button", {name: "Ask Butler about this"}).click();
      await expect(page.getByRole("combobox", {name: "Project for the new thread"})).toHaveValue(bounty);
      await page.screenshot({path: testInfo.outputPath("butler-mode-ask-about.png")});
      const composer = page.getByRole("textbox", {name: "Message Butler"});
      await composer.fill(PROMPTS.about);
      await page.getByRole("button", {name: "Start thread"}).click();
      await expect(page.getByRole("log", {name: "Butler thread messages"}).getByText(ANSWERS.about)).toBeVisible({timeout: 90_000});
      expect(stub.prompts().find(prompt => prompt.endsWith(PROMPTS.about))).toContain(`About the chat "${workTitle}" (conversation ${workId} in this project)`);
      const aboutThread = (await (await api.get("butler/threads")).json() as Threads).items.find(item => item.title === PROMPTS.about);
      expect(aboutThread?.project.id).toBe(bounty);
      // Asking about the same chat again quotes it into the open thread (same project).
      await showBoard(page);
      if (!await brief.isVisible()) await card.getByRole("button", {name: new RegExp(titlePattern)}).click();
      await brief.getByRole("button", {name: "Ask Butler about this"}).click();
      await expect(page.getByRole("button", {name: "Reply in this thread"})).toHaveAttribute("aria-pressed", "true");
      await composer.fill("QUOTE-FOLLOW-UP Is it still running?");
      await page.getByRole("button", {name: "Send reply"}).click();
      await expect.poll(() => stub.prompts().find(prompt => prompt.endsWith("QUOTE-FOLLOW-UP Is it still running?")) ?? "", {timeout: 60_000})
        .toContain(`About the chat "${workTitle}" (conversation ${workId} in this project)`);
      expect(((await (await api.get("butler/threads")).json()) as Threads).items.filter(item => item.project.id === bounty).map(item => item.session_id)).toEqual([aboutThread!.session_id]);

      // Stop from the brief; the board shows the change only after Core reports it:
      // the goal pauses and its card moves from Active to Idle.
      await showBoard(page);
      if (!await brief.isVisible()) await card.getByRole("button", {name: new RegExp(titlePattern)}).click();
      await brief.getByRole("button", {name: "Stop"}).click();
      await page.getByRole("dialog").getByRole("button", {name: "Stop response"}).click();
      await brief.getByRole("button", {name: "Back to board"}).click();
      await expect(card).toHaveCount(0, {timeout: 90_000});
      const pausedCard = bountyGroup.getByRole("article", {name: `${workTitle}: Paused, Idle`});
      await expect(pausedCard).toBeVisible({timeout: 30_000});
      await expect(pausedCard).toContainText("Paused");
      await expect(sectionHead("idle")).toContainText("Idle2");
      await expect(sectionHead("active")).toHaveCount(0);
      await expect.poll(async () => ((await (await api.get(`chat/session-activity?engagement_id=${bounty}`)).json()) as Activity)
        .find(item => item.session_id === workId)?.state, {timeout: 60_000}).toBe("idle");
      expect((await (await api.get(`chat/sessions/${workId}/goal`)).json() as {status: string}).status).toBe("paused");

      // Butler's own places: Oversight and Settings tabs, and the project's ongoing thread.
      const sections = page.getByRole("navigation", {name: "Butler sections"});
      if (await sections.isVisible()) {
        await sections.getByRole("button", {name: "Oversight"}).click();
        await expect(page.getByRole("region", {name: "Oversight", exact: true})).toBeVisible();
        await sections.getByRole("button", {name: "Settings"}).click();
        await expect(page.getByRole("region", {name: "Butler configuration"}).getByRole("region", {name: "Butler settings"})).toBeVisible({timeout: 60_000});
        await sections.getByRole("button", {name: "Board"}).click();
      }
      await showThreads(page);
      if (!await rail.isVisible()) await page.getByRole("button", {name: "Back to threads"}).click();
      // One searchable picker, not a row per project: Core may hold a thousand projects.
      await rail.getByRole("button", {name: /^Project thread:/}).click();
      await rail.getByRole("combobox", {name: "Search projects"}).fill("research");
      await expect(rail.getByRole("option", {name: /Research fixture/})).toBeVisible();
      await expect(rail.getByRole("option", {name: /Bounty fixture/})).toHaveCount(0);
      await rail.getByRole("combobox", {name: "Search projects"}).press("Enter");
      await expect(page.getByRole("region", {name: "Butler in Research fixture"}).getByRole("textbox", {name: "Ask Butler a question"})).toBeVisible({timeout: 60_000});
      if (await page.getByRole("button", {name: "Back to threads"}).isVisible()) await page.getByRole("button", {name: "Back to threads"}).click();

      // After a reload the held thread's card comes from Core, not this tab's stream.
      await showThreads(page);
      if (!await rail.isVisible()) await page.getByRole("button", {name: "Back to threads"}).click();
      await rail.getByRole("button", {name: /THREAD-A/}).click();
      await expect(page.getByRole("region", {name: "Butler's steps"})).toBeVisible({timeout: 30_000});
      await expect(page.getByText("Butler is working on this thread…")).toHaveCount(0);
      // Replying to a working thread: the send button is Stop, like a normal chat.
      const composerForm = page.locator("form").filter({has: page.getByRole("textbox", {name: "Message Butler"})});
      await expect(composerForm.getByRole("button", {name: "Stop response"})).toBeVisible();
      await expect(composerForm.getByRole("button", {name: "Send reply"})).toHaveCount(0);
      // The held thread finishes on its own and shows Butler's answer.
      releaseThread();
      await showThreads(page);
      if (!await rail.isVisible()) await page.getByRole("button", {name: "Back to threads"}).click();
      await rail.getByRole("button", {name: /THREAD-A/}).click();
      await expect(page.getByRole("log", {name: "Butler thread messages"}).getByText(ANSWERS.held)).toBeVisible({timeout: 90_000});

      // Archive the answered thread: it leaves Butler mode and stays an archived chat.
      const heldId = threads.items.find(item => item.title === PROMPTS.held)!.session_id;
      await page.getByRole("button", {name: "Archive", exact: true}).click();
      await expect(page.getByRole("button", {name: "Archive", exact: true})).toHaveCount(0);
      await expect.poll(() => new URL(page.url()).searchParams.get("thread")).toBeNull();
      await expect.poll(async () => ((await (await api.get("butler/threads")).json()) as Threads).items.some(item => item.session_id === heldId)).toBe(false);
      expect(((await (await api.get(`chat-sessions/${heldId}`)).json()) as {metadata: {archived_at?: string}}).metadata.archived_at).toBeTruthy();
      // Delete asks first; Cancel keeps the thread, Delete thread removes it from Core.
      const quickId = threads.items.find(item => item.title === PROMPTS.quick)!.session_id;
      await showThreads(page);
      if (!await rail.isVisible()) await page.getByRole("button", {name: "Back to threads"}).click();
      await rail.getByRole("button", {name: /THREAD-B/}).click();
      await page.getByRole("button", {name: "Delete", exact: true}).click();
      const confirm = page.getByRole("group", {name: "Confirm delete"});
      await expect(confirm).toBeVisible();
      await expect(confirm).toBeInViewport();
      await confirm.getByRole("button", {name: "Cancel"}).click();
      await expect(confirm).toHaveCount(0);
      await page.getByRole("button", {name: "Delete", exact: true}).click();
      await confirm.getByRole("button", {name: "Delete thread"}).click();
      await expect.poll(async () => (await api.get(`chat-sessions/${quickId}`)).status()).toBe(404);
      expect(((await (await api.get("butler/threads")).json()) as Threads).items.some(item => item.session_id === quickId)).toBe(false);

      expect((await new AxeBuilder({page}).analyze()).violations).toEqual([]);
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1)).toBe(true);
      for (const control of await page.getByRole("button").all()) {
        if (!await control.isVisible()) continue;
        const box = await control.boundingBox();
        const name = await control.getAttribute("aria-label") ?? await control.textContent();
        if (box && (await control.getAttribute("aria-label"))) expect(Math.min(box.width, box.height), `icon control ${name}`).toBeGreaterThanOrEqual(28);
      }
      await testInfo.attach("butler-mode-lan", {body: JSON.stringify({origin: core.origin, build: "production", project: testInfo.project.name,
        viewport: page.viewportSize(), asset: entry, assetSha256: build.assets[entry!],
        result: "The Butler launcher opened Butler mode; the board, grouped by project, listed only goal chats in Active, Idle and a collapsed Completed with per-section counts and distinct dots, and Completed stayed open across a reload once opened; a card opened its brief and its transcript pop-up closed with Esc; Oversight, Settings and the project thread opened in place; the board listed another project's running chat and its search beside Group filtered it as typed; the project picker found a project by typing and opened it with Enter; two Butler threads ran at once and the second answered while the first worked; the open thread survived a reload; Ask Butler about this started a thread in the card's project with the chat named, and asking again quoted the chat into that open thread; Stop ended the chat's response, which paused its goal, and the board moved the card from Active to Idle after Core reported it; the working thread showed the chat Activity card, live and again from Core after a reload, with Stop in place of Send; the held thread then showed its answer; Archive removed it from Butler mode and Core kept it as an archived chat; Delete asked first, Cancel kept the thread, and Delete thread removed it from Core."}), contentType: "application/json"});
    } finally {
      await api.dispose();
    }
  }, [
    {name: "release held Butler thread", dispose: async () => { releaseThread(); }},
    {name: "release held chat", dispose: async () => { releaseWork(); await running; }},
    {name: "model stub", dispose: () => stopModelStub(stub)},
    {name: "Butler mode fixture Core", dispose: async () => { if (core) await stopCore(core); }},
  ]);
});
