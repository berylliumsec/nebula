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
  if (path.basename(core.dataDir).startsWith("nebula-playwright-real-core-")) {
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
    expect(modelStub.requests).toHaveLength(2);
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
    await page.locator(".session-select").filter({ hasText: "Switch target conversation" }).click();
    await expect.poll(() => new URL(page.url()).searchParams.get("session")).toBe(switchTarget.session_id);
    await expect(page.locator(".chat-message.operator").getByText("Switch target conversation", { exact: true })).toBeVisible();
    await page.locator(".session-select").filter({ hasText: savedSession!.title }).click();
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

    await page.getByRole("button", { name: "Switch project" }).click();
    await page.getByRole("dialog", { name: "Project switcher" }).getByRole("button", { name: /Background Project B/ }).click();
    await expect(page.getByRole("button", { name: "Switch project" })).toContainText("Background Project B");

    let sourceSessionId = "";
    await expect.poll(async () => {
      const sessionsResponse = await api.get(`chat-sessions?engagement_id=${encodeURIComponent(projectA.id)}`);
      if (!sessionsResponse.ok()) return "";
      const sessions = await sessionsResponse.json() as Array<{ id: string; title: string }>;
      sourceSessionId = sessions.find((session) => session.title === "Keep this response running while I switch projects")?.id ?? "";
      if (!sourceSessionId) return "";
      const messagesResponse = await api.get(`chat/sessions/${sourceSessionId}/messages`);
      if (!messagesResponse.ok()) return "";
      const messages = await messagesResponse.json() as Array<{ role: string; content: string }>;
      return messages.find((message) => message.role === "assistant")?.content ?? "";
    }, { timeout: 20_000 }).toBe("**Core is continuing in Project A** and finished after the viewer detached.");

    await page.getByRole("button", { name: "Switch project" }).click();
    await page.getByRole("dialog", { name: "Project switcher" }).getByRole("button", { name: new RegExp(projectA.name) }).click();
    await page.getByRole("button", { name: "Show conversations" }).click();
    await page.locator(".session-select").filter({ hasText: "Keep this response running while I switch projects" }).click();
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

test("mobile Code keeps its controls readable and saves to authoritative real-Core state", async ({ page }) => {
  test.setTimeout(120_000);
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
    await page.getByRole("textbox", { name: "Code editor" }).fill("def scan_target():\n    return True\n");
    await page.getByRole("textbox", { name: "Code editor" }).press("Control+S");
    await expect(page.getByText("Saved /workspace/scanner.py. Use it from Terminal when you're ready.")).toBeVisible();
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

    await page.getByRole("textbox", { name: "Code editor" }).fill("def scan_target():\n    return 'unsaved operator draft'\n");
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
    await expect(page).toHaveURL(/\/findings$/);
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
    await page.getByRole("textbox", { name: "Code editor" }).fill("exact unsaved λ research draft\n");
    await expect(page.getByText(/^3 open · 1 unsaved · recovery on$/)).toHaveText("3 open · 1 unsaved · recovery on");
    await page.waitForTimeout(350);
    page.once("dialog", (dialog) => void dialog.accept());
    await page.goto(`${core.origin}/?view=code#token=${encodeURIComponent(core.token)}`);
    await expect(page.getByRole("textbox", { name: "File path" })).toHaveValue("hot-exit-notes.txt");
    await expect(page.getByRole("textbox", { name: "Code editor" })).toContainText("exact unsaved λ research draft");
    await expect(page.getByText(/^3 open · 1 unsaved · recovery on$/)).toHaveText("3 open · 1 unsaved · recovery on");
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
    await debuggerPanel.getByRole("button", { name: "Toggle breakpoint at line 1" }).click();
    await debuggerPanel.getByRole("button", { name: "Start isolated debugger" }).click();
    await expect(debuggerPanel.getByText(/^stopped/)).toBeVisible({ timeout: 30_000 });
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
