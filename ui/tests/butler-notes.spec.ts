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
 * Butler Notes on a production-built LAN Core. Core itself saves Butler note
 * creates and edits (terminal and Workbench chats have no browser to apply them),
 * so the web panel must only refresh Notes from Core's read-back receipt and
 * never write the note a second time. Deletion still needs the operator's
 * confirmation in this panel. This journey lives outside real-core.spec.ts,
 * which is above the public commit guard's per-file size limit.
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

type ToolCall = {name: string; arguments: Record<string, unknown>};

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
  const dataDir = await mkdtemp(path.join(tmpdir(), "nebula-butler-notes-core-"));
  const token = "butler-notes-test-administration-token";
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
    {name: "failed Butler Notes fixture Core", dispose: () => stopCore({process: child, dataDir, origin: "", token, output: () => output})},
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

/** OpenAI-compatible local model: verification probe, scripted Butler tool calls, then a fixed answer. */
async function startModelStub(toolCall: (body: Record<string, unknown>) => ToolCall | undefined): Promise<ModelStub> {
  const completion = (message: Record<string, unknown>, finish: string) => JSON.stringify({
    id: "chatcmpl-butler-notes", object: "chat.completion", created: 1, model: "security-model",
    choices: [{index: 0, message: {role: "assistant", ...message}, finish_reason: finish}],
    usage: {prompt_tokens: 12, completion_tokens: 4, total_tokens: 16},
  });
  const call = (name: string, args: Record<string, unknown>) => completion({content: null, tool_calls: [{
    id: `call-${name}`, type: "function", function: {name, arguments: JSON.stringify(args)},
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
    if (tools.some(tool => tool.function?.name === "finish_response")) {
      response.end(call("finish_response", {}));
      return;
    }
    const messages = Array.isArray(body.messages) ? body.messages as Array<{content?: unknown}> : [];
    if (messages.some(message => typeof message.content === "string" && message.content.includes("Name this conversation from its first exchange"))) {
      response.end(completion({content: "Butler Notes"}, "stop"));
      return;
    }
    const selected = toolCall(body);
    if (selected && tools.some(tool => tool.function?.name === selected.name)) {
      response.end(call(selected.name, selected.arguments));
      return;
    }
    response.end(completion({content: "Butler checked the note result."}, "stop"));
  });
  await new Promise<void>((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", () => resolve());
  });
  return {origin: `http://127.0.0.1:${(server.address() as AddressInfo).port}`, server};
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

test("assistant upgrade Butler saves project Notes in Core and deletes only after confirmation on production LAN Core", async ({page}, testInfo) => {
  test.setTimeout(360_000);
  const prompts = ["Create the synthetic Butler project note.", "Revise the synthetic Butler project note.", "Delete the synthetic Butler project note."];
  let noteId = "";
  let noteRevision = 0;
  let assetId = "";
  const stub = await startModelStub(body => {
    const messages = Array.isArray(body.messages) ? body.messages as Array<{role?: string; content?: unknown}> : [];
    const lastUser = messages.map(message => message.role).lastIndexOf("user");
    const prompt = prompts.find(item => String(messages[lastUser]?.content ?? "").startsWith(item));
    if (!prompt) return undefined;
    const replies = messages.slice(lastUser + 1).filter(message => message.role === "tool").length;
    const create = prompt === prompts[0];
    const operation = create ? "nebula.notes.create" : prompt === prompts[1] ? "nebula.notes.update" : "nebula.notes.delete";
    const read = create ? "nebula.notes.link_targets" : "nebula.notes.detail";
    if (replies === 0) return {name: "nebula_capabilities_search", arguments: {query: "create edit delete project notes"}};
    if (replies === 1) return {name: "nebula_capabilities_describe", arguments: {operation: read}};
    if (replies === 2) return {name: "nebula_capabilities_read", arguments: {operation: read, arguments: create ? {kind: "asset"} : {note_id: noteId}}};
    if (replies === 3) return {name: "nebula_capabilities_describe", arguments: {operation}};
    if (replies === 4) return {name: "nebula_capabilities_change", arguments: {operation, arguments: create
      ? {title: "Butler note fixture", body: "Observed fixture fact.", asset_ids: [assetId], request_id: "butler-notes-browser-0001"}
      : {note_id: noteId, expected_revision: noteRevision,
        ...(prompt === prompts[1] ? {title: "Butler revised note", body: "Revised fixture fact."} : {})}}};
    return undefined;
  });
  let core: TestCore | undefined;
  await withOwnedCleanup(async () => {
    core = await startCore();
    const api = await playwrightRequest.newContext({baseURL: `${core.origin}/api/v1/`, extraHTTPHeaders: {Authorization: `Bearer ${core.token}`}});
    try {
      await waitForCoreApi(api, 90_000);
      const projectResponse = await coreMutation(api, "post", "engagements", {name: "Butler Notes fixture", status: "active", metadata: {}});
      expect(projectResponse.ok(), await projectResponse.text()).toBe(true);
      const projectId = (await projectResponse.json() as {id: string}).id;
      const assetResponse = await coreMutation(api, "post", "assets", {engagement_id: projectId, name: "Synthetic host"});
      expect(assetResponse.ok(), await assetResponse.text()).toBe(true);
      assetId = (await assetResponse.json() as {id: string}).id;
      const modelResponse = await coreMutation(api, "post", "providers", {name: "Butler Notes model", provider_type: "vllm", endpoint: `${stub.origin}/v1`, enabled: true,
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

      // The panel must never write a Core-saved note again.
      const browserNoteWrites: string[] = [];
      page.on("request", request => {
        const pathname = new URL(request.url()).pathname;
        if (request.method() !== "GET" && /\/api\/v1\/observations(\/|$)/.test(pathname)) browserNoteWrites.push(`${request.method()} ${pathname}`);
      });
      await page.addInitScript(id => localStorage.setItem("nebula.engagement", id), projectId);
      await pairBrowser(page, core, "Butler Notes LAN browser");
      await page.goto(`${core.origin}/projects/${projectId}/workbench?view=notes`);
      const assistant = page.getByRole("dialog", {name: "Butler"});
      const notes = page.getByRole("complementary", {name: "Project notes"});
      const send = async (prompt: string) => {
        if (!await assistant.isVisible()) await openButler(page);
        await assistant.getByRole("textbox", {name: "Ask Butler a question"}).fill(prompt);
        await assistant.getByRole("button", {name: "Send to Butler"}).click();
      };

      await send(prompts[0]);
      await expect(assistant.getByText(/Created note “Butler note fixture” at Core revision \d+/)).toBeVisible({timeout: 90_000});
      const rows = await (await api.get(`observations?engagement_id=${projectId}`)).json() as Array<{id: string; title: string; revision: number; asset_ids: string[]}>;
      expect(rows.map(row => row.title)).toEqual(["Butler note fixture"]);
      expect(rows[0].asset_ids).toEqual([assetId]);
      noteId = rows[0].id; noteRevision = rows[0].revision;
      expect(browserNoteWrites).toEqual([]);
      await assistant.getByRole("button", {name: "Hide Butler"}).click();
      await notes.getByRole("button", {name: /Butler note fixture/}).click();
      await expect(page.getByRole("textbox", {name: "Note body"})).toHaveValue("Observed fixture fact.");

      await send(prompts[1]);
      await expect(assistant.getByText(/Updated note “Butler revised note” at Core revision \d+/)).toBeVisible({timeout: 90_000});
      const saved = await (await api.get(`observations/${noteId}`)).json() as {revision: number; asset_ids: string[]; body: string};
      expect(saved.revision).toBeGreaterThan(noteRevision);
      expect(saved.asset_ids).toEqual([assetId]);
      expect(saved.body).toBe("Revised fixture fact.");
      noteRevision = saved.revision;
      expect(browserNoteWrites).toEqual([]);
      await assistant.getByRole("button", {name: "Hide Butler"}).click();
      await expect(notes.getByRole("button", {name: /Butler revised note/})).toBeVisible();
      await page.reload();
      await expect(page.getByRole("textbox", {name: "Note body"})).toHaveValue("Revised fixture fact.");
      expect((await (await api.get(`observations?engagement_id=${projectId}`)).json() as unknown[]).length).toBe(1);

      await send(prompts[2]);
      const confirmation = page.getByRole("dialog", {name: "Delete project note?"});
      await expect(confirmation).toBeVisible({timeout: 90_000});
      await confirmation.getByRole("button", {name: "Cancel"}).click();
      await expect(assistant.getByText(/Note “Butler revised note” was not deleted/)).toBeVisible();
      expect((await api.get(`observations/${noteId}`)).status()).toBe(200);
      await send(prompts[2]);
      await expect(confirmation).toBeVisible({timeout: 90_000});
      await confirmation.getByRole("button", {name: "Delete note"}).click();
      // Known Core routing defect (outside this change): a read of a deleted
      // entity's ID resolves to no project, so the API worker routes it to the
      // global execution runtime, which answers 503 "starting" instead of 404.
      // The panel then honestly reports the deletion as unconfirmed. Accept
      // only these two outcomes and assert the deletion through the
      // project-scoped note list.
      await expect(assistant.getByText(/Core confirms its absence; linked assets and evidence remain|Core did not confirm the note change\. Inspect Project notes before retrying/)).toBeVisible();
      const remaining = await (await api.get(`observations?engagement_id=${projectId}`)).json() as Array<{id: string}>;
      expect(remaining.map(row => row.id)).not.toContain(noteId);
      await expect(notes.getByRole("button", {name: /Butler revised note/})).toHaveCount(0);
      expect((await api.get(`assets/${assetId}`)).status()).toBe(200);
      expect(browserNoteWrites).toEqual([`DELETE /api/v1/observations/${noteId}`]);
      expect((await new AxeBuilder({page}).include('[role="dialog"][aria-label="Butler"]').analyze()).violations).toEqual([]);
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1)).toBe(true);
      await testInfo.attach("butler-notes-lan", {body: JSON.stringify({origin: core.origin, build: "production", project: testInfo.project.name,
        viewport: page.viewportSize(), asset: entry, assetSha256: build.assets[entry!],
        result: "Core saved and read back one linked note and its edit; the panel refreshed Notes without writing them; reload showed saved fields; cancelled deletion retained the note; confirmed deletion removed only the note."}), contentType: "application/json"});
    } finally {
      await api.dispose();
    }
  }, [
    {name: "model stub", dispose: () => stopModelStub(stub)},
    {name: "Butler Notes fixture Core", dispose: async () => { if (core) await stopCore(core); }},
  ]);
});
