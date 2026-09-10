import AxeBuilder from "@axe-core/playwright";
import { createHash } from "node:crypto";
import { spawn, spawnSync, type ChildProcessWithoutNullStreams } from "node:child_process";
import { existsSync } from "node:fs";
import { mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { createServer, type Server } from "node:http";
import type { AddressInfo } from "node:net";
import { homedir, networkInterfaces, tmpdir } from "node:os";
import path from "node:path";
import { expect, request as playwrightRequest, test } from "@playwright/test";

interface RealCore {
  process: ChildProcessWithoutNullStreams;
  dataDir: string;
  origin: string;
  token: string;
}

async function startRealCore(options: { bindHost?: string; browserHost?: string } = {}): Promise<RealCore> {
  const repository = path.resolve(import.meta.dirname, "../..");
  const commonGitDir = spawnSync("git", ["-C", repository, "rev-parse", "--path-format=absolute", "--git-common-dir"], { encoding: "utf8" }).stdout.trim();
  // Match the production-test override used by the shared real-Core harness.
  const coreCandidates = [
    process.env.NEBULA_TEST_CORE_BIN,
    process.env.NEBULA_CORE_BINARY,
    path.join(repository, ".venv/bin/nebula-core"),
    commonGitDir ? path.join(path.dirname(commonGitDir), ".venv/bin/nebula-core") : undefined,
  ].filter((candidate): candidate is string => Boolean(candidate));
  const coreBinary = coreCandidates.find(existsSync);
  if (!coreBinary) throw new Error(`No nebula-core test binary was found in: ${coreCandidates.join(", ")}`);
  const dataDir = await mkdtemp(path.join(tmpdir(), "nebula-playwright-real-core-"));
  const token = "playwright-real-core-token-2026";
  const bindHost = options.bindHost ?? "127.0.0.1";
  const browserHost = options.browserHost ?? bindHost;
  const child = spawn(
    coreBinary,
    [
      "serve",
      "--host", bindHost,
      "--port", "0",
      "--token", token,
      "--allow-insecure-device-pairing",
      "--allow-browser-diagnostics",
      ...(bindHost === "127.0.0.1" ? [] : ["--allow-remote"]),
      "--data-dir", dataDir,
      "--static-dir", path.join(repository, "ui/dist"),
    ],
    {
      cwd: repository,
      env: {
        ...process.env,
        PYTHONUNBUFFERED: "1",
        PYTHONPATH: [path.join(repository, "src"), process.env.PYTHONPATH].filter(Boolean).join(path.delimiter),
      },
    },
  );
  let output = "";
  const origin = await new Promise<string>((resolve, reject) => {
    const timeout = setTimeout(() => reject(new Error(`Real Core did not become ready.\n${output}`)), 30_000);
    const inspect = (chunk: Buffer) => {
      output += chunk.toString("utf8");
      const match = output.match(/"url"\s*:\s*"http:\/\/[^:\"]+:(\d+)"/);
      if (match) {
        clearTimeout(timeout);
        resolve(`http://${browserHost}:${match[1]}`);
      }
    };
    child.stdout.on("data", inspect);
    child.stderr.on("data", (chunk) => {
      output += chunk.toString("utf8");
      if (process.env.NEBULA_TEST_CORE_LOG === "1") process.stderr.write(chunk);
    });
    child.once("exit", (code) => {
      clearTimeout(timeout);
      reject(new Error(`Real Core exited with ${code}.\n${output}`));
    });
  });
  return { process: child, dataDir, origin, token };
}

function localNetworkIpv4(): string {
  for (const addresses of Object.values(networkInterfaces())) {
    for (const address of addresses ?? []) {
      if (address.family === "IPv4" && !address.internal) return address.address;
    }
  }
  throw new Error("A non-loopback IPv4 address is required for LAN acceptance.");
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
  if (process.env.NEBULA_TEST_KEEP_DATA !== "1" && path.basename(core.dataDir).startsWith("nebula-playwright-real-core-")) {
    await rm(core.dataDir, { recursive: true, force: true });
  }
}

interface LocalModelStub {
  origin: string;
  requests: Array<Record<string, unknown>>;
  server: Server;
  fail: boolean;
}

async function startLocalModelStub(options: { fail?: boolean; streamDelayMs?: number } = {}): Promise<LocalModelStub> {
  const requests: Array<Record<string, unknown>> = [];
  const stub: LocalModelStub = { origin: "", requests, server: undefined as unknown as Server, fail: options.fail === true };
  const server = createServer(async (request, response) => {
    response.setHeader("Content-Type", "application/json");
    if (request.method === "GET" && request.url === "/v1/models") {
      response.end(JSON.stringify({
        object: "list",
        data: [{ id: "security-model", object: "model", created: 1, owned_by: "local-acceptance" }],
      }));
      return;
    }
    if (request.method === "POST" && request.url === "/v1/chat/completions") {
      const chunks: Buffer[] = [];
      for await (const chunk of request) chunks.push(Buffer.from(chunk));
      const body = JSON.parse(Buffer.concat(chunks).toString("utf8")) as Record<string, unknown>;
      requests.push(body);
      if (stub.fail) {
        response.statusCode = 503;
        response.end(JSON.stringify({ error: { message: "deliberate acceptance failure" } }));
        return;
      }
      const tools = Array.isArray(body.tools) ? body.tools as Array<{
        function?: { name?: string; parameters?: { properties?: { nonce?: { enum?: string[] } } } };
      }> : [];
      const capabilityProbe = tools.find((tool) => tool.function?.name === "nebula_capability_probe");
      const nonce = capabilityProbe?.function?.parameters?.properties?.nonce?.enum?.[0];
      if (nonce) {
        response.end(JSON.stringify({
          id: "chatcmpl-real-core-probe",
          object: "chat.completion",
          created: 1,
          model: "security-model",
          choices: [{
            index: 0,
            message: {
              role: "assistant",
              content: null,
              tool_calls: [{
                id: "call-real-core-probe",
                type: "function",
                function: { name: "nebula_capability_probe", arguments: JSON.stringify({ nonce }) },
              }],
            },
            finish_reason: "tool_calls",
          }],
          usage: { prompt_tokens: 12, completion_tokens: 4, total_tokens: 16 },
        }));
        return;
      }
      if (tools.some(tool => tool.function?.name === "finish_response")) {
        response.end(JSON.stringify({id: "chatcmpl-queue-routing", object: "chat.completion", created: 1, model: "security-model", choices: [{index: 0, message: {role: "assistant", content: null, tool_calls: [{id: "finish-queue", type: "function", function: {name: "finish_response", arguments: "{}"}}]}, finish_reason: "tool_calls"}], usage: {prompt_tokens: 12, completion_tokens: 4, total_tokens: 16}}));
        return;
      }
      const messages = Array.isArray(body.messages) ? body.messages as Array<{ content?: unknown }> : [];
      if (messages.some((message) => typeof message.content === "string" && message.content.includes("Name this conversation from its first exchange"))) {
        response.end(JSON.stringify({
          id: "chatcmpl-real-core-name",
          object: "chat.completion",
          created: 1,
          model: "security-model",
          choices: [{ index: 0, message: { role: "assistant", content: "Expired HTTPS Certificate Review" }, finish_reason: "stop" }],
          usage: { prompt_tokens: 14, completion_tokens: 5, total_tokens: 19 },
        }));
        return;
      }
      if (body.stream === true && options.streamDelayMs !== undefined) {
        response.setHeader("Content-Type", "text/event-stream");
        response.setHeader("Cache-Control", "no-cache");
        response.flushHeaders();
        response.write(`data: ${JSON.stringify({
          id: "chatcmpl-real-core-stream",
          object: "chat.completion.chunk",
          created: 1,
          model: "security-model",
          choices: [{ index: 0, delta: { role: "assistant", content: "**Core is continuing in Project A**" }, finish_reason: null }],
        })}\n\n`);
        setTimeout(() => {
          response.write(`data: ${JSON.stringify({
            id: "chatcmpl-real-core-stream",
            object: "chat.completion.chunk",
            created: 1,
            model: "security-model",
            choices: [{ index: 0, delta: { content: " and finished after the viewer detached." }, finish_reason: null }],
          })}\n\n`);
          response.write(`data: ${JSON.stringify({
            id: "chatcmpl-real-core-stream",
            object: "chat.completion.chunk",
            created: 1,
            model: "security-model",
            choices: [{ index: 0, delta: {}, finish_reason: "stop" }],
            usage: { prompt_tokens: 12, completion_tokens: 10, total_tokens: 22 },
          })}\n\n`);
          response.end("data: [DONE]\n\n");
        }, options.streamDelayMs);
        return;
      }
      response.end(JSON.stringify({
        id: "chatcmpl-real-core",
        object: "chat.completion",
        created: 1,
        model: "security-model",
        choices: [{
          index: 0,
          message: { role: "assistant", content: "Real Core retained the exact research context." },
          finish_reason: "stop",
        }],
        usage: { prompt_tokens: 18, completion_tokens: 8, total_tokens: 26 },
      }));
      return;
    }
    response.statusCode = 404;
    response.end(JSON.stringify({ error: "not found" }));
  });
  stub.server = server;
  await new Promise<void>((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", () => resolve());
  });
  const address = server.address() as AddressInfo;
  stub.origin = `http://127.0.0.1:${address.port}`;
  return stub;
}

async function stopLocalModelStub(stub: LocalModelStub): Promise<void> {
  stub.server.closeAllConnections();
  await new Promise<void>((resolve, reject) => {
    stub.server.close((error) => error ? reject(error) : resolve());
  });
}

test("production mission defaults to unlimited duration through real Core", async ({ page }) => {
  test.setTimeout(60_000);
  const lanAddress = localNetworkIpv4();
  const core = await startRealCore({ bindHost: "0.0.0.0", browserHost: lanAddress });
  const modelStub = await startLocalModelStub();
  const api = await playwrightRequest.newContext({
    baseURL: `${core.origin}/api/v1/`,
    extraHTTPHeaders: { Authorization: `Bearer ${core.token}` },
  });
  try {
    const engagementsResponse = await api.get("engagements");
    expect(engagementsResponse.ok()).toBe(true);
    const engagements = await engagementsResponse.json() as Array<{ id: string }>;
    const projectId = engagements[0]?.id;
    expect(projectId).toBeTruthy();

    const providerResponse = await api.post("providers", { data: {
      name: "Unlimited mission model",
      provider_type: "vllm",
      endpoint: `${modelStub.origin}/v1`,
      enabled: true,
      is_local: true,
      model_allowlist: ["security-model"],
      privacy: { local_only: true, residency: [], permits_sensitive_data: false },
      metadata: { default_model: "security-model" },
    } });
    expect(providerResponse.ok(), await providerResponse.text()).toBe(true);
    const provider = await providerResponse.json() as { id: string; revision: number };
    const verificationResponse = await api.post(
      `providers/${encodeURIComponent(provider.id)}/capabilities/verify`,
      { data: { model: "security-model", expected_revision: provider.revision } },
    );
    expect(verificationResponse.ok(), await verificationResponse.text()).toBe(true);
    expect(await verificationResponse.json()).toMatchObject({
      verification: { model: "security-model", status: "verified" },
    });

    await page.goto(`${core.origin}/?view=missions#token=${encodeURIComponent(core.token)}`);
    const controls = page.getByRole("region", { name: "Mission controls" });
    await controls.getByRole("button", { name: "Automate task" }).click();
    const dialog = page.getByRole("dialog", { name: "Automate task" });
    await dialog.getByLabel("Mission name").fill("Unlimited production mission");
    await dialog.getByLabel("Objective", { exact: true }).first().fill("Confirm the unlimited mission default");
    await dialog.getByText("Advanced", { exact: true }).click();
    await expect(dialog.getByLabel("Duration (minutes)")).toHaveValue("");
    await expect(dialog.getByLabel("Duration (minutes)")).toHaveAttribute("placeholder", "Unlimited");
    await dialog.getByRole("button", { name: "Automate task" }).click();

    await expect.poll(async () => {
      const response = await api.get("runs", { params: { engagement_id: projectId } });
      expect(response.ok(), await response.text()).toBe(true);
      const runs = await response.json() as Array<{
        metadata?: { name?: string };
        status: string;
        budget: { max_duration_seconds: number | null };
      }>;
      const run = runs.find((item) => item.metadata?.name === "Unlimited production mission");
      return run ? { status: run.status, duration: run.budget.max_duration_seconds } : undefined;
    }, { timeout: 20_000 }).toEqual({ status: "complete", duration: null });
    await expect(page.getByRole("navigation", { name: "Mission history" }).getByText("Unlimited production mission")).toBeVisible();
  } finally {
    await api.dispose();
    await stopLocalModelStub(modelStub);
    await stopRealCore(core);
  }
});

test("production assistant preserves exact research context and relaunch-safe drafts through real Core", async ({ page }) => {
  test.setTimeout(120_000);
  const lanAddress = localNetworkIpv4();
  const core = await startRealCore({ bindHost: "0.0.0.0", browserHost: lanAddress });
  const modelStub = await startLocalModelStub();
  const api = await playwrightRequest.newContext({
    baseURL: `${core.origin}/api/v1/`,
    extraHTTPHeaders: { Authorization: `Bearer ${core.token}` },
  });
  try {
    const engagementsResponse = await api.get("engagements");
    expect(engagementsResponse.ok()).toBe(true);
    const engagements = await engagementsResponse.json() as Array<{ id: string }>;
    const projectId = engagements[0]?.id;
    expect(projectId).toBeTruthy();

    const providerResponse = await api.post("providers", { data: {
      name: "Real Core research model",
      provider_type: "vllm",
      endpoint: `${modelStub.origin}/v1`,
      enabled: true,
      is_local: true,
      model_allowlist: ["security-model"],
      privacy: { local_only: true, residency: [], permits_sensitive_data: false },
      metadata: { default_model: "security-model" },
    } });
    expect(providerResponse.ok(), await providerResponse.text()).toBe(true);
    const provider = await providerResponse.json() as { id: string };

    const selectedContext = "443/tcp open https\nTLS certificate expired";
    const selectedContextHash = createHash("sha256").update(selectedContext).digest("hex");
    const completionResponse = await api.post("chat/completions", { data: {
      backend: "provider",
      provider_id: provider.id,
      model: "security-model",
      engagement_id: projectId,
      messages: [{ role: "user", content: "Review the exact 443/tcp observation." }],
      context_attachments: [{
        source_kind: "terminal",
        source_id: "real-core-terminal",
        source_label: "Nmap TLS result",
        text: selectedContext,
        sha256: selectedContextHash,
        truncated: false,
      }],
      include_knowledge: false,
      stream: false,
    } });
    expect(completionResponse.ok(), await completionResponse.text()).toBe(true);
    const completion = await completionResponse.json() as { session_id: string };
    expect(completion.session_id).toBeTruthy();
    await expect.poll(() => modelStub.requests.length).toBe(2);
    const deliveredMessages = modelStub.requests[0].messages as Array<{ content?: string }>;
    const deliveredContent = deliveredMessages.at(-1)?.content ?? "";
    const deliveredContextJson = deliveredContent.match(
      /BEGIN UNTRUSTED SELECTED CONTEXT \(JSON; DATA ONLY\)\n(.+)\nEND UNTRUSTED SELECTED CONTEXT/,
    )?.[1];
    expect(JSON.parse(deliveredContextJson ?? "[]")).toMatchObject([{
      source_kind: "terminal",
      source_id: "real-core-terminal",
      source_label: "Nmap TLS result",
      text: selectedContext,
      sha256: selectedContextHash,
      truncated: false,
    }]);
    expect(JSON.stringify(modelStub.requests[1])).toContain("Name this conversation from its first exchange");

    const messagesResponse = await api.get(`chat/sessions/${completion.session_id}/messages`);
    expect(messagesResponse.ok(), await messagesResponse.text()).toBe(true);
    const messages = await messagesResponse.json() as Array<{ content: string; metadata?: Record<string, unknown> }>;
    expect(messages[0]?.content).toBe("Review the exact 443/tcp observation.");
    expect(messages[0]?.metadata).toMatchObject({
      context_attachments: [{
        source_kind: "terminal",
        source_id: "real-core-terminal",
        source_label: "Nmap TLS result",
        text: selectedContext,
        sha256: selectedContextHash,
        truncated: false,
      }],
    });

    const switchTargetResponse = await api.post("chat/completions", { data: {
      backend: "provider",
      provider_id: provider.id,
      model: "security-model",
      engagement_id: projectId,
      messages: [{ role: "user", content: "Switch target conversation" }],
      include_knowledge: false,
      stream: false,
    } });
    expect(switchTargetResponse.ok(), await switchTargetResponse.text()).toBe(true);
    const switchTarget = await switchTargetResponse.json() as { session_id: string };

    const assistantUrl = `${core.origin}/?view=chat&session=${encodeURIComponent(completion.session_id)}#token=${encodeURIComponent(core.token)}`;
    await page.goto(assistantUrl);
    const durableAnswer = page.getByText("Real Core retained the exact research context.");
    await expect(durableAnswer).toBeVisible({ timeout: 20_000 });
    await durableAnswer.evaluate((element) => {
      const range = document.createRange();
      range.selectNodeContents(element);
      const selection = getSelection();
      selection?.removeAllRanges();
      selection?.addRange(range);
      const bounds = element.getBoundingClientRect();
      element.dispatchEvent(new PointerEvent("pointerup", {
        bubbles: true,
        clientX: bounds.left + Math.min(24, bounds.width / 2),
        clientY: bounds.top + bounds.height / 2,
      }));
    });
    await page.getByRole("button", { name: "Ask Nebula" }).click();
    await expect.poll(() => new URL(page.url()).searchParams.get("session")).toBe(completion.session_id);
    await expect.poll(() => new URL(page.url()).searchParams.get("handoff")).not.toBeNull();
    await expect(durableAnswer).toBeVisible();
    await expect(page.getByRole("region", { name: "Selected context pack" })).toBeVisible();
    await expect(page.getByText("BEGIN UNTRUSTED SELECTED CONTEXT", { exact: false })).toHaveCount(0);
    await page.getByRole("button", { name: /Open context details/ }).click();
    const inspector = page.getByLabel("Session inspector");
    await expect(inspector.getByRole("heading", { name: "Working context" })).toBeVisible();
    await expect(inspector.getByText(/estimated.*target input tokens/)).toBeVisible();

    const composer = page.getByRole("textbox", { name: "Message the analyst assistant" });
    await composer.fill("draft retained across a production relaunch");
    // Core intentionally keeps bearer credentials in memory. Re-open the same
    // authorized launch URL to exercise a fresh document without weakening that boundary.
    await page.goto(assistantUrl);
    await expect(page.getByText("Real Core retained the exact research context.")).toBeVisible({ timeout: 20_000 });
    await expect(page.getByRole("textbox", { name: "Message the analyst assistant" })).toHaveValue("draft retained across a production relaunch");

    await page.getByRole("button", { name: "Show conversations" }).click();
    const sessionsResponse = await api.get("chat-sessions");
    expect(sessionsResponse.ok(), await sessionsResponse.text()).toBe(true);
    const sessions = await sessionsResponse.json() as Array<{ id: string; title: string }>;
    const savedSession = sessions.find((session) => session.id === completion.session_id);
    expect(savedSession).toBeTruthy();
    expect(savedSession!.title).toBe("Expired HTTPS Certificate Review");
    const switchTargetSession = page.locator(`.session-select[data-session-id="${switchTarget.session_id}"]`);
    await expect(switchTargetSession).toBeVisible();
    await switchTargetSession.click({ force: true });
    await expect.poll(() => new URL(page.url()).searchParams.get("session")).toBe(switchTarget.session_id);
    await expect(page.locator(".chat-message.operator").getByText("Switch target conversation", { exact: true })).toBeVisible();
    const sourceSession = page.locator(`.session-select[data-session-id="${completion.session_id}"]`);
    await expect(sourceSession).toBeVisible();
    await sourceSession.click({ force: true });
    await expect.poll(() => new URL(page.url()).searchParams.get("session")).toBe(completion.session_id);
    await expect(page.locator(".chat-message.operator").getByText("Review the exact 443/tcp observation.", { exact: true })).toBeVisible();
    const activeConversation = page.locator(".session-list-item.active");
    const activeConversationActions = page.locator(".session-list-item.active .session-actions-trigger");
    await activeConversation.hover();
    await activeConversationActions.click();
    await expect(page.getByRole("menuitem", { name: "Copy link" })).toBeFocused();
    await page.keyboard.press("Enter");
    await expect(page.getByText("Conversation link copied without authentication material.")).toBeVisible();

    await activeConversation.hover();
    await activeConversationActions.click();
    const exportMenuItem = page.getByRole("menuitem", { name: "Export transcript" });
    await exportMenuItem.click({ force: true });
    const exportDialog = page.getByRole("dialog").filter({ hasText: "The Markdown transcript can contain sensitive prompts" });
    await expect(exportDialog).toBeVisible();
    const downloadPromise = page.waitForEvent("download");
    await exportDialog.getByRole("button", { name: "Export transcript" }).click();
    const download = await downloadPromise;
    const transcriptPath = await download.path();
    expect(transcriptPath).toBeTruthy();
    const transcript = await readFile(transcriptPath!, "utf8");
    expect(transcript).toContain("Real Core retained the exact research context.");
    expect(transcript).toContain(selectedContext);
    expect(transcript).toContain(selectedContextHash);
    expect(new URL(page.url()).hostname).toBe(lanAddress);
  } finally {
    await api.dispose();
    await stopRealCore(core);
    await stopLocalModelStub(modelStub);
  }
});

test("production assistant work survives a project switch through real Core", async ({ page }) => {
  test.setTimeout(60_000);
  const lanAddress = localNetworkIpv4();
  const core = await startRealCore({ bindHost: "0.0.0.0", browserHost: lanAddress });
  const modelStub = await startLocalModelStub({ streamDelayMs: 1_500 });
  const api = await playwrightRequest.newContext({
    baseURL: `${core.origin}/api/v1/`,
    extraHTTPHeaders: { Authorization: `Bearer ${core.token}` },
  });
  try {
    const projectsResponse = await api.get("engagements");
    expect(projectsResponse.ok()).toBe(true);
    const projects = await projectsResponse.json() as Array<{ id: string; name: string }>;
    const projectA = projects[0];
    expect(projectA).toBeTruthy();
    const projectBResponse = await api.post("engagements", { data: {
      name: "Background Project B",
      description: "Project switch acceptance target",
      status: "active",
      tags: [],
    } });
    expect(projectBResponse.ok(), await projectBResponse.text()).toBe(true);

    const providerResponse = await api.post("providers", { data: {
      name: "Real Core streaming model",
      provider_type: "vllm",
      endpoint: `${modelStub.origin}/v1`,
      enabled: true,
      is_local: true,
      model_allowlist: ["security-model"],
      privacy: { local_only: true, residency: [], permits_sensitive_data: false },
      metadata: { default_model: "security-model" },
    } });
    expect(providerResponse.ok(), await providerResponse.text()).toBe(true);

    await page.addInitScript((projectId) => localStorage.setItem("nebula.engagement", projectId), projectA.id);
    await page.goto(`${core.origin}/?view=chat#token=${encodeURIComponent(core.token)}`);
    await page.getByRole("button", { name: "New chat", exact: true }).click();
    const composer = page.getByRole("textbox", { name: "Message the analyst assistant" });
    await expect(composer).toBeEnabled({ timeout: 20_000 });
    await composer.fill("Keep this response running while I switch projects");
    await page.getByRole("button", { name: "Send message" }).click();
    await expect(page.locator(".chat-message.assistant .assistant-markdown strong")).toHaveText("Core is continuing in Project A", { timeout: 20_000 });

    await expect.poll(() => new URL(page.url()).searchParams.get("session")).toBeTruthy();
    const sourceSessionId = new URL(page.url()).searchParams.get("session")!;
    await page.getByRole("button", { name: "Switch project" }).click();
    await page.getByRole("dialog", { name: "Project switcher" }).getByRole("button", { name: "Background Project B active", exact: true }).click();
    await expect(page.getByRole("button", { name: "Switch project" })).toContainText("Background Project B");

    await expect.poll(async () => {
      const messagesResponse = await api.get(`chat/sessions/${sourceSessionId}/messages`);
      if (!messagesResponse.ok()) return "";
      const messages = await messagesResponse.json() as Array<{ role: string; content: string }>;
      return messages.find((message) => message.role === "assistant")?.content ?? "";
    }, { timeout: 20_000 }).toBe("**Core is continuing in Project A** and finished after the viewer detached.");

    await page.getByRole("button", { name: "Switch project" }).click();
    await page.getByRole("dialog", { name: "Project switcher" }).getByRole("button", { name: `${projectA.name} active`, exact: true }).click();
    await page.getByRole("button", { name: "Show conversations" }).click();
    await page.goto(`${core.origin}/?view=chat&session=${sourceSessionId}#token=${encodeURIComponent(core.token)}`);
    await expect(page.locator(".chat-message.assistant .assistant-markdown strong")).toHaveText("Core is continuing in Project A", { timeout: 20_000 });
    await expect(page.locator(".chat-message.assistant .assistant-markdown")).toContainText("and finished after the viewer detached.");
    expect(new URL(page.url()).hostname).toBe(lanAddress);
  } finally {
    await api.dispose();
    await stopLocalModelStub(modelStub);
    await stopRealCore(core);
  }
});

test("production LAN mission ledger survives failure, retry, and relaunch through real Core", async ({ page }) => {
  test.setTimeout(90_000);
  const lanAddress = localNetworkIpv4();
  const core = await startRealCore({ bindHost: "0.0.0.0", browserHost: lanAddress });
  const modelStub = await startLocalModelStub({ fail: true });
  const api = await playwrightRequest.newContext({
    baseURL: `${core.origin}/api/v1/`,
    extraHTTPHeaders: { Authorization: `Bearer ${core.token}` },
  });
  try {
    const engagementsResponse = await api.get("engagements");
    expect(engagementsResponse.ok()).toBe(true);
    const engagements = await engagementsResponse.json() as Array<{ id: string }>;
    const projectId = engagements[0]?.id;
    expect(projectId).toBeTruthy();

    const providerResponse = await api.post("providers", { data: {
      name: "Mission ledger acceptance model",
      provider_type: "vllm",
      endpoint: `${modelStub.origin}/v1`,
      enabled: true,
      is_local: true,
      model_allowlist: ["security-model"],
      privacy: { local_only: true, residency: [], permits_sensitive_data: false },
      metadata: { default_model: "security-model" },
    } });
    expect(providerResponse.ok(), await providerResponse.text()).toBe(true);
    const provider = await providerResponse.json() as { id: string };

    const missionResponse = await api.post("missions", { data: {
      engagement_id: projectId,
      name: "Durable ledger recovery",
      objective: "Summarize the verified scope without executable tools.",
      backend: "native",
      provider_id: provider.id,
      model: "security-model",
      stages: [{ title: "Analyze", objective: "Review scope" }, { title: "Verify", objective: "Record the outcome" }],
      max_tool_calls: 0,
      max_retries: 0,
      max_concurrency: 1,
    } });
    expect(missionResponse.ok(), await missionResponse.text()).toBe(true);
    const failedMission = await missionResponse.json() as { id: string };
    await expect.poll(async () => {
      const response = await api.get(`runs?engagement_id=${encodeURIComponent(projectId!)}`);
      const runs = await response.json() as Array<{ id: string; status: string }>;
      return runs.find((run) => run.id === failedMission.id)?.status;
    }, { timeout: 30_000 }).toBe("failed");

    const missionUrl = `${core.origin}/?view=missions#token=${encodeURIComponent(core.token)}`;
    await page.goto(missionUrl);
    await expect(page.getByText("Durable ledger recovery", { exact: true }).first()).toBeVisible({ timeout: 20_000 });
    await expect(page.getByRole("region", { name: "Mission activity" })).toBeVisible();
    await expect(page.getByText("item upsert", { exact: false })).toHaveCount(0);
    await expect(page.getByText("This mission failed")).toBeVisible();

    modelStub.fail = false;
    const retryResponse = await api.post(`runs/${failedMission.id}/retry`, { data: { allow_cloud_tool_results: false } });
    expect(retryResponse.ok(), await retryResponse.text()).toBe(true);
    const retriedMission = await retryResponse.json() as { id: string };
    await expect.poll(async () => {
      const response = await api.get(`runs?engagement_id=${encodeURIComponent(projectId!)}`);
      const runs = await response.json() as Array<{ id: string; status: string }>;
      return runs.find((run) => run.id === retriedMission.id)?.status;
    }, { timeout: 30_000 }).toBe("complete");

    // Core intentionally keeps bearer credentials in memory; use the same
    // authorized launch URL to exercise a fresh production document.
    await page.goto(missionUrl);
    await expect(page.getByText("Durable ledger recovery", { exact: true }).first()).toBeVisible({ timeout: 20_000 });
    const ledger = page.getByRole("region", { name: "Mission activity" });
    await expect(ledger).toBeVisible();
    await expect(ledger.getByText(/actions?$/)).toBeVisible();
    await expect(page.getByText("item upsert", { exact: false })).toHaveCount(0);
    expect(new URL(page.url()).hostname).toBe(lanAddress);
  } finally {
    await api.dispose();
    await stopRealCore(core);
    await stopLocalModelStub(modelStub);
  }
});

test("real Core Browser shows durable scope and an honest device-browser handoff", async ({ page }) => {
  test.setTimeout(60_000);
  const lanAddress = localNetworkIpv4();
  const core = await startRealCore({ bindHost: "0.0.0.0", browserHost: lanAddress });
  const api = await playwrightRequest.newContext({
    baseURL: `${core.origin}/api/v1/`,
    extraHTTPHeaders: { Authorization: `Bearer ${core.token}` },
  });
  try {
    const engagementsResponse = await api.get("engagements");
    expect(engagementsResponse.ok()).toBe(true);
    const engagements = await engagementsResponse.json() as Array<{ id: string }>;
    const projectId = engagements[0]?.id;
    expect(projectId).toBeTruthy();
    const scopeResponse = await api.get(`engagements/${projectId}/scope`);
    const scope = await scopeResponse.json() as { revision: number };
    const update = await api.put(`engagements/${projectId}/scope`, { data: {
      allowed_cidrs: [],
      allowed_domains: ["example.com"],
      allowed_urls: [],
      allowed_ports: [443],
      not_before: null,
      not_after: null,
      prohibited_actions: [],
      local_only: true,
      max_concurrency: 1,
      grants: [],
      expected_revision: scope.revision,
    } });
    expect(update.ok(), await update.text()).toBe(true);
    const browserWorkspaceResponse = await api.get(`engagements/${projectId}/browser-workspace`);
    expect(browserWorkspaceResponse.ok(), await browserWorkspaceResponse.text()).toBe(true);
    const browserWorkspace = await browserWorkspaceResponse.json() as {
      identities: Array<{ id: string }>;
      sessions: Array<{ id: string }>;
    };
    const assessmentResponse = await api.post(
      `engagements/${projectId}/browser-assessments`,
      { data: {
        name: "LAN guided assessment",
        objective: "Map the authorized account surface and preserve evidence.",
        profile: "explore",
        session_id: browserWorkspace.sessions[0].id,
        identity_ids: [browserWorkspace.identities[0].id],
        primary_identity_id: browserWorkspace.identities[0].id,
        target_urls: ["https://example.com/"],
      } },
    );
    expect(assessmentResponse.ok(), await assessmentResponse.text()).toBe(true);
    const assessment = await assessmentResponse.json() as { id: string; status: string };
    expect(assessment.status).toBe("draft");

    await page.addInitScript(() => {
      window.open = () => ({}) as Window;
    });
    await page.goto(`${core.origin}/?view=browser#token=${encodeURIComponent(core.token)}`);
    await expect(page.getByRole("button", { name: "Nebula Core ready" })).toBeVisible({ timeout: 20_000 });
    await page.getByLabel("Browser engine").selectOption("native");
    await expect(page.getByText("Browse from this device")).toBeVisible();
    await expect(page.getByText(/No target · Open a page to compare it with Project scope/)).toBeVisible();
    const address = page.getByRole("textbox", { name: "Web address" });
    await address.fill("https://example.com/account");
    await page.getByRole("button", { name: "Open", exact: true }).click();
    await expect(page.getByText(/In scope · Matches Project scope revision 2/)).toBeVisible();
    await expect(page.getByText("The isolated embedded webview is a desktop-app capability.")).toBeVisible();
    await expect(page.getByRole("button", { name: "Ask Nebula about the live page" })).toHaveCount(0);

    await page.getByRole("button", { name: "Research workbench" }).click();
    await expect(page.getByRole("heading", { name: "LAN guided assessment" })).toBeVisible();
    await expect(page.getByRole("button", { name: "Prepare / Retry" })).toBeVisible();
    await expect(page.getByText(/Manual legacy browsing remains available/)).toBeVisible();
    await expect.poll(() => new URL(page.url()).searchParams.get("assessment")).toBe(assessment.id);
    await page.getByRole("button", { name: "Repeater", exact: true }).click();
    await expect(page.getByRole("heading", { name: "Repeater" })).toBeVisible();
    await page.getByLabel("Name").fill("Durable account request");
    await page.getByLabel("URL", { exact: true }).fill("https://example.com/account");
    await page.getByRole("button", { name: "Save Repeater request" }).click();
    await expect(page.getByRole("status").filter({ hasText: "Repeater request saved" })).toBeVisible();
    await expect(page.getByText("Durable account request", { exact: true })).toBeVisible();
    await expect(page.getByRole("button", { name: "Send once" })).toBeDisabled();
    await page.goto(`${core.origin}/?view=browser&browserTool=repeater#token=${encodeURIComponent(core.token)}`);
    await expect(page.getByRole("button", { name: "Nebula Core ready" })).toBeVisible({ timeout: 20_000 });
    await page.getByLabel("Browser engine").selectOption("native");
    await page.getByRole("button", { name: "Research workbench" }).click();
    await expect.poll(() => new URL(page.url()).searchParams.get("tool")).toBe("repeater");
    expect(new URL(page.url()).searchParams.has("browserTool")).toBe(false);
    await page.getByRole("button", { name: "Repeater", exact: true }).click();
    await expect(page.getByText("Durable account request", { exact: true })).toBeVisible();
    expect(new URL(page.url()).hostname).toBe(lanAddress);
  } finally {
    await api.dispose();
    await stopRealCore(core);
  }
});

test("real Core persists network scope changed through universal settings search", async ({ page }) => {
  test.setTimeout(60_000);
  const lanAddress = localNetworkIpv4();
  const core = await startRealCore({ bindHost: "0.0.0.0", browserHost: lanAddress });
  const api = await playwrightRequest.newContext({
    baseURL: `${core.origin}/api/v1/`,
    extraHTTPHeaders: { Authorization: `Bearer ${core.token}` },
  });
  try {
    await page.goto(`${core.origin}/findings#token=${encodeURIComponent(core.token)}`);
    await expect(page.getByRole("heading", { name: "Findings", exact: true })).toBeVisible({ timeout: 20_000 });
    await page.getByRole("button", { name: "Search pages, actions, and settings" }).click();
    await page.getByRole("textbox", { name: "Search pages, actions, and settings" }).fill("network ports");
    await page.getByRole("option", { name: /Project policy and network scope/ }).click();
    const lens = page.getByRole("dialog", { name: "Project policy and network scope" });
    const saveScope = page.getByRole("button", { name: "Save scope" });
    await expect(saveScope).toBeEnabled({ timeout: 20_000 });
    await page.getByLabel("Allowed domains").fill("https://www.Google.com/");
    await page.getByLabel("All targets and ports").check();
    await saveScope.click();
    const confirmation = page.getByRole("dialog", { name: "Allow every network target and port?" });
    await expect(confirmation).toBeVisible();
    await confirmation.getByRole("button", { name: "Allow all targets" }).click();
    await expect(lens.getByRole("status").filter({ hasText: "Network scope updated" })).toBeVisible();

    const engagements = await (await api.get("engagements")).json() as Array<{ id: string }>;
    const saved = await api.get(`engagements/${engagements[0].id}/scope`);
    expect(saved.ok(), await saved.text()).toBe(true);
    expect(await saved.json()).toMatchObject({
      allowed_domains: ["www.google.com"],
      allow_all_targets: true,
    });

    await page.goto("about:blank");
    await page.goto(`${core.origin}/findings#token=${encodeURIComponent(core.token)}`);
    await expect(page.getByRole("heading", { name: "Findings", exact: true })).toBeVisible({ timeout: 20_000 });
    await page.getByRole("button", { name: "Search pages, actions, and settings" }).click();
    await page.getByRole("textbox", { name: "Search pages, actions, and settings" }).fill("network ports");
    const reloadedScope = page.waitForResponse((response) => response.url().endsWith("/scope") && response.request().method() === "GET");
    await page.getByRole("option", { name: /Project policy and network scope/ }).click();
    expect(await (await reloadedScope).json()).toMatchObject({ allow_all_targets: true });
    await expect(lens.getByLabel("All targets and ports")).toBeChecked();
    await expect(lens.getByLabel("Allowed domains")).toHaveValue("www.google.com");
    await expect(lens.getByLabel("Allowed domains")).toBeDisabled();
    expect(new URL(page.url()).hostname).toBe(lanAddress);
  } finally {
    await api.dispose();
    await stopRealCore(core);
  }
});

test("production LAN handoff survives reload without persisting unsent bytes", async ({ page }) => {
  test.setTimeout(60_000);
  const lanAddress = localNetworkIpv4();
  const core = await startRealCore({ bindHost: "0.0.0.0", browserHost: lanAddress });
  const api = await playwrightRequest.newContext({
    baseURL: `${core.origin}/api/v1/`,
    extraHTTPHeaders: { Authorization: `Bearer ${core.token}` },
  });
  try {
    const engagementsResponse = await api.get("engagements");
    expect(engagementsResponse.ok()).toBe(true);
    const engagements = await engagementsResponse.json() as Array<{ id: string }>;
    const projectId = engagements[0]?.id;
    expect(projectId).toBeTruthy();

    const unsentBytes = "UNSENT_REAL_CORE_SELECTION_MUST_NOT_PERSIST";
    const createdResponse = await api.post("handoffs", { data: {
      project_id: projectId,
      source_refs: [],
      action_id: "ask_nebula",
      origin_device_id: "paired-mac",
      source_hashes: {},
      source_labels: {},
      transient: true,
    } });
    expect(createdResponse.ok(), await createdResponse.text()).toBe(true);
    const envelope = await createdResponse.json() as { id: string; revision: number };
    expect(JSON.stringify(envelope)).not.toContain(unsentBytes);

    const localPairingApi = await playwrightRequest.newContext({
      baseURL: `http://127.0.0.1:${new URL(core.origin).port}/api/v1/`,
      extraHTTPHeaders: { Authorization: `Bearer ${core.token}` },
    });
    const pairingResponse = await localPairingApi.post("auth/pairings", { data: { name: "Handoff recovery browser" } });
    expect(pairingResponse.ok(), await pairingResponse.text()).toBe(true);
    const pairing = await pairingResponse.json() as { secret: string; confirmation_code: string };
    await localPairingApi.dispose();
    await page.goto(`${core.origin}/#pair=${encodeURIComponent(pairing.secret)}&code=${encodeURIComponent(pairing.confirmation_code)}`);
    await page.getByLabel("Device name").fill("Handoff recovery browser");
    await page.getByRole("button", { name: "Pair device" }).click();
    await expect(page.getByRole("button", { name: "Nebula Core ready" })).toBeVisible({ timeout: 20_000 });

    const route = `/projects/${encodeURIComponent(projectId)}/workbench?view=chat&handoff=${encodeURIComponent(envelope.id)}`;
    await page.goto(`${core.origin}${route}`);
    await expect(page.getByText("Resume on the originating device", { exact: true })).toBeVisible({ timeout: 20_000 });
    await expect(page.getByText(/Unsent selected bytes remained only in memory on paired-mac/)).toBeVisible();
    expect(new URL(page.url()).hostname).toBe(lanAddress);

    await page.reload();
    await expect(page.getByText("Resume on the originating device", { exact: true })).toBeVisible({ timeout: 20_000 });
    const refreshedResponse = await api.get(`handoffs/${encodeURIComponent(envelope.id)}?device_id=linux-browser`);
    expect(refreshedResponse.ok(), await refreshedResponse.text()).toBe(true);
    const refreshedText = await refreshedResponse.text();
    expect(refreshedText).not.toContain(unsentBytes);
    expect(JSON.parse(refreshedText)).toMatchObject({ recovery: "resume_origin" });
  } finally {
    await api.dispose();
    await stopRealCore(core);
  }
});

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

test("assistant upgrade mobile Code keeps its controls readable and saves to authoritative real-Core state", async ({ page }) => {
  test.setTimeout(120_000);
  // WebKit fill() can report success without inserting into a shadow-root
  // contenteditable. Exercise real keyboard input and check the visible draft.
  const replaceEditorText = async (text: string) => {
    const editor = page.getByRole("textbox", { name: "Code editor" });
    await editor.press("Meta+A");
    await editor.press("Control+A");
    await page.keyboard.insertText(text);
    await expect(editor).toContainText(text.trim());
  };
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
    await replaceEditorText("real Core mobile proof\n");

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

    await page.getByRole("textbox", { name: "Code editor" }).press("Control+S");
    await expect(page.getByText("Saved /workspace/mobile-proof.txt. Use it from Terminal when you're ready.")).toBeVisible();
    const listingResponse = await api.get(`engagements/${projectId}/workspace?path=&offset=0&limit=100`);
    expect(listingResponse.ok()).toBe(true);
    expect(JSON.stringify(await listingResponse.json())).toContain("mobile-proof.txt");
    const fileResponse = await api.get(`engagements/${projectId}/workspace/download?path=mobile-proof.txt`);
    expect(fileResponse.ok()).toBe(true);
    expect(await fileResponse.text()).toBe("real Core mobile proof\n");

    await page.getByRole("button", { name: "New editor file" }).click();
    await page.getByRole("textbox", { name: "File path" }).fill("scanner.py");
    await replaceEditorText("def scan_target():\n    return True\n");
    await page.getByRole("textbox", { name: "Code editor" }).press("Control+S");
    await expect(page.getByText("Saved /workspace/scanner.py. Use it from Terminal when you're ready.")).toBeVisible();
    const scannerResponse = await api.get(`engagements/${projectId}/workspace/download?path=scanner.py`);
    expect(scannerResponse.ok()).toBe(true);
    expect(await scannerResponse.text()).toBe("def scan_target():\n    return True\n");
    const workspaceRoot = path.join(core.dataDir, "engagement-workspaces", createHash("sha256").update(projectId!).digest("hex"));
    await mkdir(path.join(workspaceRoot, ".vscode"), { recursive: true });
    await writeFile(path.join(workspaceRoot, ".vscode", "tasks.json"), `{
      // Real-Core compatibility fixture.
      tasks: [
        { label: 'Inspect Python', type: 'process', command: 'python', args: ['--version'], group: 'test' },
        { label: 'Extension-owned task', type: 'npm', command: 'test' },
      ],
    }`, "utf8");
    await writeFile(path.join(workspaceRoot, ".vscode", "launch.json"), `{
      configurations: [
        { name: 'Debug active scanner', type: 'debugpy', request: 'launch', program: '${"${file}"}', args: ['--fixture', '${"${workspaceFolder}"}/sample.bin'] },
        { name: 'Attach process', type: 'debugpy', request: 'attach' },
      ],
    }`, "utf8");
    const configuredTasks = await api.get(`engagements/${projectId}/workspace/tasks`);
    expect(configuredTasks.ok(), await configuredTasks.text()).toBe(true);
    expect(JSON.stringify(await configuredTasks.json())).toContain("Inspect Python");
    const configuredLaunch = await api.get(`engagements/${projectId}/workspace/debug-configurations`, { params: { path: "scanner.py" } });
    expect(configuredLaunch.ok(), await configuredLaunch.text()).toBe(true);
    expect(JSON.stringify(await configuredLaunch.json())).toContain("Debug active scanner");
    await expect(page.getByRole("tab", { name: /mobile-proof\.txt/ })).toBeVisible();
    await expect(page.getByRole("tab", { name: /scanner\.py/ })).toHaveAttribute("aria-selected", "true");
    await expect(page.getByRole("button", { name: "Project tasks and tests" })).toBeVisible();
    const taskRequest = page.waitForResponse((response) => response.url().includes("/workspace/tasks"));
    await page.getByRole("button", { name: "Project tasks and tests" }).click();
    const taskResponse = await taskRequest;
    expect(taskResponse.url()).toContain(encodeURIComponent(projectId!));
    expect(JSON.stringify(await taskResponse.json())).toContain("Inspect Python");
    const taskReview = page.getByRole("dialog", { name: "Project tasks and tests" });
    await expect(taskReview.getByRole("option", { name: /Inspect Python/ })).toBeEnabled();
    await expect(taskReview.getByRole("option", { name: /Extension-owned task/ })).toBeDisabled();
    await expect(taskReview).toContainText("requires a VS Code extension");
    await taskReview.getByRole("button", { name: "Close project tasks" }).click();
    await page.getByRole("button", { name: "Debug saved Python" }).click();
    const debuggerReview = page.getByRole("dialog", { name: "Python debugger" });
    await expect(debuggerReview).toContainText("Isolated launch review");
    await expect(debuggerReview).toContainText("project is read-only, networking is disabled");
    await expect(debuggerReview.getByRole("button", { name: "Start isolated debugger" })).toBeEnabled();
    await expect(debuggerReview.getByLabel("Launch profile").locator("option:checked")).toHaveText("Debug active scanner");
    await expect(debuggerReview.getByLabel("Python arguments (JSON array)")).toHaveValue('["--fixture","/workspace/sample.bin"]');
    await expect(debuggerReview.getByLabel("Unsupported launch profiles")).toContainText("Attach profiles cannot cross Nebula's isolated debug boundary.");
    await debuggerReview.getByRole("button", { name: "Close debugger" }).click();

    await page.keyboard.press("Control+P");
    const quickOpen = page.getByRole("dialog", { name: "Quick open" });
    await expect(quickOpen).toBeVisible();
    await quickOpen.getByRole("textbox", { name: "Find a workspace file" }).fill("mobile-proof");
    await quickOpen.getByRole("option", { name: /mobile-proof\.txt/ }).click();
    await expect(page.getByRole("tab", { name: /mobile-proof\.txt/ })).toHaveAttribute("aria-selected", "true");
    await expect(page.locator(".cm-line").first()).toHaveText("real Core mobile proof");

    await page.keyboard.press("Control+Shift+F");
    const textSearch = page.getByRole("dialog", { name: "Search workspace text" });
    await textSearch.getByRole("textbox", { name: "Search workspace text" }).fill("scan_target");
    await textSearch.getByRole("option", { name: /scanner\.py.*Line 1/ }).click();
    await expect(page.getByRole("tab", { name: /scanner\.py/ })).toHaveAttribute("aria-selected", "true");

    await page.getByRole("button", { name: "Preserve as Evidence" }).click();
    const preserveDialog = page.getByRole("dialog", { name: "Preserve scanner.py as Evidence?" });
    await preserveDialog.getByRole("button", { name: "Preserve as Evidence" }).click();
    await expect(page.getByText(/Preserved scanner\.py as Evidence/)).toBeVisible();
    const evidenceResponse = await api.get(`evidence?engagement_id=${projectId}&offset=0&limit=100`);
    expect(evidenceResponse.ok()).toBe(true);
    expect(JSON.stringify(await evidenceResponse.json())).toContain("scanner.py");

    await writeFile(path.join(workspaceRoot, "scanner.py"), "def scan_target():\n    return False  # Terminal edit\n", "utf8");
    await page.getByRole("button", { name: "Chat", exact: true }).click();
    await page.getByRole("button", { name: "More workbench views" }).click();
    await page.getByRole("dialog", { name: "More views" }).getByRole("button", { name: /Code/ }).click();
    await expect(page.getByRole("textbox", { name: "Code editor" })).toContainText("return False  # Terminal edit");
    await expect(page.getByText("Workspace synchronized: 1 reloaded.")).toBeVisible();

    await replaceEditorText("def scan_target():\n    return 'unsaved operator draft'\n");
    await writeFile(path.join(workspaceRoot, "scanner.py"), "def scan_target():\n    return 'newer agent edit'\n\nscan_target()\n", "utf8");
    await page.getByRole("button", { name: "Chat", exact: true }).click();
    await page.getByRole("button", { name: "More workbench views" }).click();
    await page.getByRole("dialog", { name: "More views" }).getByRole("button", { name: /Code/ }).click();
    await expect(page.getByText("Newer workspace version detected")).toBeVisible();
    await expect(page.getByRole("textbox", { name: "Code editor" })).toContainText("unsaved operator draft");
    await page.getByRole("button", { name: "Reload", exact: true }).click();
    const reload = page.getByRole("dialog", { name: "Reload the workspace file?" });
    await reload.getByRole("button", { name: "Reload file" }).click();
    await expect(page.getByRole("textbox", { name: "Code editor" })).toContainText("newer agent edit");

    await expect(page.getByText(/Python · open-buffer intelligence ready/)).toBeVisible({ timeout: 20_000 });
    const editor = page.getByRole("textbox", { name: "Code editor" });
    await page.locator(".cm-line").nth(3).click();
    await editor.press("Home");
    await editor.press("ArrowRight");
    await page.getByRole("button", { name: "Go to definition" }).click();
    await expect(page.getByText(/^Ln 1, Col /)).toBeVisible();
    await page.locator(".cm-line").nth(3).click();
    await editor.press("Home");
    await editor.press("ArrowRight");
    await page.getByRole("button", { name: "Find references" }).click();
    const references = page.getByRole("listbox", { name: "Reference list" });
    await expect(references).toBeVisible();
    await expect(references.getByRole("option")).toHaveCount(2);

    await page.getByRole("button", { name: "Candidate finding" }).click();
    const findingHandoff = page.getByRole("dialog", { name: "Draft an evidence-backed candidate finding?" });
    await expect(findingHandoff).toContainText("Nothing is validated or confirmed automatically");
    await findingHandoff.getByRole("button", { name: "Continue to Findings" }).click();
    await expect(page).toHaveURL(/\/findings(?:\?|$)/);
    const candidate = page.getByRole("dialog", { name: "Create candidate finding" });
    await expect(candidate.getByLabel("Title")).toHaveValue(/scanner\.py:\d+ security observation/);
    await expect(candidate.getByLabel("Description")).toContainText("Source: /workspace/scanner.py:");
    await expect(candidate.getByLabel("Description")).toContainText("Evidence record:");
    await expect(candidate.getByRole("combobox", { name: "Severity", exact: true })).toHaveValue("info");
    await candidate.getByRole("button", { name: "Create candidate" }).click();
    await expect(candidate).toBeHidden();
    const findingsResponse = await api.get(`findings?engagement_id=${projectId}&offset=0&limit=100`);
    expect(findingsResponse.ok()).toBe(true);
    const createdFindings = await findingsResponse.json() as Array<{ status: string; evidence_ids: string[] }>;
    expect(createdFindings.some((finding) => finding.status === "candidate" && finding.evidence_ids.length === 1)).toBe(true);

    await page.waitForTimeout(350);
    await page.goto(`${core.origin}/?view=code#token=${encodeURIComponent(core.token)}`);
    await expect(page.getByRole("tab", { name: /scanner\.py/ })).toHaveAttribute("aria-selected", "true");
    await expect(page.getByRole("textbox", { name: "Code editor" })).toContainText("scan_target");

    await page.getByRole("button", { name: "New editor file" }).click();
    await page.getByRole("textbox", { name: "File path" }).fill("hot-exit-notes.txt");
    await replaceEditorText("exact unsaved λ research draft\n");
    await expect(page.getByText(/^3 open · 1 unsaved · recovery on$/)).toHaveText("3 open · 1 unsaved · recovery on");

    // Keep the last draft beyond the former 20-buffer recovery cutoff active.
    for (let index = 4; index <= 21; index++) {
      await page.getByRole("button", { name: "New editor file" }).click();
      await page.getByRole("textbox", { name: "File path" }).fill(`recovery-${index}.txt`);
      await replaceEditorText(`unsaved draft ${index} λ`);
    }
    // Observe the actual IndexedDB transaction, not just the previous ready UI.
    await expect.poll(() => page.evaluate(async () => {
      const database = await new Promise<IDBDatabase>((resolve, reject) => {
        const request = indexedDB.open("nebula-editor-state");
        request.onsuccess = () => resolve(request.result);
        request.onerror = () => reject(request.error);
      });
      try {
        const envelope = await new Promise<{ sessions: Record<string, { buffers: Array<{ filePath: string; content: string }> }> }>((resolve, reject) => {
          const request = database.transaction("hot-exit").objectStore("hot-exit").get("sessions/v1");
          request.onsuccess = () => resolve(request.result);
          request.onerror = () => reject(request.error);
        });
        return Object.values(envelope.sessions).some((session) => session.buffers.length === 21 && session.buffers.some((buffer) => buffer.filePath === "recovery-21.txt" && buffer.content === "unsaved draft 21 λ"));
      } finally {
        database.close();
      }
    })).toBe(true);
    page.once("dialog", (dialog) => void dialog.accept());
    await page.goto(`${core.origin}/?view=code#token=${encodeURIComponent(core.token)}`);
    await expect(page.getByRole("textbox", { name: "File path" })).toHaveValue("recovery-21.txt");
    await expect(page.getByRole("textbox", { name: "Code editor" })).toContainText("unsaved draft 21 λ");
    await expect(page.getByText(/^21 open · 19 unsaved · recovery on$/)).toHaveText("21 open · 19 unsaved · recovery on");
    await page.getByRole("tab", { name: /hot-exit-notes\.txt/ }).click();
    await expect(page.getByRole("textbox", { name: "Code editor" })).toContainText("exact unsaved λ research draft");
    await page.waitForTimeout(350);
    page.once("dialog", (dialog) => void dialog.accept());
    await page.goto(`${core.origin}/?view=code#token=${encodeURIComponent(core.token)}`);
    await expect(page.getByRole("textbox", { name: "File path" })).toHaveValue("hot-exit-notes.txt");
    await expect(page.getByRole("textbox", { name: "Code editor" })).toContainText("exact unsaved λ research draft");
    await expect(page.getByText(/^21 open · 19 unsaved · recovery on$/)).toHaveText("21 open · 19 unsaved · recovery on");
  } finally {
    await api.dispose();
    await stopRealCore(core);
  }
});

