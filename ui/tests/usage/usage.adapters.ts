import type { Page, Route } from "@playwright/test";
import type { UsageCore } from "./usage.fixture";

const timestamp = "2026-07-18T14:15:00Z";

const entity = {
  created_at: timestamp,
  updated_at: timestamp,
  revision: 1,
};

async function json(route: Route, body: unknown, status = 200): Promise<void> {
  await route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
}

export async function installAssistantAdapter(page: Page, projectId: string): Promise<void> {
  const provider = {
    ...entity,
    id: "usage-local-provider",
    name: "Northstar Lab Model",
    provider_type: "vllm",
    endpoint: "http://127.0.0.1:8000/v1",
    enabled: true,
    is_local: true,
    secret_ref: null,
    model_allowlist: ["northstar-sec-8b"],
    capabilities: { streaming: true, cancellation: true },
    privacy: { local_only: true, permits_sensitive_data: true },
    metadata: { default_model: "northstar-sec-8b" },
  };
  let chatCreated = false;
  await page.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    if (path.endsWith("/providers") && request.method() === "GET") {
      await json(route, [provider]);
      return;
    }
    if (path.endsWith("/providers/usage-local-provider/health") && request.method() === "POST") {
      await json(route, { provider_id: provider.id, healthy: true, models: ["northstar-sec-8b"], detail: "Local model is ready." });
      return;
    }
    if (path.endsWith("/chat-sessions") && request.method() === "GET") {
      await json(route, chatCreated ? [{
        ...entity,
        id: "usage-chat-session",
        engagement_id: projectId,
        title: "TLS remediation priorities",
        backend: "provider",
        provider_profile_id: provider.id,
        model: "northstar-sec-8b",
        metadata: { tools_enabled: false },
      }] : []);
      return;
    }
    if (path.endsWith("/chat/completions") && request.method() === "POST") {
      chatCreated = true;
      const frames = [
        { type: "started", provider_id: provider.id, model: "northstar-sec-8b", session_id: "usage-chat-session", turn_id: "usage-turn-1" },
        { type: "delta", provider_id: provider.id, model: "northstar-sec-8b", delta: "The highest-priority action is to disable TLS 1.1 on the test API listener. " },
        { type: "delta", provider_id: provider.id, model: "northstar-sec-8b", delta: "Retest representative clients against TLS 1.2 and 1.3, then preserve the new protocol inventory as evidence.\n\n" },
        { type: "delta", provider_id: provider.id, model: "northstar-sec-8b", delta: "The rules of engagement require changes to remain inside `api.northstar.test`." },
        {
          type: "done",
          turn_id: "usage-turn-1",
          session_id: "usage-chat-session",
          provider_id: provider.id,
          model: "northstar-sec-8b",
          message: {
            id: "usage-assistant-message",
            role: "assistant",
            content: "The highest-priority action is to disable TLS 1.1 on the test API listener. Retest representative clients against TLS 1.2 and 1.3, then preserve the new protocol inventory as evidence.\n\nThe rules of engagement require changes to remain inside `api.northstar.test`.",
          },
          usage: { input_tokens: 418, output_tokens: 76, total_tokens: 494 },
          context_usage: { input_tokens: 215, output_tokens: 0, total_tokens: 215 },
          finish_reason: "stop",
          citations: [{
            source_id: "usage-roe-source",
            name: "Northstar rules of engagement",
            citation: "rules-of-engagement.md",
            chunk_id: "usage-roe-chunk-1",
            excerpt: "Testing is limited to api.northstar.test.",
          }],
        },
      ];
      await route.fulfill({
        status: 200,
        contentType: "text/event-stream",
        headers: { "Cache-Control": "no-cache" },
        body: `${frames.map((frame) => `data: ${JSON.stringify(frame)}\n\n`).join("")}data: [DONE]\n\n`,
      });
      return;
    }
    await route.fallback();
  });
}

export async function installMissionAdapter(page: Page, projectId: string): Promise<void> {
  const runId = "usage-mission";
  let approvalPending = true;
  const run = {
    ...entity,
    id: runId,
    engagement_id: projectId,
    objective: "Confirm externally visible services on the authorized test API without leaving project scope.",
    status: "waiting_approval",
    backend: "native",
    started_at: "2026-07-18T14:05:00Z",
    metadata: { name: "Northstar test API verification", completed_tasks: 3, total_tasks: 5, spent_usd: 0.18 },
  };
  const approval = {
    ...entity,
    id: "usage-approval",
    run_id: runId,
    engagement_id: projectId,
    origin: "mission",
    status: "pending",
    risk_class: "active_scan",
    requested_by: "Network analyst",
    target: "api.northstar.test:443",
    policy_rationale: "Confirm the TLS service on the one authorized test endpoint.",
    expected_effects: ["Sends a bounded service-detection request to TCP 443", "Stores the output in the mission ledger"],
    requested_at: "2026-07-18T14:14:30Z",
    exact_request: {
      tool_name: "nmap.service_detection",
      arguments: { target: "api.northstar.test", ports: [443], version_intensity: 2 },
      argv: ["nmap", "-sV", "--version-intensity", "2", "-p", "443", "api.northstar.test"],
      argument_editing: true,
      image: "ghcr.io/nebula/security-tools@sha256:approved-test-fixture",
      runtime_digest: "sha256:approved-test-fixture",
    },
  };
  const events = [
    { sequence: 41, id: "usage-event-41", kind: "run.started", run_id: runId, actor: "Supervisor", occurred_at: "2026-07-18T14:05:00Z", summary: "Mission plan accepted with five bounded tasks", payload: {} },
    { sequence: 42, id: "usage-event-42", kind: "task.turn_completed", run_id: runId, actor: "Scope planner", occurred_at: "2026-07-18T14:07:00Z", summary: "Scope restricted to api.northstar.test", payload: {} },
    { sequence: 43, id: "usage-event-43", kind: "tool.completed", run_id: runId, actor: "Recon specialist", occurred_at: "2026-07-18T14:10:00Z", summary: "Passive DNS review completed", payload: {} },
    { sequence: 44, id: "usage-event-44", kind: "approval.requested", run_id: runId, actor: "Network analyst", occurred_at: "2026-07-18T14:14:30Z", summary: "Service detection on TCP 443 is waiting for operator approval", payload: { approval_id: approval.id } },
  ];
  await installEventSocket(page, events);
  await page.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path.endsWith("/runs") && request.method() === "GET") {
      await json(route, [run]);
      return;
    }
    if (path.endsWith("/approvals") && request.method() === "GET") {
      await json(route, approvalPending ? [approval] : []);
      return;
    }
    if (path.endsWith("/approvals/usage-approval/decision") && request.method() === "POST") {
      approvalPending = false;
      await json(route, { ...approval, status: "approved", revision: 2, updated_at: "2026-07-18T14:16:00Z" });
      return;
    }
    if (path.endsWith(`/runs/${runId}/context`)) {
      await json(route, {
        owner_type: "run",
        owner_id: runId,
        status: "current",
        context_window: 8192,
        max_output_tokens: 2048,
        target_input_tokens: 4608,
        estimated_input_tokens: 1210,
        compacted_through: 0,
        source_references: [],
        compaction_usage: { input_tokens: 0, output_tokens: 0, total_tokens: 0 },
        compaction_cost_usd: 0,
      });
      return;
    }
    await route.fallback();
  });
}

async function installEventSocket(page: Page, events: unknown[]): Promise<void> {
  await page.addInitScript((eventFrames) => {
    const NativeWebSocket = globalThis.WebSocket;
    function ScenarioWebSocket(this: Record<string, unknown>, url: string | URL, protocols?: string | string[]) {
      if (!String(url).includes("/events/ws")) return new NativeWebSocket(url, protocols);
      const target = new EventTarget() as EventTarget & Record<string, unknown>;
      Object.assign(target, {
        url: String(url),
        protocol: "nebula.events.v1",
        extensions: "",
        bufferedAmount: 0,
        binaryType: "blob",
        readyState: 1,
        send: () => undefined,
        close: () => target.dispatchEvent(new CloseEvent("close", { code: 1000, reason: "scenario complete", wasClean: true })),
      });
      globalThis.setTimeout(() => {
        target.dispatchEvent(new Event("open"));
        eventFrames.forEach((event, index) => globalThis.setTimeout(() => {
          target.dispatchEvent(new MessageEvent("message", { data: JSON.stringify({ kind: "event", event }) }));
        }, 120 * (index + 1)));
      }, 20);
      return target;
    }
    Object.defineProperties(ScenarioWebSocket, {
      CONNECTING: { value: 0 }, OPEN: { value: 1 }, CLOSING: { value: 2 }, CLOSED: { value: 3 },
    });
    Object.defineProperty(globalThis, "WebSocket", { configurable: true, writable: true, value: ScenarioWebSocket });
  }, events);
}