test("production Code reads real Git changes and hands mutations to Nebula Terminal", async ({ page }) => {
  test.setTimeout(60_000);
  const core = await startRealCore();
  const projectFolder = await mkdtemp(path.join(tmpdir(), "nebula-source-control-project-"));
  const api = await playwrightRequest.newContext({
    baseURL: `${core.origin}/api/v1/`,
    extraHTTPHeaders: { Authorization: `Bearer ${core.token}` },
  });
  const git = (...gitArguments: string[]) => {
    const result = spawnSync("git", ["-C", projectFolder, ...gitArguments], { encoding: "utf8" });
    expect(result.status, result.stderr).toBe(0);
  };
  try {
    git("init", "-b", "research");
    git("config", "user.name", "Nebula Acceptance");
    git("config", "user.email", "nebula@example.invalid");
    await writeFile(path.join(projectFolder, "scanner.py"), "def scan():\n    return 'baseline'\n", "utf8");
    await writeFile(path.join(projectFolder, "Makefile"), "lint:\n\tpython -m compileall scanner.py\n", "utf8");
    git("add", "scanner.py", "Makefile");
    git("commit", "-m", "baseline");
    await writeFile(path.join(projectFolder, "scanner.py"), "def scan():\n    return 'changed'\n", "utf8");

    const create = await api.post("engagements", { data: {
      name: "Source Control Acceptance",
      description: "",
      client_name: null,
      status: "draft",
      tags: [],
      workspace_path: projectFolder,
      metadata: {},
    } });
    expect(create.ok(), await create.text()).toBe(true);
    const project = await create.json() as { id: string };
    await page.addInitScript((projectId) => localStorage.setItem("nebula.engagement", projectId), project.id);
    await page.goto(`${core.origin}/?view=code#token=${encodeURIComponent(core.token)}`);
    await expect(page.getByRole("tab", { name: "Workspace code editor", exact: true })).toBeVisible({ timeout: 20_000 });

    await page.getByRole("tab", { name: "Changes" }).click();
    await expect(page.getByText("research", { exact: true })).toBeVisible();
    await expect(page.getByText("1 changed path.", { exact: true })).toBeVisible();
    await page.getByRole("button", { name: "Working diff" }).click();
    const diff = page.getByRole("dialog", { name: "scanner.py" });
    await expect(diff.getByLabel("Diff for scanner.py")).toContainText("+    return 'changed'");
    await diff.getByRole("button", { name: "Close source-control diff" }).click();

    await page.getByRole("button", { name: /scanner\.py/ }).click();
    await expect(page.locator(".cm-line").nth(1)).toHaveText("    return 'changed'");
    await expect(page.getByText(/open-buffer intelligence ready/)).toBeVisible({ timeout: 10_000 });
    await page.getByRole("button", { name: "Problems" }).click();
    await expect(page.locator(".cm-panel-lint")).toBeVisible();
    await page.getByRole("button", { name: "Tasks" }).click();
    const tasks = page.getByRole("dialog", { name: "Project tasks and tests" });
    await tasks.getByRole("option", { name: /make: lint/ }).click();
    const executionReview = page.getByRole("dialog", { name: "Review exact code execution" });
    await expect(executionReview.locator(".execution-source-review")).toContainText("make lint");
    await executionReview.getByRole("button", { name: "Close execution review" }).click();
    await page.getByRole("tab", { name: "Changes" }).click();
    await expect(page.getByText(/Stage, commit, branch, pull, and push remain in Nebula Terminal/)).toBeVisible();
    await expect(page.getByRole("button", { name: "Open Terminal" })).toBeVisible();
  } finally {
    await api.dispose();
    await stopRealCore(core);
    if (path.basename(projectFolder).startsWith("nebula-source-control-project-")) {
      await rm(projectFolder, { recursive: true, force: true });
    }
  }
});