export async function installTerminalAdapter(page: Page): Promise<void> {
  const digest = `sha256:${"b".repeat(64)}`;
  const imageDigest = `sha256:${"c".repeat(64)}`;
  const runtime = {
    source_image: "docker.io/kalilinux/kali-rolling:latest",
    interpreter: "/bin/bash",
    arguments: ["--noprofile", "--norc", "-i"],
    base_image: `docker.io/kalilinux/kali-rolling@${digest}`,
    base_image_digest: digest,
    image: imageDigest,
    image_digest: imageDigest,
    installed_packages: ["kali-linux-headless", "nmap"],
    runner_profile_id: "usage-local-runner",
    runner_profile_revision: 1,
    runner_runtime: "podman",
    runner_isolation: "rootless",
    runner_executable: "/usr/bin/podman",
    runner_platform: "linux/amd64",
  };
  const setup = {
    core: { status: "ready", detail: null },
    scratch_project_id: "scratch-project",
    terminal: {
      status: "ready",
      runner_profile_id: "usage-local-runner",
      candidates: [],
      image_preparation: {
        phase: "ready", operation_id: null, project_id: null, progress_percent: 100,
        progress_indeterminate: false, can_cancel: false, can_retry: false,
        image_digest: imageDigest, started_at: timestamp, completed_at: timestamp,
        detail: "Cached workstation image verified.",
      },
      detail: "Verified rootless local runtime.",
    },
    assistant: { status: "needs_model", provider_profile_id: null, detail: "Optional model connection not configured." },
  };
  await installTerminalSocket(page);
  await page.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path.endsWith("/setup/status") || path.endsWith("/setup/runtime/refresh")) {
      await json(route, setup);
      return;
    }
    if (path.endsWith("/container-terminals/recover") && request.method() === "POST") {
      await json(route, { sessions: [] });
      return;
    }
    if (path.endsWith("/container-terminal/capacity")) {
      await json(route, { active_sessions: 0, available_sessions: 32, max_active_sessions: 32 });
      return;
    }
    if (path.endsWith("/container-terminal/capabilities")) {
      await json(route, {
        engagement_id: "usage-project", ready: true, source_image: runtime.source_image,
        installed_packages: runtime.installed_packages, workspace: "/workspace",
        network: { mode: "unrestricted", runtime_network: "bridge", published_ports: [] },
        security: { container_user: "root", root_filesystem: "writable", linux_capabilities: [], no_new_privileges: true, host_network: false, runtime_socket: false, host_shell: false },
        limits: { cpu_count: 2, memory_mb: 2048, pids: 512, timeout_seconds: 1800, output_bytes_per_stream: 2_000_000 },
        idle_timeout_seconds: 1800, fresh_container: true, detail: null,
      });
      return;
    }
    if (path.endsWith("/container-terminal/preflight") && request.method() === "POST") {
      await json(route, {
        allowed: true, detail: "Request is confined to the authorized project workspace.", runtime,
        network: { mode: "unrestricted", runtime_network: "bridge", published_ports: [] },
        security: { container_user: "root", root_filesystem: "writable", linux_capabilities: [], no_new_privileges: true, host_network: false, runtime_socket: false, host_shell: false },
        limits: { cpu_count: 2, memory_mb: 2048, pids: 512, timeout_seconds: 1800, output_bytes_per_stream: 2_000_000 },
        workspace: "/workspace", policy_rule: "human_terminal_unrestricted",
        preview_fingerprint: "d".repeat(64), preview_token: "usage.preview.signed",
        expires_at: "2026-07-18T15:00:00Z", idle_timeout_seconds: 1800, fresh_container: true,
      });
      return;
    }
    if (path.endsWith("/container-terminal/sessions") && request.method() === "POST") {
      await json(route, {
        session_id: "usage-terminal", created_at: timestamp,
        websocket_ticket: "usage-one-use-ticket", ticket_expires_at: "2026-07-18T15:00:00Z",
        websocket_path: "/api/v1/container-terminals/usage-terminal/ws",
        reconnect_grace_seconds: 600, replay_max_bytes: 1_048_576, last_sequence: 0,
      });
      return;
    }
    if (path.endsWith("/terminal/commands/status")) {
      await json(route, {
        engagement_id: "usage-project", enabled: true, capture_mode: "selected_tools",
        record_count: 0, recorded_output_count: 0, metadata_only_count: 0,
        classification_failure_count: 0, degraded_count: 0, truncated_count: 0,
        audit_gap_count: 0, captured_output_bytes: 0, retention_days: 90, max_records: 10_000,
        oldest_recorded_at: null, newest_recorded_at: null,
      });
      return;
    }
    await route.fallback();
  });
}

async function installTerminalSocket(page: Page): Promise<void> {
  await page.addInitScript(() => {
    const NativeWebSocket = globalThis.WebSocket;
    function ScenarioWebSocket(this: Record<string, unknown>, url: string | URL, protocols?: string | string[]) {
      if (!String(url).includes("/container-terminals/")) return new NativeWebSocket(url, protocols);
      const target = new EventTarget() as EventTarget & Record<string, unknown>;
      let sequence = 0;
      let command = "";
      const output = (text: string) => {
        sequence += 1;
        target.dispatchEvent(new MessageEvent("message", { data: JSON.stringify({
          type: "output", encoding: "base64", sequence, data: btoa(text),
        }) }));
      };
      Object.assign(target, {
        url: String(url), protocol: "nebula.container-terminal.v1", extensions: "",
        bufferedAmount: 0, binaryType: "blob", readyState: 1,
        send: (raw: string) => {
          const frame = JSON.parse(raw) as { type: string; data?: string };
          if (frame.type !== "input" || !frame.data) return;
          output(frame.data);
          command += frame.data;
          if (!/[\r\n]/.test(frame.data)) return;
          const submitted = command.trim();
          command = "";
          if (submitted.includes("/etc/os-release")) {
            output("\r\nPRETTY_NAME=\"Kali GNU/Linux Rolling\"\r\nID=kali\r\nVERSION_CODENAME=kali-rolling\r\n");
          } else if (submitted.startsWith("printf")) {
            output("\r\n");
          } else if (submitted.startsWith("sha256sum")) {
            output("\r\n8d84c15a865c81ad9c36fb09118a578c83f88ad72db6327d8ccf63fc63d89b4d  target.txt\r\n");
          } else {
            output("\r\ncommand completed\r\n");
          }
          output("root@nebula:/workspace# ");
        },
        close: () => target.dispatchEvent(new CloseEvent("close", { code: 1000, reason: "scenario complete", wasClean: true })),
      });
      globalThis.setTimeout(() => {
        target.dispatchEvent(new MessageEvent("message", { data: JSON.stringify({
          type: "ready", max_duration_seconds: 0, idle_timeout_seconds: 1800,
          reconnect_grace_seconds: 600, replay_max_bytes: 1_048_576,
          reconnect_ticket: "usage-reconnect-ticket", replay_truncated: false,
        }) }));
        output("root@nebula:/workspace# ");
      }, 25);
      return target;
    }
    Object.defineProperties(ScenarioWebSocket, {
      CONNECTING: { value: 0 }, OPEN: { value: 1 }, CLOSING: { value: 2 }, CLOSED: { value: 3 },
    });
    Object.defineProperty(globalThis, "WebSocket", { configurable: true, writable: true, value: ScenarioWebSocket });
  });
}

export async function installDesktopBrowserAdapter(page: Page, core: UsageCore): Promise<void> {
  await page.addInitScript(({ endpoint, token }) => {
    const calls: Array<{ command: string; args: Record<string, unknown> }> = [];
    Object.assign(window, { __NEBULA_BROWSER_CALLS__: calls });
    Object.assign(window, {
      __TAURI_INTERNALS__: {
        invoke: async (command: string, args: Record<string, unknown> = {}) => {
          calls.push({ command, args });
          if (command === "start_local_backend") return { endpoint: `${endpoint}/api/v1`, token, protocol: "nebula-sidecar-v1" };
          if (command === "browser_capabilities") return { engine: "isolated system webview", projectStorage: "persistent" };
          return undefined;
        },
        transformCallback: () => 1,
        unregisterCallback: () => undefined,
        convertFileSrc: (value: string) => value,
      },
    });
  }, { endpoint: core.origin, token: core.token });
}