test("production Code quick-open works from a non-loopback LAN origin", async ({ page }) => {
  test.setTimeout(60_000);
  const lanAddress = Object.values(networkInterfaces())
    .flat()
    .find((address) => address?.family === "IPv4" && !address.internal)?.address;
  test.skip(!lanAddress, "No non-loopback IPv4 interface is available for the LAN-origin gate.");
  const core = await startRealCore({ bindHost: "0.0.0.0", browserHost: lanAddress });
  const api = await playwrightRequest.newContext({
    baseURL: `${core.origin}/api/v1/`,
    extraHTTPHeaders: { Authorization: `Bearer ${core.token}` },
  });
  try {
    const engagementsResponse = await api.get("engagements");
    expect(engagementsResponse.ok()).toBe(true);
    const projectId = (await engagementsResponse.json() as Array<{ id: string }>)[0]?.id;
    expect(projectId).toBeTruthy();
    const upload = await api.put(
      `engagements/${projectId}/workspace/file?path=lan-proof.py&overwrite=false`,
      { data: Buffer.from("print('lan production proof')\n"), headers: { "Content-Type": "text/plain" } },
    );
    expect(upload.ok(), await upload.text()).toBe(true);

    await page.goto(`${core.origin}/?view=code#token=${encodeURIComponent(core.token)}`);
    await expect(page.getByRole("tab", { name: "Workspace code editor", exact: true })).toBeVisible({ timeout: 20_000 });
    expect(new URL(page.url()).hostname).toBe(lanAddress);
    await page.getByRole("button", { name: /lan-proof\.py/ }).click();
    await expect(page.locator(".cm-line").first()).toHaveText("print('lan production proof')");
    await expect(page.getByText(/open-buffer intelligence ready/)).toBeVisible({ timeout: 10_000 });
    await page.getByRole("button", { name: "Open", exact: true }).click();
    const quickOpen = page.getByRole("dialog", { name: "Quick open" });
    await quickOpen.getByRole("textbox", { name: "Find a workspace file" }).fill("lan-proof");
    await quickOpen.getByRole("option", { name: /lan-proof\.py/ }).click();
    await expect(page.locator(".cm-line").first()).toHaveText("print('lan production proof')");
  } finally {
    await api.dispose();
    await stopRealCore(core);
  }
});

test("real Core persists a project folder chosen through the host browser", async ({ page }) => {
  test.setTimeout(60_000);
  const lanAddress = Object.values(networkInterfaces())
    .flat()
    .find((address) => address?.family === "IPv4" && !address.internal)?.address;
  const core = await startRealCore(lanAddress ? { bindHost: "0.0.0.0", browserHost: lanAddress } : {});
  const folderParent = await mkdtemp(path.join(homedir(), ".nebula-folder-picker-"));
  const selectedFolder = path.join(folderParent, "fresh-project");
  const paginatedFolder = path.join(folderParent, "project-502");
  await Promise.all(Array.from({ length: 503 }, (_, index) => mkdir(path.join(folderParent, `project-${index.toString().padStart(3, "0")}`))));
  const api = await playwrightRequest.newContext({
    baseURL: `${core.origin}/api/v1/`,
    extraHTTPHeaders: { Authorization: `Bearer ${core.token}` },
  });
  try {
    await page.goto(`${core.origin}/settings#token=${encodeURIComponent(core.token)}`);
    await expect(page.getByRole("heading", { name: "Settings", exact: true })).toBeVisible({ timeout: 20_000 });
    if (lanAddress) expect(new URL(page.url()).hostname).toBe(lanAddress);
    await page.getByRole("button", { name: "Switch project" }).click();
    const switcher = page.getByRole("dialog", { name: "Project switcher" });
    await switcher.getByRole("button", { name: "New project" }).click();
    await switcher.getByLabel("Name", { exact: true }).fill("Linked Folder Acceptance");
    await switcher.getByRole("button", { name: "Browse folders" }).click();

    const browser = page.getByRole("dialog", { name: "Choose project folder" });
    await expect(browser).toBeVisible();
    await browser.getByRole("button", { name: path.basename(folderParent), exact: true }).click();
    await expect(browser.getByText(folderParent, { exact: true })).toBeVisible();
    await browser.getByRole("button", { name: "New folder" }).click();
    await browser.getByRole("textbox", { name: "New folder name" }).fill("fresh-project");
    await browser.getByRole("button", { name: "Create folder" }).click();
    await expect(browser.getByText(selectedFolder, { exact: true })).toBeVisible();
    expect(existsSync(selectedFolder)).toBe(true);
    await browser.getByRole("button", { name: "Up one level" }).click();
    await expect(browser.getByRole("button", { name: "project-498", exact: true })).toBeVisible();
    await browser.getByRole("button", { name: "Load more folders" }).click();
    await browser.getByRole("button", { name: "project-502", exact: true }).click();
    await expect(browser.getByText(paginatedFolder, { exact: true })).toBeVisible();
    await browser.getByRole("button", { name: "Select folder" }).click();
    await expect(switcher.getByLabel("Project folder", { exact: true })).toHaveValue(paginatedFolder);
    await switcher.getByRole("button", { name: "Create" }).click();
    await expect(page.getByRole("button", { name: "Switch project" })).toContainText("Linked Folder Acceptance");

    const response = await api.get("engagements");
    expect(response.ok()).toBe(true);
    const projects = await response.json() as Array<{ name: string; workspace_path?: string }>;
    expect(projects.find((project) => project.name === "Linked Folder Acceptance")).toMatchObject({
      workspace_path: paginatedFolder,
    });

    await page.goto(`${core.origin}/settings#token=${encodeURIComponent(core.token)}`);
    await expect(page.getByRole("button", { name: "Switch project" })).toContainText("Linked Folder Acceptance", { timeout: 20_000 });
  } finally {
    await api.dispose();
    await stopRealCore(core);
    if (path.basename(folderParent).startsWith(".nebula-folder-picker-")) {
      await rm(folderParent, { recursive: true, force: true });
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

    await page.goto(`${core.origin}/?view=code#token=${encodeURIComponent(core.token)}`);
    await page.getByRole("button", { name: "New file", exact: true }).first().click();
    await page.getByRole("textbox", { name: "File path" }).fill("debug-proof.py");
    const debugEditor = page.getByRole("textbox", { name: "Code editor" });
    await debugEditor.fill("value = 41\nprint(f'debug-finished:{value + 1}')\n");
    await debugEditor.press("Control+S");
    await expect(
      page.getByText("Saved /workspace/debug-proof.py. Use it from Terminal when you're ready."),
    ).toBeVisible({ timeout: 30_000 });
    await debugEditor.press("Control+Home");
    await page.getByRole("button", { name: "Debug saved Python" }).click();
    const debuggerPanel = page.getByRole("dialog", { name: "Python debugger" });
    const breakpointButton = debuggerPanel.getByRole("button", { name: "Toggle breakpoint at line 1" });
    await breakpointButton.click();
    await expect(breakpointButton).toHaveAttribute("aria-pressed", "true");
    await debuggerPanel.getByRole("button", { name: "Start isolated debugger" }).click();
    await expect(debuggerPanel.getByText(/^stopped/)).toBeVisible({ timeout: 120_000 });
    await expect(debuggerPanel.getByText(/debug-proof\.py:1/)).toBeVisible();
    await debuggerPanel.getByRole("button", { name: "Continue" }).click();
    await expect(debuggerPanel.getByText(/^ended/)).toBeVisible({ timeout: 30_000 });
    await expect(debuggerPanel.getByText(/debug-finished:42/)).toBeVisible();
    await debuggerPanel.getByRole("button", { name: "Close debugger" }).click();
    await page.goto(`${core.origin}/#token=${encodeURIComponent(core.token)}`);

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
    await expect(page.getByText(/Removed 2 workspace entries/)).toBeVisible({ timeout: 30_000 });

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

test("assistant upgrade project creation switches canonical project and isolates chats", async ({ page }) => {
  test.setTimeout(90_000);
  const core = await startRealCore({ bindHost: "0.0.0.0", browserHost: localNetworkIpv4() });
  const stub = await startLocalModelStub({ streamDelayMs: 50 });
  const api = await playwrightRequest.newContext({ baseURL: `${core.origin}/api/v1/`, extraHTTPHeaders: { Authorization: `Bearer ${core.token}` } });
  try {
    const projects = await (await api.get("engagements")).json() as Array<{ id: string }>;
    const provider = await (await api.post("providers", { data: { name: "Project navigation acceptance", provider_type: "vllm", endpoint: `${stub.origin}/v1`, enabled: true, is_local: true, model_allowlist: ["security-model"], privacy: { local_only: true, residency: [], permits_sensitive_data: false }, metadata: { default_model: "security-model" } } })).json() as { id: string };
    const response = await api.post("chat/completions", { data: { backend: "provider", provider_id: provider.id, model: "security-model", engagement_id: projects[0].id, messages: [{ role: "user", content: "Old project conversation" }], include_knowledge: false, stream: false } });
    expect(response.ok(), await response.text()).toBe(true);
    const oldChat = await response.json() as { session_id: string };
    const pairingApi = await playwrightRequest.newContext({ baseURL: `http://127.0.0.1:${new URL(core.origin).port}/api/v1/`, extraHTTPHeaders: { Authorization: `Bearer ${core.token}` } });
    const pairingResponse = await pairingApi.post("auth/pairings", { data: { name: "Project creation browser" } });
    expect(pairingResponse.ok(), await pairingResponse.text()).toBe(true);
    const pairing = await pairingResponse.json() as { secret: string; confirmation_code: string };
    await pairingApi.dispose();
    await page.goto(`${core.origin}/#pair=${encodeURIComponent(pairing.secret)}&code=${encodeURIComponent(pairing.confirmation_code)}`);
    await page.getByLabel("Device name").fill("Project creation browser");
    await page.getByRole("button", { name: "Pair device" }).click();
    await expect(page.getByRole("button", { name: "Nebula Core ready" })).toBeVisible({ timeout: 20_000 });
    await page.goto(`${core.origin}/projects/${projects[0].id}/workbench?view=chat&session=${oldChat.session_id}`);
    await expect(page.locator(".chat-message.operator")).toContainText("Old project conversation");
    const sidebar = page.getByRole("button", { name: "Show sidebar" });
    if (await sidebar.isVisible()) await sidebar.click();
    await page.getByRole("button", { name: "Switch project" }).click();
    const switcher = page.getByRole("dialog", { name: "Project switcher" });
    await switcher.getByRole("button", { name: "New project" }).click();
    await switcher.getByLabel("Name", { exact: true }).fill("New isolated project");
    await switcher.getByRole("button", { name: "Create", exact: true }).click();
    await expect(switcher).toBeHidden();
    const saved = await (await api.get("engagements")).json() as Array<{ id: string; name: string }>;
    const created = saved.find(project => project.name === "New isolated project")!;
    expect(created).toBeTruthy();
    await expect(page).toHaveURL(`${core.origin}/projects/${created.id}/workbench?view=chat`);
    await expect.poll(() => page.evaluate(() => localStorage.getItem("nebula.engagement"))).toBe(created.id);
    await expect(page.getByRole("button", { name: "Start new chat", exact: true })).toBeVisible();
    await expect(page.locator(".chat-message.operator")).toHaveCount(0);
    await page.reload();
    await expect(page.getByRole("button", { name: "Start new chat", exact: true })).toBeVisible();
    await expect.poll(() => page.evaluate(() => localStorage.getItem("nebula.engagement"))).toBe(created.id);
    await page.getByRole("button", { name: "Start new chat", exact: true }).click();
    await page.getByRole("textbox", { name: "Message the analyst assistant" }).fill("New project conversation");
    await page.getByRole("button", { name: "Send message", exact: true }).click();
    await expect(page.locator(".chat-message.operator")).toContainText("New project conversation");
    await expect.poll(async () => {
      const chats = await (await api.get(`chat-sessions?engagement_id=${created.id}`)).json() as Array<{ id: string }>;
      return chats.length;
    }).toBe(1);
    const oldChats = await (await api.get(`chat-sessions?engagement_id=${projects[0].id}`)).json() as Array<{ id: string }>;
    expect(oldChats.map(chat => chat.id)).toEqual([oldChat.session_id]);
  } finally {
    await api.dispose();
    await stopLocalModelStub(stub);
    await stopRealCore(core);
  }
});

test("assistant upgrade foundation production LAN reads durable conversation", async ({ page }, testInfo) => {
  // WebKit's production-LAN pass can spend more than a minute covering the
  // complete durable-conversation lifecycle on shared CI runners.
  test.setTimeout(120_000);
  const core = await startRealCore({bindHost: "0.0.0.0", browserHost: localNetworkIpv4()});
  const stub = await startLocalModelStub({streamDelayMs: 250});
  const api = await playwrightRequest.newContext({baseURL: `${core.origin}/api/v1/`, extraHTTPHeaders: {Authorization: `Bearer ${core.token}`}});
  try {
    const projects = await (await api.get("engagements")).json() as Array<{id: string}>;
    const provider = await (await api.post("providers", {data: {name: "Assistant acceptance", provider_type: "vllm", endpoint: `${stub.origin}/v1`, enabled: true, is_local: true, model_allowlist: ["security-model"], privacy: {local_only: true, residency: [], permits_sensitive_data: false}, metadata: {default_model: "security-model"}}})).json() as {id: string};
    const response = await api.post("chat/completions", {data: {backend: "provider", provider_id: provider.id, model: "security-model", engagement_id: projects[0].id, messages: [{role: "user", content: "Hello"}], include_knowledge: false, stream: false}});
    expect(response.ok(), await response.text()).toBe(true);
    const chat = await response.json() as {session_id: string};
    const url = `${core.origin}/?view=chat&session=${chat.session_id}#token=${encodeURIComponent(core.token)}`;
    await page.goto(url);
    await expect(page.locator(".chat-message.operator")).toContainText("Hello");
    await expect(page.locator(".chat-message.operator .activity-ledger")).toHaveCount(0);
    await expect(page.locator(".chat-thread")).toHaveCount(1);
    await expect(page.locator(".chat-message.operator")).toHaveCount(1);
    await expect(page.getByText("Connection unavailable", {exact: true})).toHaveCount(0);
    await page.getByRole("button", {name: "Assistant settings", exact: true}).click();
    await expect(page.getByRole("dialog", {name: "Assistant settings"})).toBeVisible();
    await page.getByRole("button", {name: "Close assistant settings"}).click();
    await page.goto(url);
    await expect(page.locator(".chat-message.operator")).toContainText("Hello");
    const operator = page.locator(".chat-message.operator");
    await operator.getByRole("button", {name: "Bookmark", exact: true}).click();
    await expect(operator.getByRole("button", {name: "Bookmark", exact: true})).toHaveAttribute("aria-pressed", "true");
    await page.goto(url);
    await expect(operator.getByRole("button", {name: "Bookmark", exact: true})).toHaveAttribute("aria-pressed", "true");
    await page.locator(".assistant-search > summary").click();
    await page.getByLabel("Search transcript", {exact: true}).fill("Hello");
    await page.getByRole("button", {name: "Search messages", exact: true}).click();
    await expect(page.locator(".assistant-search ol li")).toHaveCount(1);
    await page.getByRole("button", {name: "Attach files", exact: true}).click();
    await page.locator(".assistant-attachment-dialog input[type=file]").setInputFiles({name: "review.txt", mimeType: "text/plain", buffer: Buffer.from("Exact attachment preview\n")});
    await expect(page.locator(".assistant-attachment-dialog pre")).toContainText("Exact attachment preview");
    await page.getByRole("button", {name: "Attach this excerpt"}).click();
    await expect(page.getByRole("region", {name: "Selected context pack"})).toContainText("review.txt");
    const composer = page.getByRole("textbox", {name: "Message the analyst assistant"});
    await composer.fill("Preserve this unsent draft");
    await page.getByRole("button", {name: "Results", exact: true}).click();
    await expect(page.getByRole("region", {name: "Conversation results"})).toBeVisible();
    await page.getByRole("button", {name: "Context", exact: true}).click();
    await expect(page.getByText("Prepared for your next message", {exact: true})).toBeVisible();
    await page.getByRole("button", {name: "Close details"}).click();
    await expect(composer).toHaveValue("Preserve this unsent draft");
    await operator.getByRole("button", {name: "Save as decision", exact: true}).click();
    const decisions = page.getByRole("region", {name: "Saved decisions and constraints"});
    await decisions.getByRole("textbox", {name: "Operator context text", exact: true}).fill("Keep future responses concise");
    await decisions.getByRole("button", {name: "Save operator context", exact: true}).click();
    await expect(decisions).toContainText("Keep future responses concise");
    await decisions.getByRole("button", {name: "Edit decision", exact: true}).click();
    await decisions.getByRole("textbox", {name: "Operator context text", exact: true}).fill("Use concise plain language");
    await decisions.getByRole("button", {name: "Save operator context", exact: true}).click();
    await expect(decisions).toContainText("revision 2");
    await decisions.getByRole("button", {name: "Promote to project", exact: true}).click();
    await expect(decisions).toContainText("decision · project");
    await page.getByRole("button", {name: "Close details"}).click();
    await composer.fill("Queue first task");
    await page.getByRole("button", {name: "Queue for later", exact: true}).click();
    const queue = page.getByRole("region", {name: "Core follow-up queue"});
    await expect(queue).toContainText("Queue first task");
    await composer.fill("Queue second task");
    await page.getByRole("button", {name: "Queue for later", exact: true}).click();
    await expect(queue.locator("li")).toHaveCount(2);
    await queue.getByRole("button", {name: "Edit queued message 1", exact: true}).click();
    await queue.getByRole("textbox", {name: "Edit queued text"}).fill("Edited queued first task");
    await queue.getByRole("button", {name: "Save queued edit"}).click();
    await expect(queue).toContainText("Edited queued first task");
    await queue.getByRole("button", {name: "Move message 2 up"}).click();
    await expect(queue.locator("li").first()).toContainText("Queue second task");
    const geometry = await page.locator(".chat-panel").evaluate(panel => {
      const box = panel.getBoundingClientRect(); const composer = panel.querySelector(".chat-composer")!.getBoundingClientRect();
      const thread = panel.querySelector(".chat-thread")!.getBoundingClientRect();
      const queueTexts = [...panel.querySelectorAll(".chat-follow-up-queue li > div:first-child p")].map(node=>node.getBoundingClientRect().width);
      return {panelBottom: box.bottom, composerBottom: composer.bottom, threadHeight: thread.height, queueTexts};
    });
    expect(geometry.composerBottom).toBeLessThanOrEqual(geometry.panelBottom + 1);
    expect(geometry.threadHeight).toBeGreaterThan(50);
    expect(geometry.queueTexts.every(width => width >= 120)).toBe(true);
    await testInfo.attach("queued-work", {body: await page.screenshot(), contentType: "image/png"});
    await testInfo.attach("build-origin", {body: JSON.stringify({origin: core.origin, project: testInfo.project.name, viewport: page.viewportSize(), assets: await page.locator("script[src]").evaluateAll(nodes => nodes.map(node => node.getAttribute("src")))}), contentType: "application/json"});
    const secondDevice = await page.context().browser()!.newContext();
    const secondPage = await secondDevice.newPage();
    await secondPage.goto(url);
    await expect(secondPage.getByRole("region", {name: "Core follow-up queue"}).locator("li").first()).toContainText("Queue second task");
    await secondDevice.close();
    await queue.getByRole("button", {name: "Resume queue", exact: true}).click();
    await page.goto("about:blank");
    await expect.poll(async () => {
      const record = await (await api.get(`chat/sessions/${chat.session_id}/queue`)).json() as {items: {status: string; detail?: string}[]};
      if (record.items.some(item => item.status === "needs_review")) { throw new Error(JSON.stringify(record.items)); }
      return record.items.map(item => item.status);
    }, {timeout: 15_000}).toEqual(["complete", "complete"]);
    await page.goto(url);
    await expect(page.locator(".chat-message.operator")).toHaveCount(3);
    await expect(page.locator(".chat-message.operator").nth(1)).toContainText("Queue second task");
    await expect(page.getByRole("region", {name: "Catch up on this conversation"})).toBeVisible();
    await page.getByRole("button", {name: "Dismiss catch-up", exact: true}).click();
    await expect(page.getByRole("region", {name: "Catch up on this conversation"})).toHaveCount(0);
    await page.goto(url);
    await expect(page.locator(".chat-message.operator")).toHaveCount(3);
    await expect(page.getByRole("region", {name: "Catch up on this conversation"})).toHaveCount(0);
    await page.locator(".chat-evidence").first().locator("summary").first().click();
    await expect(page.locator(".chat-evidence").first()).toContainText("No supporting citations");
    const queuedModelRequests = stub.requests.filter(request => (request.stream === true || Array.isArray(request.tools) && request.tools.length > 0) && JSON.stringify(request.messages).includes("Queue second task"));
    expect(queuedModelRequests.length).toBeGreaterThan(0);
    expect(queuedModelRequests.every(request => JSON.stringify(request.messages).includes("Use concise plain language"))).toBe(true);
    const recorded = await (await api.get(`chat/sessions/${chat.session_id}/context-sources`)).json() as {items: {operator_decisions: {text: string; scope: string}[]}[]};
    expect(recorded.items.some(item => item.operator_decisions.some(entry => entry.text === "Use concise plain language" && entry.scope === "project"))).toBe(true);
    await operator.first().getByRole("button", {name: "Edit and branch"}).click();
    await expect(page.getByRole("textbox", {name: "Message the analyst assistant"})).toHaveValue("Hello");
    await expect(page.locator(".chat-message")).toHaveCount(0);
    await expect(page.getByRole("button", {name: "Open parent"})).toBeVisible();
    await testInfo.attach("production-lan-chat", {body: await page.screenshot(), contentType: "image/png"});
  } finally { await api.dispose(); await stopRealCore(core); await stopLocalModelStub(stub); }
});

for (const decision of ["Approve", "Reject"] as const) {
  test(`assistant upgrade stabilization real Core ${decision.toLowerCase()} continues exactly once`, async ({page}, testInfo) => {
    test.setTimeout(90_000);
    const repository = path.resolve(import.meta.dirname, "../..");
    const commonGitDir = spawnSync("git", ["-C", repository, "rev-parse", "--path-format=absolute", "--git-common-dir"], {encoding: "utf8"}).stdout.trim();
    const python = process.env.NEBULA_TEST_PYTHON ?? path.join(path.dirname(commonGitDir), ".venv/bin/python");
    const dataDir = await mkdtemp(path.join(tmpdir(), "nebula-stabilization-core-"));
    const reservation = createServer();
    await new Promise<void>(resolve => reservation.listen(0, "0.0.0.0", resolve));
    const port = (reservation.address() as AddressInfo).port;
    await new Promise<void>((resolve, reject) => reservation.close(error => error ? reject(error) : resolve()));
    const origin = `http://${localNetworkIpv4()}:${port}`;
    const processHandle = spawn(python, [path.join(repository, "ui/tests/fixtures/approval_core.py"), "--root", dataDir, "--static-dir", path.join(repository, "ui/dist"), "--port", String(port)], {cwd: repository, env: {...process.env, PYTHONPATH: path.join(repository, "src")}});
    let logs = "";
    processHandle.stdout.on("data", chunk => {logs += chunk.toString();});
    processHandle.stderr.on("data", chunk => {logs += chunk.toString();});
    const api = await playwrightRequest.newContext({baseURL: `${origin}/api/v1/`, extraHTTPHeaders: {Authorization: "Bearer stabilization-fixture"}});
    try {
      await expect.poll(async () => {
        if (processHandle.exitCode !== null) throw new Error(logs);
        try {return (await api.get("health")).ok();} catch {return false;}
      }, {timeout: 30_000}).toBe(true);
      expect((await api.post("harnesses/inert-fixture/health")).ok()).toBe(true);
      const pairingResponse = await api.post(`http://127.0.0.1:${port}/api/v1/auth/pairings`, {data: {name: "Stabilization browser"}});
      expect(pairingResponse.ok(), await pairingResponse.text()).toBe(true);
      const pairing = await pairingResponse.json();
      await page.goto(`${origin}/?view=chat#pair=${encodeURIComponent(pairing.secret)}&code=${encodeURIComponent(pairing.confirmation_code)}`);
      await page.getByLabel("Device name").fill("Stabilization browser");
      await page.getByRole("button", {name: "Pair device", exact: true}).click();
      await expect(page.getByRole("button", {name: /Nebula Core (ready|degraded)/})).toBeVisible({timeout: 20_000});
      await page.goto(`${origin}/?view=chat`);
      await page.getByRole("button", {name: "New chat", exact: true}).click();
      const composer = page.getByRole("textbox", {name: "Message the analyst assistant", exact: true});
      await expect(composer).toBeEnabled();
      await composer.fill("Review the inert fixture request. No commands execute.");
      await page.getByRole("button", {name: "Send message", exact: true}).click();
      await page.getByRole("button", {name: "Review pending actions", exact: true}).click({timeout: 30_000});
      const card = page.getByRole("region", {name: "Approval required", exact: true});
      await expect(card).toBeVisible();
      await page.reload();
      await expect(card).toBeVisible();
      await card.getByRole("button", {name: decision, exact: true}).click();
      const answer = decision === "Approve" ? "APPROVAL_ACCEPTED_ONCE" : "APPROVAL_DECLINED";
      await expect(page.locator(".chat-message.assistant .assistant-markdown").last()).toContainText(answer);
      await expect(page.getByRole("button", {name: "Review pending actions", exact: true})).toHaveCount(0);
      await expect(page.getByText("Action required", {exact: true})).toHaveCount(0);
      const id = new URL(page.url()).searchParams.get("session"); expect(id).toBeTruthy();
      const state = await (await api.get(`chat/sessions/${id}/state`)).json();
      expect(state.execution).toBe("complete"); expect(state.pending).toEqual([]);
      expect(state.decisions[0].continuation.status).toBe("delivered");
      expect(state.decisions[0].continuation.adapter_status).toBe("not_required");
      expect(state.decisions[0].progress).toBe("observed");
      expect(state.decisions[0].progress_sequence).toBeGreaterThan(state.decisions[0].continuation.progress_after_sequence);
      const approval = state.decisions[0];
      const repeated = await api.post(`approvals/${approval.approval_id}/decision`, {data: {decision: decision.toLowerCase()}});
      expect(repeated.ok()).toBe(true);
      await page.reload();
      await expect(page.locator(".chat-message.assistant .assistant-markdown")).toHaveCount(1);
      await expect(page.locator(".chat-message.assistant .assistant-markdown")).toContainText(answer);
      await testInfo.attach("real-core-approval", {body: JSON.stringify({origin, state, runtime: "inert adapter; real Core/persistence/UI"}), contentType: "application/json"});
      await testInfo.attach("real-core-approval-screen", {body: await page.screenshot(), contentType: "image/png"});
    } finally {
      await api.dispose();
      await stopRealCore({process: processHandle, dataDir, origin, token: "stabilization-fixture"});
    }
  });
}

for (const runtime of [
  {name: "codex", kind: "codex_app_server"},
  {name: "grok", kind: "grok_acp"},
]) {
  test(`assistant upgrade native ${runtime.name} production LAN conversation`, async ({page}, testInfo) => {
    test.skip(process.env.NEBULA_ASSISTANT_NATIVE_ACCEPTANCE !== "1", "Requires an explicitly enabled local CLI login; fixture coverage runs separately.");
    test.setTimeout(180_000);
    const database = process.env.NEBULA_ASSISTANT_PROFILE_DB;
    if (!database) throw new Error("Set NEBULA_ASSISTANT_PROFILE_DB to discover an existing runtime; no hardcoded model fallback is used.");
    const repository = path.resolve(import.meta.dirname, "../..");
    const commonGitDir = spawnSync("git", ["-C", repository, "rev-parse", "--path-format=absolute", "--git-common-dir"], {encoding: "utf8"}).stdout.trim();
    const configured = spawnSync(process.env.NEBULA_TEST_PYTHON ?? path.join(path.dirname(commonGitDir), ".venv/bin/python"),
      [path.join(import.meta.dirname, "fixtures/configured_harness.py"), database, runtime.kind], {encoding: "utf8"});
    if (configured.status !== 0) throw new Error(`Runtime discovery failed: ${configured.stderr}`);
    const configuration = JSON.parse(configured.stdout);
    const core = await startRealCore({bindHost: "0.0.0.0", browserHost: localNetworkIpv4()});
    const api = await playwrightRequest.newContext({baseURL: `${core.origin}/api/v1/`, extraHTTPHeaders: {Authorization: `Bearer ${core.token}`}});
    try {
      const response = await api.post("harnesses", {data: {...configuration, name: `Assistant acceptance ${runtime.name}`, enabled: true, privacy: {local_only: false, permits_sensitive_data: true}}});
      expect(response.ok(), await response.text()).toBe(true);
      const profile = await response.json() as {id: string};
      const health = await api.post(`harnesses/${profile.id}/health`);
      expect(health.ok(), await health.text()).toBe(true);
      const discovered = await (await api.get(`harnesses/${profile.id}`)).json();
      const model = discovered.default_model || discovered.capabilities?.models?.[0];
      expect(typeof model === "string" && model.length > 0, "Health discovery must advertise a model").toBe(true);
      const url = `${core.origin}/?view=chat#token=${encodeURIComponent(core.token)}`;
      await page.goto(url);
      await page.getByRole("button", {name: "New chat", exact: true}).click();
      const composer = page.getByRole("textbox", {name: "Message the analyst assistant", exact: true});
      await expect(composer).toBeEnabled({timeout: 30_000});
      await composer.fill("Reply with exactly NEBULA_CHAT_ACCEPTED. Do not use tools, access files, or change anything.");
      await page.getByRole("button", {name: "Send message", exact: true}).click();
      await expect(page.locator(".chat-message.assistant .assistant-markdown").last()).toContainText("NEBULA_CHAT_ACCEPTED", {timeout: 120_000});
      await expect(page.getByRole("button", {name: "Stop response", exact: true})).toHaveCount(0, {timeout: 30_000});
      await expect.poll(() => new URL(page.url()).searchParams.get("session")).toBeTruthy();
      const session = new URL(page.url()).searchParams.get("session");
      await page.goto(`${core.origin}/?view=chat&session=${session}#token=${encodeURIComponent(core.token)}`);
      await expect(page.locator(".chat-message.assistant .assistant-markdown").last()).toContainText("NEBULA_CHAT_ACCEPTED");
      await expect(page.getByText("Connection unavailable", {exact: true})).toHaveCount(0);
      await page.locator(".chat-evidence").last().locator("summary").first().click();
      await expect(page.locator(".chat-evidence").last()).toContainText("interpretation");
      await testInfo.attach("native-runtime-build", {body: JSON.stringify({runtime: runtime.name, model, origin: core.origin, session, health: await health.json(), assets: await page.locator("script[src]").evaluateAll(nodes=>nodes.map(node=>node.getAttribute("src")))}), contentType: "application/json"});
      await testInfo.attach("native-runtime-chat", {body: await page.screenshot(), contentType: "image/png"});
    } finally {await api.dispose(); await stopRealCore(core);}
  });
}

test("assistant upgrade deployed local service retains operator workflow", async ({page}, testInfo) => {
  const origin = process.env.NEBULA_ASSISTANT_LIVE_ORIGIN;
  const tokenFile = process.env.NEBULA_ASSISTANT_LIVE_TOKEN_FILE;
  test.skip(!origin || !tokenFile, "Explicit deployed-service origin and a private token file are required.");
  test.setTimeout(180_000);
  const token = (await readFile(tokenFile!, "utf8")).trim();
  const api = await playwrightRequest.newContext({baseURL: `${origin}/api/v1/`, extraHTTPHeaders: {Authorization: `Bearer ${token}`}});
  try {
    const profiles = await (await api.get("harnesses")).json() as {id: string; kind: string}[];
    const profile = profiles.find(item => item.kind === "codex_app_server"); expect(profile).toBeTruthy();
    await page.goto(`${origin}/?view=chat#token=${encodeURIComponent(token)}`);
    await page.getByRole("button", {name: "New chat", exact: true}).click();
    await page.getByRole("button", {name: "Assistant settings", exact: true}).click();
    await page.getByLabel("Chat runtime", {exact: true}).selectOption("harness");
    await page.getByLabel("Chat harness", {exact: true}).selectOption(profile!.id);
    await page.getByRole("button", {name: "Close assistant settings"}).click();
    const composer = page.getByRole("textbox", {name: "Message the analyst assistant", exact: true});
    await composer.fill("Reply exactly NEBULA_LOCAL_VALIDATED. Do not use tools, access files, or change anything.");
    await page.getByRole("button", {name: "Send message", exact: true}).click();
    await expect(page.locator(".chat-message.assistant .assistant-markdown").last()).toContainText("NEBULA_LOCAL_VALIDATED", {timeout: 120_000});
    await expect(page.getByRole("button", {name: "Stop response", exact: true})).toHaveCount(0);
    const session = new URL(page.url()).searchParams.get("session"); expect(session).toBeTruthy();
    const savedUrl = `${origin}/?view=chat&session=${session}#token=${encodeURIComponent(token)}`;
    await page.goto(savedUrl);
    const operator = page.locator(".chat-message.operator").first();
    await operator.getByRole("button", {name: "Bookmark", exact: true}).click();
    await operator.getByRole("button", {name: "Save as decision", exact: true}).click();
    const decisions = page.getByRole("region", {name: "Saved decisions and constraints"});
    await decisions.getByRole("textbox", {name: "Operator context text"}).fill("This validation conversation uses text-only replies and no tools.");
    await decisions.getByRole("button", {name: "Save operator context"}).click();
    await expect(decisions).toContainText("This validation conversation uses text-only replies and no tools.");
    await page.getByRole("button", {name: "Close details"}).click();
    await composer.fill("Reply exactly NEBULA_QUEUE_VALIDATED. Do not use tools or access files.");
    await page.getByRole("button", {name: "Queue for later", exact: true}).click();
    const queue = page.getByRole("region", {name: "Core follow-up queue"});
    await expect(queue).toContainText("NEBULA_QUEUE_VALIDATED");
    await queue.getByRole("button", {name: "Resume queue", exact: true}).click();
    await page.goto("about:blank");
    await expect.poll(async () => {
      const record = await (await api.get(`chat/sessions/${session}/queue`)).json() as {items: {status: string; detail?: string}[]};
      if (record.items.some(item => item.status === "needs_review")) throw new Error(JSON.stringify(record.items.map(item=>({status:item.status,detail:item.detail}))));
      return record.items.every(item=>item.status === "complete");
    }, {timeout: 90_000}).toBe(true);
    await page.goto(savedUrl);
    await expect(page.locator(".chat-message.assistant .assistant-markdown").last()).toContainText("NEBULA_QUEUE_VALIDATED");
    await expect(operator.getByRole("button", {name: "Bookmark", exact: true})).toHaveAttribute("aria-pressed", "true");
    await page.locator(".chat-evidence").last().locator("summary").first().click();
    await expect(page.locator(".chat-evidence").last()).toContainText("interpretation");
    await page.getByRole("button", {name: "Results", exact: true}).click();
    await expect(page.getByRole("region", {name: "Conversation results"})).toBeVisible();
    await page.getByRole("button", {name: "Context", exact: true}).click();
    await expect(decisions).toContainText("This validation conversation uses text-only replies and no tools.");
    await page.getByRole("button", {name: "Close details"}).click();
    await testInfo.attach("deployed-build", {body: JSON.stringify({origin, session, assets: await page.locator("script[src]").evaluateAll(nodes=>nodes.map(node=>node.getAttribute("src")))}), contentType: "application/json"});
    await testInfo.attach("deployed-chat", {body: await page.screenshot(), contentType: "image/png"});
  } finally {await api.dispose();}
});

test("project removal archives, retries, restores and clears the last selection on production LAN", async ({ page }, testInfo) => {
  // Hosted WebKit spends 2–4s on many successful clicks in this full lifecycle;
  // traces reached the final restored state at 90s before the last assertions.
  test.setTimeout(120_000);
  const startedAt = Date.now();
  const core = await startRealCore({ bindHost: "0.0.0.0", browserHost: localNetworkIpv4() });
  const stub = await startLocalModelStub();
  const api = await playwrightRequest.newContext({ baseURL: `${core.origin}/api/v1/`, extraHTTPHeaders: { Authorization: `Bearer ${core.token}` } });
  try {
    const projects = await (await api.get("engagements")).json() as Array<{ id: string; name: string; scope_policy_id: string }>;
    const project = projects[0];
    const folder = path.join(core.dataDir, "retained-project");
    await mkdir(folder);
    await writeFile(path.join(folder, "keep.txt"), "project files stay intact");
    const linkedResponse = await api.post("engagements", { data: { name: "Project with a long name that must wrap safely on a small phone", workspace_path: folder } });
    expect(linkedResponse.ok(), await linkedResponse.text()).toBe(true);
    const linked = await linkedResponse.json() as { id: string; name: string };
    const provider = await (await api.post("providers", { data: { name: "Retention fixture", provider_type: "vllm", endpoint: `${stub.origin}/v1`, enabled: true, is_local: true, model_allowlist: ["security-model"], privacy: { local_only: true, residency: [], permits_sensitive_data: false } } })).json() as { id: string };
    const chatResponse = await api.post("chat/completions", { data: { backend: "provider", provider_id: provider.id, model: "security-model", engagement_id: linked.id, messages: [{ role: "user", content: "Keep this project history" }], include_knowledge: false, stream: false } });
    expect(chatResponse.ok(), await chatResponse.text()).toBe(true);
    const chat = await chatResponse.json() as { session_id: string };
    const historyResponse = await api.get(`chat/sessions/${chat.session_id}/messages`);
    expect(historyResponse.ok()).toBe(true);
    const historyBefore = await historyResponse.json();
    const pairingApi = await playwrightRequest.newContext({ baseURL: `http://127.0.0.1:${new URL(core.origin).port}/api/v1/`, extraHTTPHeaders: { Authorization: `Bearer ${core.token}` } });
    const pairing = await (await pairingApi.post("auth/pairings", { data: { name: "Project removal browser" } })).json() as { secret: string; confirmation_code: string };
    await pairingApi.dispose();
    await page.goto(`${core.origin}/#pair=${encodeURIComponent(pairing.secret)}&code=${encodeURIComponent(pairing.confirmation_code)}`);
    await page.getByLabel("Device name").fill("Project removal browser");
    await page.getByRole("button", { name: "Pair device" }).click();
    await expect(page.getByRole("button", { name: "Nebula Core ready" })).toBeVisible({ timeout: 20_000 });
    await page.goto(`${core.origin}/projects/${linked.id}/workbench`);
    const switcher = page.getByRole("dialog", { name: "Project switcher" });
    const openSwitcher = async () => test.step("Open project switcher", async () => {
      const sidebar = page.getByRole("button", { name: "Show sidebar" });
      const switchProject = page.getByRole("button", { name: "Switch project" });
      // Reload returns before the responsive shell has necessarily mounted.
      // Wait for its visible entry point before deciding whether to open it.
      await expect(sidebar.or(switchProject).filter({ visible: true }).first()).toBeVisible();
      if (await sidebar.isVisible()) await sidebar.click();
      console.info("project-removal switcher readiness", {
        elapsedMs: Date.now() - startedAt,
        sidebarCollapsed: await page.locator(".app-shell").evaluate(element => element.classList.contains("sidebar-collapsed")),
        switcherBounds: await switchProject.boundingBox(),
        coreReady: await page.getByRole("button", { name: "Nebula Core ready" }).isVisible(),
      });
      if (!await switcher.isVisible()) await switchProject.click({ timeout: 10_000 });
    });
    const remove = async (name: string) => {
      await switcher.getByRole("button", { name: `Remove project ${name}`, exact: true }).click();
      await page.getByRole("button", { name: "Remove project", exact: true }).click();
    };
    await openSwitcher();
    const removeButton = switcher.getByRole("button", { name: `Remove project ${linked.name}`, exact: true });
    const geometry = await removeButton.boundingBox();
    expect(geometry!.width).toBeGreaterThanOrEqual(44);
    expect(geometry!.height).toBeGreaterThanOrEqual(44);
    expect(await switcher.evaluate(el => el.scrollWidth <= el.clientWidth)).toBe(true);
    await expect(removeButton).toBeInViewport({ ratio: 1 });
    await page.screenshot({ path: testInfo.outputPath("project-switcher.png"), animations: "disabled" });
    const menuBounds = await switcher.boundingBox();
    expect(menuBounds!.x).toBeGreaterThanOrEqual(0);
    expect(menuBounds!.x + menuBounds!.width).toBeLessThanOrEqual(page.viewportSize()!.width);
    const accessibility = await new AxeBuilder({ page }).include(".engagement-menu").withTags(["wcag2a", "wcag2aa"]).analyze();
    expect(accessibility.violations).toEqual([]);
    await removeButton.focus();
    await page.keyboard.press("Enter");
    await expect(page.getByText("Files and chat history are kept.", { exact: false })).toBeVisible();
    await page.getByRole("button", { name: "Cancel", exact: true }).click();
    await expect(removeButton).toBeFocused();
    await removeButton.click();
    await page.getByRole("button", { name: "Cancel", exact: true }).click();
    await expect(removeButton).toBeFocused();
    expect((await (await api.get(`engagements/${linked.id}`)).json()).status).not.toBe("archived");
    // A transient mutation failure must keep the project visible and permit a real retry.
    await page.route(`**/api/v1/engagements/${linked.id}`, async route => {
      if (route.request().method() === "PATCH") await route.fulfill({ status: 503, json: { detail: "Temporary save failure" } });
      else await route.continue();
    });
    await remove(linked.name);
    await expect(switcher.getByRole("alert")).toContainText("Try again");
    await page.unroute(`**/api/v1/engagements/${linked.id}`);
    await remove(linked.name);
    await expect(removeButton).toHaveCount(0);
    await expect(page).toHaveURL(`${core.origin}/projects/${project.id}/workbench`);
    expect((await (await api.get(`engagements/${linked.id}`)).json()).status).toBe("archived");
    expect(await readFile(path.join(folder, "keep.txt"), "utf8")).toBe("project files stay intact");
    expect(await (await api.get(`chat/sessions/${chat.session_id}/messages`)).json()).toEqual(historyBefore);
    await page.reload();
    await openSwitcher();
    await expect(removeButton).toHaveCount(0);
    await switcher.getByRole("button", { name: "Archived projects (1)" }).click();
    await switcher.getByRole("button", { name: `Restore project ${linked.name}` }).click();
    await expect(removeButton).toBeVisible();
    await switcher.locator(".project-switcher-row").filter({ hasText: linked.name }).getByRole("button").first().click();
    await expect(page).toHaveURL(new RegExp(`/projects/${linked.id}/`));
    await page.goto(`${core.origin}/projects/${linked.id}/workbench?view=chat&session=${chat.session_id}`);
    await expect(page.locator(".chat-message.operator")).toContainText("Keep this project history");
    await openSwitcher();
    await remove(linked.name);
    await expect(removeButton).toHaveCount(0);
    await remove(project.name);
    await expect(page).toHaveURL(`${core.origin}/`);
    await expect.poll(() => page.evaluate(() => localStorage.getItem("nebula.engagement"))).toBeNull();
    await page.reload();
    await openSwitcher();
    await expect(switcher.getByText("No active projects.", { exact: false })).toBeVisible();
    await expect(switcher.getByRole("button", { name: "New project" })).toBeEnabled();
    await switcher.getByRole("button", { name: "Archived projects (2)" }).click();
    await switcher.getByRole("button", { name: `Restore project ${project.name}` }).click();
    await expect(switcher.getByRole("button", { name: `Remove project ${project.name}` })).toBeVisible();
    const scopes = await (await api.get(`engagements/${project.id}/scope`)).json();
    expect(scopes).toBeTruthy();
    await testInfo.attach("project-removal-production-evidence", { body: JSON.stringify({ origin: core.origin, build: "production", project: testInfo.project.name, viewport: page.viewportSize(), retainedFiles: true }), contentType: "application/json" });
  } finally {
    await api.dispose();
    await stopLocalModelStub(stub);
    await stopRealCore(core);
  }
});

test("stabilization real Core preserves note drafts and reuses saved notes in reports", async ({page}, testInfo) => {
  test.setTimeout(90_000);
  const core = await startRealCore({bindHost: "0.0.0.0", browserHost: localNetworkIpv4()});
  const api = await playwrightRequest.newContext({baseURL: `${core.origin}/api/v1/`, extraHTTPHeaders: {Authorization: `Bearer ${core.token}`}});
  try {
    const projects = await (await api.get("engagements")).json() as {id: string}[];
    const project = projects[0];
    const pairing = await api.post(core.origin.replace(new URL(core.origin).hostname, "127.0.0.1") + "/api/v1/auth/pairings", {data: {name: "Output acceptance"}});
    expect(pairing.ok()).toBe(true);
    const pair = await pairing.json();
    await page.goto(`${core.origin}/#pair=${encodeURIComponent(pair.secret)}&code=${encodeURIComponent(pair.confirmation_code)}`);
    await page.getByLabel("Device name").fill("Output acceptance");
    await page.getByRole("button", {name: "Pair device", exact: true}).click();
    await expect(page.getByRole("button", {name: /Nebula Core (ready|degraded)/})).toBeVisible({timeout: 20_000});
    await page.goto(`${core.origin}/projects/${project.id}/workbench?view=notes`);
    await page.getByRole("button", {name: "New note", exact: true}).click();
    await page.getByLabel("Note title", {exact: true}).fill("Local fixture mechanism");
    await page.getByLabel("Note body", {exact: true}).fill("The local fixture button changes Ready to Saved. No external site was contacted.");
    let failSave = true;
    await page.route("**/api/v1/observations", async route => {
      if (failSave && route.request().method() === "POST") {
        failSave = false;
        await route.fulfill({status: 503, json: {detail: "Injected fixture save failure"}});
      } else await route.continue();
    });
    await page.getByRole("button", {name: "Save", exact: true}).click();
    await expect(page.getByText("Injected fixture save failure", {exact: false}).first()).toBeVisible();
    await expect(page.getByLabel("Note body", {exact: true})).toHaveValue(/Ready to Saved/);
    await page.getByRole("button", {name: "Save", exact: true}).click();
    await expect(page.getByRole("region", {name: "Edit Local fixture mechanism", exact: true})).toBeVisible();
    await page.reload();
    await page.getByRole("button", {name: /Local fixture mechanism/}).click();
    await expect(page.getByLabel("Note body", {exact: true})).toHaveValue(/Ready to Saved/);
    const notes = await (await api.get(`observations?engagement_id=${project.id}`)).json() as {id: string; title: string}[];
    expect(notes.filter(note => note.title === "Local fixture mechanism")).toHaveLength(1);
    await page.goto(`${core.origin}/projects/${project.id}/reports`);
    await page.getByRole("button", {name: "New report", exact: true}).click();
    const dialog = page.getByRole("dialog", {name: "New report", exact: true});
    await dialog.getByLabel("Title", {exact: true}).fill("Disposable mechanism report");
    await dialog.getByRole("button", {name: "Create report", exact: true}).click();
    await expect(dialog).toBeHidden();
    await page.getByLabel("Report title", {exact: true}).fill("Reviewed local mechanism");
    await page.getByRole("checkbox", {name: /Local fixture mechanism/}).check();
    await page.getByRole("button", {name: "Save report", exact: true}).click();
    await expect(page.getByRole("button", {name: "Save report", exact: true})).toBeDisabled();
    await page.reload();
    await expect(page.getByLabel("Report title", {exact: true})).toHaveValue("Reviewed local mechanism");
    await expect(page.getByRole("checkbox", {name: /Local fixture mechanism/})).toBeChecked();
    const reports = await (await api.get(`reports?engagement_id=${project.id}`)).json() as {observation_ids: string[]}[];
    expect(reports).toHaveLength(1);
    expect(reports[0].observation_ids).toContain(notes.find(note => note.title === "Local fixture mechanism")!.id);
    await page.goto(`${core.origin}/projects/${project.id}/workbench?view=notes`);
    await page.getByRole("button", {name: /Local fixture mechanism/}).click();
    await page.getByRole("button", {name: "Delete", exact: true}).click();
    await expect(page.getByText("This note is retained by a report", {exact: true})).toBeVisible();
    await expect(page.getByLabel("Note body", {exact: true})).toHaveValue(/Ready to Saved/);
    await testInfo.attach("saved-output-lineage", {body: JSON.stringify({origin: core.origin, notes, reports}), contentType: "application/json"});
    await testInfo.attach("note-retention", {body: await page.screenshot(), contentType: "image/png"});
  } finally {await api.dispose(); await stopRealCore(core);}
});

test("project execution mode saves host consent and executes against a host folder on production LAN", async ({ page }) => {
  test.setTimeout(90_000);
  const core = await startRealCore({ bindHost: "0.0.0.0", browserHost: localNetworkIpv4() });
  const backup = await mkdtemp(path.join(tmpdir(), "nebula-host-mode-backup-"));
  await writeFile(path.join(backup, "marker.txt"), "HOST_MODE_BACKUP_MARKER");
  const api = await playwrightRequest.newContext({ baseURL: `${core.origin}/api/v1/`, extraHTTPHeaders: { Authorization: `Bearer ${core.token}` } });
  try {
    await page.goto(`${core.origin}/findings#token=${encodeURIComponent(core.token)}`);
    await expect(page.getByRole("heading", { name: "Findings", exact: true })).toBeVisible({ timeout: 20_000 });
    const openPolicy = async () => {
      await page.getByRole("button", { name: "Search pages, actions, and settings" }).click();
      await page.getByRole("textbox", { name: "Search pages, actions, and settings" }).fill("network ports");
      await page.getByRole("option", { name: /Project policy and network scope/ }).click();
    };
    await openPolicy();
    const mode = page.getByRole("combobox", { name: "Project execution mode" });
    await expect(mode).toHaveValue("docker");
    await mode.selectOption("host");
    const save = page.getByRole("button", { name: "Save runtime policy" });
    await expect(save).toBeDisabled();
    await page.getByRole("checkbox", { name: /Allow host filesystem and network access/ }).check();
    await expect(save).toBeEnabled();
    await save.click();
    await expect(page.getByRole("status").filter({ hasText: "Runtime policy updated" })).toBeVisible();
    const projects = await (await api.get("engagements")).json() as Array<{ id: string }>;
    const projectId = projects[0].id;
    const policy = await (await api.get(`engagements/${projectId}/automation-policy`)).json();
    expect(policy).toMatchObject({ execution_mode: "host", host_access_acknowledged: true });
    const ready = await api.get(`automation/runtime?engagement_id=${projectId}`);
    expect(await ready.json()).toMatchObject({ ready: true, runner_profile_id: "host" });
    const command = await api.post(`engagements/${projectId}/automation-sessions/api/host-mode-test/commands`, { data: { command: "cat marker.txt", cwd: backup } });
    expect(command.ok(), await command.text()).toBe(true);
    expect(await command.json()).toMatchObject({ exit_code: 0, stdout: "HOST_MODE_BACKUP_MARKER" });
    await page.goto("about:blank");
    await page.goto(`${core.origin}/findings#token=${encodeURIComponent(core.token)}`);
    await expect(page.getByRole("heading", { name: "Findings", exact: true })).toBeVisible({ timeout: 20_000 });
    await openPolicy();
    await expect(mode).toHaveValue("host");
    await expect(page.getByRole("checkbox", { name: /Allow host filesystem and network access/ })).toBeChecked();
    await mode.selectOption("docker");
    await save.click();
    await expect(page.getByRole("status").filter({ hasText: "Runtime policy updated" })).toBeVisible();
    expect(await (await api.get(`engagements/${projectId}/automation-policy`)).json()).toMatchObject({ execution_mode: "docker", host_access_acknowledged: false });
  } finally {
    await api.dispose();
    await stopRealCore(core);
    await rm(backup, { recursive: true, force: true });
  }
});
