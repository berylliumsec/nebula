import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Page } from "@playwright/test";

const workspaces = [
  ["workbench", "/", "Workbench"],
  ["findings", "/findings", "Findings"],
  ["reports", "/reports", "Reports"],
  ["project", "/project", "Scratch Project"],
  ["library", "/library", "Library"],
  ["settings", "/settings", "Settings"],
] as const;

const firstRunThemeTest = "Zero Dark is the first-run default theme";

const entity = {
  created_at: "2026-07-12T10:00:00Z",
  updated_at: "2026-07-12T11:00:00Z",
  revision: 1,
};

const runtime = {
  source_image: "docker.io/kalilinux/kali-rolling:latest",
  interpreter: "/bin/bash",
  arguments: ["--noprofile", "--norc", "-i"],
  base_image: `docker.io/kalilinux/kali-rolling@sha256:${"b".repeat(64)}`,
  base_image_digest: `sha256:${"b".repeat(64)}`,
  image: `sha256:${"c".repeat(64)}`,
  image_digest: `sha256:${"c".repeat(64)}`,
  installed_packages: ["kali-linux-headless", "iputils-ping"],
  runner_profile_id: "local",
  runner_profile_revision: 1,
  runner_runtime: "podman",
  runner_isolation: "rootless",
  runner_executable: "/usr/bin/podman",
  runner_platform: "linux/amd64",
};

const network = { mode: "unrestricted", runtime_network: "bridge", published_ports: [] };
const security = {
  container_user: "root",
  root_filesystem: "writable",
  linux_capabilities: [],
  no_new_privileges: true,
  host_network: false,
  runtime_socket: false,
  host_shell: false,
};
const limits = {
  cpu_count: 2,
  memory_mb: 2048,
  pids: 512,
  timeout_seconds: 1800,
  output_bytes_per_stream: 2_000_000,
};

async function installTruthfulCore(page: Page) {
  await page.addInitScript(() => {
    (globalThis as typeof globalThis & { __terminalFrames?: unknown[] }).__terminalFrames = [];
    class PreviewTerminalWebSocket extends EventTarget {
      static readonly CONNECTING = 0;
      static readonly OPEN = 1;
      static readonly CLOSING = 2;
      static readonly CLOSED = 3;
      readonly url: string;
      readonly protocol = "nebula.container-terminal.v1";
      readonly extensions = "";
      readonly bufferedAmount = 0;
      readonly binaryType = "blob";
      readyState = PreviewTerminalWebSocket.CONNECTING;

      constructor(url: string | URL) {
        super();
        this.url = String(url);
        globalThis.setTimeout(() => {
          this.readyState = PreviewTerminalWebSocket.OPEN;
          this.dispatchEvent(new MessageEvent("message", { data: JSON.stringify({
            type: "ready",
            max_duration_seconds: 0,
            idle_timeout_seconds: 1800,
            reconnect_grace_seconds: 600,
            replay_max_bytes: 1_048_576,
            reconnect_ticket: "preview-reconnect-ticket",
            replay_truncated: false,
          }) }));
          this.dispatchEvent(new MessageEvent("message", { data: JSON.stringify({
            type: "output",
            encoding: "base64",
            sequence: 1,
            data: btoa("root@nebula:/workspace# "),
          }) }));
        }, 10);
      }

      send(value: string): void {
        try {
          (globalThis as typeof globalThis & { __terminalFrames?: unknown[] }).__terminalFrames?.push(JSON.parse(value));
        } catch {
          // The production transport sends JSON text frames only.
        }
      }
      close(code = 1000, reason = "preview closed"): void {
        if (this.readyState === PreviewTerminalWebSocket.CLOSED) return;
        this.readyState = PreviewTerminalWebSocket.CLOSED;
        this.dispatchEvent(new CloseEvent("close", { code, reason, wasClean: true }));
      }
    }
    Object.defineProperty(globalThis, "WebSocket", {
      configurable: true,
      writable: true,
      value: PreviewTerminalWebSocket,
    });
  });

  // WebKit can replace the underlying page target during full document
  // navigations. Keep the permanent Core fixture on the browser context so
  // the API authority survives every route in the visual-audit journey.
  await page.context().route("**/api/v1/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    let body: unknown = [];
    if (path.endsWith("/health")) {
      body = {
        status: "ok",
        version: "3.0.0",
        mode: "local",
        runner: "ready",
        human_pty: "unavailable",
        container_terminal: "configured",
        diagnostics: {
          writable: true,
          degraded: false,
          browser_event_ingress: "enabled",
        },
      };
    } else if (path.endsWith("/diagnostics/settings")) {
      body = {
        schema: "nebula.diagnostics-settings/v1",
        global_level: "error",
        feature_levels: {},
      };
    } else if (path.endsWith("/diagnostics/files")) {
      body = {
        files: [{ name: "chat.log", size_bytes: 2048, modified_at: "2026-07-14T12:00:00Z" }],
        health: {
          schema: "nebula.diagnostics-status/v1",
          writable: true,
          degraded: false,
          global_level: "error",
          feature_levels: {},
          disk_usage_bytes: 2048,
          dropped_record_count: 0,
        },
      };
    } else if (path.endsWith("/diagnostics/errors")) {
      body = { errors: [{
        schema: "nebula.diagnostic/v1",
        timestamp: "2026-07-14T12:00:00Z",
        sequence: 12,
        level: "ERROR",
        feature: "chat",
        source: "core",
        event_code: "chat.stream.failed",
        message: "The assistant response stream stopped unexpectedly.",
        safe_failure_cause: "The configured model provider stopped the stream.",
        stage: "stream",
        outcome: "failure",
        retryable: true,
        error_id: "err_preview_123",
        request_id: "req_preview_123",
        exception_type: "ProviderError",
        stack_frames: [{ module: "chat", function: "stream", line: 42 }],
        metadata: { component: "response_stream", provider: "local" },
      }] };
    } else if (path.endsWith("/auth/devices")) {
      body = [];
    } else if (path.endsWith("/auth/pairings")) {
      body = {
        secret: "preview-single-use-secret",
        confirmation_code: "123456",
        expires_at: "2026-07-14T12:05:00Z",
      };
    } else if (path.endsWith("/setup/status") || path.endsWith("/setup/runtime/refresh")) {
      body = {
        core: { status: "ready", detail: null },
        scratch_project_id: "scratch-project",
        terminal: {
          status: "ready",
          runner_profile_id: "local",
          candidates: [{
            candidate_id: `fixed:${"a".repeat(32)}`,
            runner_profile_id: "local",
            source: "detected",
            name: "Local Podman",
            runtime: "podman",
            executable: "/usr/bin/podman",
            context: null,
            platform: "linux/amd64",
            isolation: "rootless",
            healthy: true,
            detail: "Verified fixed-path local runtime.",
          }],
          image_preparation: {
            phase: "ready",
            operation_id: null,
            project_id: "scratch-project",
            progress_percent: 100,
            progress_indeterminate: false,
            can_cancel: false,
            can_retry: false,
            image_digest: runtime.image_digest,
            started_at: "2026-07-12T09:59:00Z",
            completed_at: "2026-07-12T10:00:00Z",
            detail: "Cached workstation image verified.",
          },
          detail: "Verified fixed-path local runtime.",
        },
        assistant: { status: "needs_model", provider_profile_id: null, detail: "Optional model connection not configured." },
      };
    } else if (path.endsWith("/workspace-folders")) {
      const requested = url.searchParams.get("path") ?? "/home/agent";
      body = {
        path: requested,
        parent: requested === "/home/agent" ? "/home" : "/home/agent",
        directories: requested === "/home/agent" ? [{
          name: "a-very-long-project-folder-name-that-must-not-expand-the-dialog",
          path: "/home/agent/a-very-long-project-folder-name-that-must-not-expand-the-dialog",
        }] : [],
        truncated: false,
      };
    } else if (path.endsWith("/engagements/scratch-project/scope")) {
      body = {
        ...entity,
        id: "scope-scratch",
        engagement_id: "scratch-project",
        allowed_cidrs: [],
        allowed_domains: ["example.com"],
        allowed_urls: [],
        allowed_ports: [443],
        allow_all_targets: false,
        not_before: null,
        not_after: null,
        prohibited_actions: [],
        local_only: true,
        max_concurrency: 1,
        grants: [],
        revision: 3,
      };
    } else if (path.endsWith("/engagements/scratch-project/browser-workspace")) {
      body = {
        identities: [{ ...entity, id: "browser-identity-preview", engagement_id: "scratch-project", name: "Default identity", description: "Project-isolated preview profile", color: "#7c6cff", storage_partition: "browser-00000000-0000-0000-0000-000000000000", ephemeral: false, is_default: true, revoked_at: null, metadata: {} }],
        sessions: [{ ...entity, id: "browser-session-preview", engagement_id: "scratch-project", name: "Research session", identity_id: "browser-identity-preview", status: "active", capture_mode: "headers", proxy_enabled: false, tabs: [], active_tab_id: null, upstream_proxy_enabled: false, upstream_proxy_url: null, interception_enabled: false, device_owner: null, last_seen_at: entity.updated_at, metadata: {} }],
        traffic: [],
        frames: [],
        actions: [],
        handoffs: [],
      };
    } else if (path.endsWith("/engagements/scratch-project/browser-research")) {
      body = {
        site_nodes: [],
        crawl_jobs: [],
        intercepts: [],
        repeater_tabs: [{
          ...entity,
          id: "repeater-preview",
          engagement_id: "scratch-project",
          session_id: "browser-session-preview",
          identity_id: "browser-identity-preview",
          name: "Profile lookup",
          group: "Ungrouped",
          notes: "",
          protocol: "http",
          method: "GET",
          url: "https://example.com/profile",
          headers: [["Accept", "application/json"]],
          body_template: "",
          history_exchange_ids: ["repeater-result-preview"],
          evidence_ids: [],
          state: "ready",
          request_count: 1,
          error: null,
        }],
        repeater_results: [{
          ...entity,
          id: "repeater-result-preview",
          engagement_id: "scratch-project",
          tab_id: "repeater-preview",
          sequence: 0,
          exchange_id: null,
          status_code: 200,
          response_headers: [["content-type", "application/json"]],
          response_bytes: 128,
          duration_ms: 18,
          response_body_artifact_id: null,
          error: null,
        }],
        attacks: [],
        attack_results: [],
        token_analyses: [],
      };
    } else if (path.endsWith("/browser-sessions/browser-session-preview/tabs") && request.method() === "PUT") {
      const update = request.postDataJSON() as { tabs: unknown[]; active_tab_id: string | null; device_owner: string };
      body = { ...entity, revision: 2, id: "browser-session-preview", engagement_id: "scratch-project", name: "Research session", identity_id: "browser-identity-preview", status: "active", capture_mode: "headers", proxy_enabled: false, tabs: update.tabs, active_tab_id: update.active_tab_id, upstream_proxy_enabled: false, upstream_proxy_url: null, interception_enabled: false, device_owner: update.device_owner, last_seen_at: entity.updated_at, metadata: {} };
    } else if (path.endsWith("/engagements")) {
      body = [{
        ...entity,
        id: "scratch-project",
        name: "Scratch Project",
        description: "A local workspace ready for terminal testing.",
        status: "active",
        tags: [],
        metadata: { created_by: "system:bootstrap", bootstrap_kind: "scratch_project_v1" },
      }];
    } else if (path.endsWith("/container-terminal/capabilities")) {
      body = {
        engagement_id: "scratch-project",
        ready: true,
        source_image: runtime.source_image,
        installed_packages: runtime.installed_packages,
        workspace: "/workspace",
        network,
        security,
        limits,
        idle_timeout_seconds: 1800,
        fresh_container: true,
        detail: null,
      };
    } else if (path.endsWith("/container-terminal/preflight") && request.method() === "POST") {
      body = {
        allowed: true,
        detail: "Request is confined to the Scratch Project workspace.",
        runtime,
        network,
        security,
        limits,
        workspace: "/workspace",
        policy_rule: "human_terminal_unrestricted",
        preview_fingerprint: "d".repeat(64),
        preview_token: "preview.signed",
        expires_at: "2026-07-13T21:00:00Z",
        idle_timeout_seconds: 1800,
        fresh_container: true,
      };
    } else if (path.endsWith("/container-terminals/recover") && request.method() === "POST") {
      body = { sessions: [] };
    } else if (path.endsWith("/container-terminal/recover") && request.method() === "POST") {
      body = { active: false };
    } else if (path.endsWith("/container-terminal/capacity")) {
      body = { active_sessions: 1, available_sessions: 31, max_active_sessions: 32 };
    } else if (path.endsWith("/container-terminal/sessions") && request.method() === "POST") {
      body = {
        session_id: "terminal-preview",
        websocket_ticket: "preview-one-use-ticket",
        ticket_expires_at: "2026-07-13T21:00:00Z",
        websocket_path: "/api/v1/container-terminals/terminal-preview/ws",
        reconnect_grace_seconds: 600,
        replay_max_bytes: 1_048_576,
        last_sequence: 0,
      };
    } else if (path.endsWith("/evidence/upload") && request.method() === "POST") {
      const upload = request.postDataJSON() as { title?: string; evidence_type?: string; metadata?: Record<string, unknown> };
      body = {
        ...entity,
        id: "terminal-screenshot-evidence",
        engagement_id: "scratch-project",
        evidence_type: upload.evidence_type ?? "terminal-screenshot",
        title: upload.title ?? "Terminal screenshot",
        description: "Immutable capture of the visible Nebula terminal viewport.",
        artifact_id: "terminal-screenshot-artifact",
        finding_id: null,
        execution_id: null,
        asset_ids: [],
        sha256: "e".repeat(64),
        captured_at: "2026-07-13T20:00:00Z",
        captured_by: null,
        source_version: "terminal-viewport-v1",
        metadata: upload.metadata ?? {},
      };
    } else if (path.endsWith("/provider-catalog")) {
      body = [{
        flavor: "vllm",
        adapter: "openai_compatible",
        display_name: "Local vLLM",
        local: true,
        default_base_url: "http://127.0.0.1:8000/v1",
        suggested_key_env: null,
        support_tier: "native",
        notes: "Models are discovered from the selected local runtime.",
      }];
    } else if (path.endsWith("/providers/discover-local")) {
      body = [];
    } else if (path.endsWith("/terminal/commands/status")) {
      body = {
        engagement_id: "scratch-project",
        enabled: true,
        capture_mode: "selected_tools",
        record_count: 0,
        recorded_output_count: 0,
        metadata_only_count: 0,
        classification_failure_count: 0,
        degraded_count: 0,
        truncated_count: 0,
        audit_gap_count: 0,
        captured_output_bytes: 0,
        retention_days: 90,
        max_records: 10_000,
        oldest_recorded_at: null,
        newest_recorded_at: null,
      };
    } else if (path.endsWith("/terminal/commands")) {
      body = { records: [], total: 0, offset: 0, limit: 100, next_offset: null };
    } else if (path.endsWith("/terminal/recording-tools")) {
      body = {
        engagement_id: "scratch-project",
        inventory_status: "verified",
        runtime_image_digest: runtime.image_digest,
        manifest_sha256: "f".repeat(64),
        default_tools: ["nmap", "nikto"],
        custom_tools: [],
        disabled_tools: [],
        effective_tools: ["nmap", "nikto"],
        revision: 1,
        updated_at: entity.updated_at,
      };
    } else if (path.endsWith("/workspace/reset-status")) {
      body = {
        engagement_id: "scratch-project",
        can_reset: true,
        active_terminal_count: 0,
        active_execution_count: 0,
        reason_code: null,
        detail: "No active terminal or reviewed execution is using the workspace.",
      };
    } else if (path.endsWith("/workspace/source-control/diff")) {
      body = {
        engagement_id: "scratch-project",
        path: "scanner.py",
        staged: false,
        text: "@@ -1 +1 @@\n-return 'old'\n+return 'changed'",
        truncated: false,
        head: "abcdef123456",
      };
    } else if (path.endsWith("/workspace/source-control")) {
      body = {
        engagement_id: "scratch-project",
        state: "ready",
        branch: "research/mock",
        head: "abcdef123456",
        files: [{
          path: "scanner.py",
          index_status: "unmodified",
          worktree_status: "modified",
          original_path: null,
        }],
        truncated: false,
        detail: "1 changed path.",
      };
    } else if (path.endsWith("/workspace/tasks")) {
      body = {
        engagement_id: "scratch-project",
        tasks: [
          { id: "a".repeat(64), label: "Inspect Python", command: "python --version", kind: "test", source: ".vscode/tasks.json", detail: "VS Code process task", supported: true, unsupported_reason: null },
          { id: "b".repeat(64), label: "Extension-owned task", command: ":", kind: "custom", source: ".vscode/tasks.json", detail: "VS Code task", supported: false, unsupported_reason: "Task type 'npm' requires a VS Code extension and is not executed by Nebula." },
        ],
        scanned_entries: 4,
        truncated: false,
      };
    } else if (path.endsWith("/workspace/debug-configurations")) {
      body = {
        engagement_id: "scratch-project",
        active_path: "scanner.py",
        configurations: [],
        truncated: false,
      };
    } else if (path.endsWith("/workspace")) {
      body = {
        engagement_id: "scratch-project",
        path: "",
        entries: [],
        offset: 0,
        next_offset: null,
        total: 0,
      };
    }
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(body),
    });
  });
}

async function openWorkspace(page: Page, route: string, heading: string) {
  if (page.url() === "about:blank") {
    await page.goto(route);
  } else {
    await page.evaluate((nextRoute) => {
      const previous = location.href;
      const next = new URL(nextRoute, location.origin);
      history.pushState({}, "", `${next.pathname}${next.search}${next.hash}`);
      dispatchEvent(new PopStateEvent("popstate"));
      if (new URL(previous).hash !== next.hash) {
        dispatchEvent(new HashChangeEvent("hashchange", { oldURL: previous, newURL: next.href }));
      }
    }, route);
  }
  if (route === "/?view=browser") {
    await expect.poll(() => page.evaluate(() => ({
      pathname: location.pathname,
      view: new URLSearchParams(location.search).get("view"),
    }))).toEqual({ pathname: "/", view: "browser" });
  } else {
    await expect.poll(() => page.evaluate(() => `${location.pathname}${location.search}${location.hash}`)).toBe(route);
  }
  if (heading === "Workbench") {
    if ((page.viewportSize()?.width ?? 1_000) <= 760) {
      await expect(page.getByRole("navigation", { name: "Mobile operator navigation" })).toBeVisible({ timeout: 15_000 });
    } else {
      await expect(page.getByRole("tab", { name: "Terminal", exact: true })).toBeVisible({ timeout: 15_000 });
    }
    await expect(page.getByText("Start in Terminal, edit shared code, browse a target, ask the assistant, or open your project files.")).toHaveCount(0);
  } else {
    await expect(page.getByRole("heading", { name: heading, exact: true })).toBeVisible({ timeout: 15_000 });
  }
  await expect(page.locator("#nebula-boot")).toHaveCount(0);
  await expect(page.getByText("Interface preview")).toHaveCount(0);
  await expect(page.getByText(/Jordan|Acme/i)).toHaveCount(0);
  if (route === "/") {
    const liveTerminal = page.locator(".container-terminal-live");
    await expect(liveTerminal).toBeVisible({ timeout: 20_000 });
    await expect(liveTerminal.getByText("Connected", { exact: true })).toBeVisible();
    await expect(liveTerminal.getByRole("button", { name: "Screenshot" })).toBeVisible();
  }
  await page.evaluate(() => document.fonts.ready);
  await page.waitForTimeout(120);
}

async function setTheme(page: Page, theme: "zero-dark" | "zero-light") {
  await page.evaluate((value) => {
    const oldValue = localStorage.getItem("nebula.theme");
    localStorage.setItem("nebula.theme", value);
    dispatchEvent(new StorageEvent("storage", { key: "nebula.theme", oldValue, newValue: value }));
  }, theme);
  await expect(page.locator("html")).toHaveAttribute("data-theme", theme);
}

async function findPathologicalText(page: Page) {
  return page.locator(".page").evaluateAll((roots) => {
    const issues: string[] = [];
    const candidates = roots.flatMap((root) => [...root.querySelectorAll<HTMLElement>("h1, h2, h3, p, strong, small, dd")]);
    for (const element of candidates) {
      const style = getComputedStyle(element);
      const rect = element.getBoundingClientRect();
      const text = element.textContent?.replace(/\s+/g, " ").trim() ?? "";
      if (!text || text.length < 18 || rect.width <= 0 || rect.height <= 0 || style.visibility === "hidden") continue;
      const lineHeight = Number.parseFloat(style.lineHeight) || Number.parseFloat(style.fontSize) * 1.2;
      if (rect.width < 64 && rect.height > lineHeight * 3.25) {
        issues.push(`${element.tagName.toLowerCase()}.${element.className}: ${Math.round(rect.width)}x${Math.round(rect.height)} "${text.slice(0, 72)}"`);
        continue;
      }
      const walker = document.createTreeWalker(element, NodeFilter.SHOW_TEXT);
      let textNode = walker.nextNode() as Text | null;
      while (textNode) {
        const value = textNode.data;
        for (const match of value.matchAll(/[A-Za-z]{8,}/g)) {
          const range = document.createRange();
          range.setStart(textNode, match.index ?? 0);
          range.setEnd(textNode, (match.index ?? 0) + match[0].length);
          const lineTops = new Set([...range.getClientRects()].filter((box) => box.width > 0).map((box) => Math.round(box.top)));
          if (lineTops.size > 2) {
            issues.push(`${element.tagName.toLowerCase()}.${element.className}: word "${match[0]}" split across ${lineTops.size} lines`);
            break;
          }
        }
        textNode = walker.nextNode() as Text | null;
      }
    }
    return issues;
  });
}

test.beforeEach(async ({ page }, testInfo) => {
  await installTruthfulCore(page);
  if (testInfo.title !== firstRunThemeTest) {
    await page.addInitScript(() => {
      if (localStorage.getItem("nebula.theme") === null) localStorage.setItem("nebula.theme", "zero-dark");
    });
  }
});

test("browser keeps native bounds and opens scoped live context as a reviewed AI draft", async ({ browser }, testInfo) => {
  test.skip(testInfo.project.name !== "desktop", "Native browser geometry needs one explicit desktop run.");
  const context = await browser.newContext({
    viewport: { width: 1440, height: 900 },
    deviceScaleFactor: 2,
    colorScheme: "dark",
    reducedMotion: "reduce",
  });
  const page = await context.newPage();
  await installTruthfulCore(page);
  await page.addInitScript(() => {
    localStorage.setItem("nebula.theme", "zero-dark");
    const calls: Array<{ command: string; args: Record<string, unknown> }> = [];
    const callbacks = new Map<number, (value: unknown) => void>();
    const eventHandlers = new Map<string, number>();
    let callbackId = 0;
    const emit = (event: string, payload: unknown) => {
      const handler = eventHandlers.get(event);
      if (handler !== undefined) callbacks.get(handler)?.({ event, id: 1, payload });
    };
    Object.assign(window, { __NEBULA_BROWSER_CALLS__: calls });
    Object.assign(window, {
      __TAURI_INTERNALS__: {
        invoke: async (command: string, args: Record<string, unknown> = {}) => {
          calls.push({ command, args });
          if (command === "resolve_backend_connection") {
            return { endpoint: `${location.origin}/api/v1`, token: "", protocol: "nebula-sidecar-v1", source: "local" };
          }
          if (command === "desktop_device_id") return "desktop-playwright";
          if (command === "browser_capabilities") {
            return { engine: "Playwright native-bounds mock", projectStorage: "persistent" };
          }
          if (command === "plugin:event|listen") {
            eventHandlers.set(String(args.event), Number(args.handler));
            return args.handler;
          }
          if (command === "plugin:event|unlisten") return undefined;
          if (command === "browser_create_tab") {
            queueMicrotask(() => emit("nebula-browser-page", {
              tabId: args.tabId,
              url: args.url,
              state: "loaded",
            }));
          }
          if (command === "browser_capture_context") {
            queueMicrotask(() => emit("nebula-browser-context", {
              requestId: args.requestId,
              tabId: args.tabId,
              state: "ready",
              context: {
                url: "https://example.com/account",
                title: "Mock target account",
                selectedText: "role=analyst",
                text: "Authenticated account portal",
                truncated: false,
                forms: [{
                  method: "POST",
                  action: "https://example.com/profile",
                  fields: [{ name: "display_name", id: "name", type: "text", autocomplete: "name", required: true }],
                }],
                links: [{ text: "Billing", href: "https://example.com/billing" }],
              },
            }));
          }
          return undefined;
        },
        transformCallback: (callback: (value: unknown) => void) => {
          callbackId += 1;
          callbacks.set(callbackId, callback);
          return callbackId;
        },
        unregisterCallback: (id: number) => callbacks.delete(id),
        convertFileSrc: (path: string) => path,
      },
    });
  });
  await openWorkspace(page, "/", "Workbench");
  await page.getByRole("tab", { name: "Project browser", exact: true }).click();
  await page.getByRole("textbox", { name: "Start browsing" }).fill("example.com");
  await page.getByRole("textbox", { name: "Start browsing" }).press("Enter");
  await expect.poll(() => page.evaluate(() => (
    (window as Window & { __NEBULA_BROWSER_CALLS__?: Array<{ command: string }> })
      .__NEBULA_BROWSER_CALLS__?.some((call) => call.command === "browser_create_tab")
  ))).toBe(true);
  const addressField = page.getByRole("textbox", { name: "Address or search" });
  await addressField.focus();
  await expect(addressField).toBeFocused();
  await expect.poll(() => addressField.evaluate((address) => {
    const addressShell = address.closest("form")!;
    const addressStyle = getComputedStyle(address);
    const addressShellStyle = getComputedStyle(addressShell);
    return {
      borderWidth: addressStyle.borderTopWidth,
      boxShadow: addressStyle.boxShadow,
      outlineStyle: addressStyle.outlineStyle,
      shellBorderWidth: addressShellStyle.borderTopWidth,
      shellBorderColor: addressShellStyle.borderTopColor,
      shellHasFocusWithin: addressShell.matches(":focus-within"),
    };
  })).toEqual({
    borderWidth: "0px",
    boxShadow: "none",
    outlineStyle: "none",
    shellBorderWidth: "1px",
    shellBorderColor: "rgb(104, 168, 255)",
    shellHasFocusWithin: true,
  });

  const geometry = await page.evaluate(() => {
    const toolbar = document.querySelector<HTMLElement>(".browser-toolbar")!;
    const address = document.querySelector<HTMLInputElement>("#browser-address")!;
    const surface = document.querySelector<HTMLElement>(".browser-surface")!;
    const browserPanel = document.querySelector<HTMLElement>(".workbench-browser")!;
    const toolbarRect = toolbar.getBoundingClientRect();
    const addressRect = address.getBoundingClientRect();
    const surfaceRect = surface.getBoundingClientRect();
    const panelRect = browserPanel.getBoundingClientRect();
    const calls = (window as Window & { __NEBULA_BROWSER_CALLS__?: Array<{ command: string; args: Record<string, unknown> }> }).__NEBULA_BROWSER_CALLS__ ?? [];
    const create = calls.find((call) => call.command === "browser_create_tab");
    return {
      toolbar: { top: toolbarRect.top, bottom: toolbarRect.bottom, height: toolbarRect.height },
      address: { top: addressRect.top, bottom: addressRect.bottom, height: addressRect.height },
      surfaceTop: surfaceRect.top,
      panelBottom: panelRect.bottom,
      bounds: create?.args.bounds as { y: number; height: number },
      devicePixelRatio: window.devicePixelRatio,
    };
  });
  expect(geometry.toolbar.height).toBeGreaterThanOrEqual(48);
  expect(geometry.address.top).toBeGreaterThanOrEqual(geometry.toolbar.top);
  expect(geometry.address.bottom).toBeLessThanOrEqual(geometry.toolbar.bottom);
  expect(geometry.surfaceTop).toBeGreaterThanOrEqual(geometry.toolbar.bottom);
  expect(geometry.bounds.y).toBeGreaterThanOrEqual(geometry.toolbar.bottom);
  expect(geometry.bounds.y * geometry.devicePixelRatio).toBe(
    Math.ceil(geometry.toolbar.bottom * geometry.devicePixelRatio),
  );
  expect(geometry.bounds.y + geometry.bounds.height).toBeLessThanOrEqual(geometry.panelBottom + 1);
  expect(geometry.devicePixelRatio).toBe(2);
  await expect(page.getByText("In scope")).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath("browser-address-bar-2x.png") });

  await page.getByRole("button", { name: "Ask Nebula about the live page" }).click();
  await expect(page).toHaveURL(/view=chat/);
  const attachment = page.getByRole("region", { name: "Selected context pack" });
  await expect(attachment).toContainText("Browser · Mock target account");
  await expect(attachment).toContainText("characters");
  const composer = page.getByRole("textbox", { name: "Message the analyst assistant" });
  await expect(composer).toBeDisabled();
  await expect(composer).toHaveAttribute("placeholder", "Add a model or harness in Settings…");
  await expect(page.locator(".chat-message")).toHaveCount(0);
  await context.close();
});

test("terminal screenshot capture opens a full-height integrated editor", async ({ page }, testInfo) => {
  await openWorkspace(page, "/", "Workbench");
  const uploadRequest = page.waitForRequest((request) => request.url().endsWith("/evidence/upload") && request.method() === "POST");
  await page.getByRole("button", { name: "Screenshot" }).click();
  const upload = (await uploadRequest).postDataJSON() as {
    content_base64: string;
    media_type: string;
    metadata: { pixel_width: number; pixel_height: number };
  };
  expect(upload.media_type).toBe("image/png");
  expect(upload.content_base64.startsWith("iVBOR")).toBe(true);
  expect(upload.metadata.pixel_width).toBeGreaterThan(0);
  expect(upload.metadata.pixel_height).toBeGreaterThan(0);

  const dialog = page.getByRole("dialog", { name: "Edit terminal screenshot" });
  await expect(dialog).toBeVisible();
  await expect(dialog.getByRole("img", { name: /Editable image/ })).toBeVisible();
  await expect(dialog).toContainText("Original preserved");

  const readDimensions = () => dialog.evaluate((element) => {
    const editor = element.querySelector<HTMLElement>('[aria-label="Image editor"]');
    const viewport = editor?.querySelector<HTMLElement>("div[class*='viewport']");
    const canvas = viewport?.querySelector<HTMLCanvasElement>("canvas");
    const viewportStyle = viewport ? getComputedStyle(viewport) : undefined;
    return {
      dialogHeight: element.getBoundingClientRect().height,
      editorHeight: editor?.getBoundingClientRect().height ?? 0,
      viewportHeight: viewport?.getBoundingClientRect().height ?? 0,
      viewportContentWidth: (viewport?.clientWidth ?? 0)
        - Number.parseFloat(viewportStyle?.paddingLeft ?? "0")
        - Number.parseFloat(viewportStyle?.paddingRight ?? "0"),
      viewportContentHeight: (viewport?.clientHeight ?? 0)
        - Number.parseFloat(viewportStyle?.paddingTop ?? "0")
        - Number.parseFloat(viewportStyle?.paddingBottom ?? "0"),
      canvasWidth: canvas?.getBoundingClientRect().width ?? 0,
      canvasHeight: canvas?.getBoundingClientRect().height ?? 0,
    };
  });
  await expect.poll(async () => {
    const dimensions = await readDimensions();
    return dimensions.canvasWidth <= dimensions.viewportContentWidth + 1
      && dimensions.canvasHeight <= dimensions.viewportContentHeight + 1;
  }, { timeout: 15_000 }).toBe(true);
  const dimensions = await readDimensions();
  const mobile = (page.viewportSize()?.width ?? 1_000) <= 760;
  const viewportHeight = page.viewportSize()?.height ?? 900;
  expect(dimensions.dialogHeight).toBeGreaterThan(Math.min(760, viewportHeight - 80));
  expect(dimensions.editorHeight).toBeGreaterThan(mobile
    ? Math.min(440, viewportHeight - 190)
    : Math.min(650, viewportHeight - 160));
  expect(dimensions.viewportHeight).toBeGreaterThan(mobile
    ? Math.min(320, viewportHeight - 310)
    : Math.min(440, viewportHeight - 290));
  expect(dimensions.canvasWidth).toBeLessThanOrEqual(dimensions.viewportContentWidth + 1);
  expect(dimensions.canvasHeight).toBeLessThanOrEqual(dimensions.viewportContentHeight + 1);
});

test("terminal pointer selection has a visible high-contrast highlight", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "desktop", "Canvas selection rendering needs one desktop visual run.");
  await openWorkspace(page, "/", "Workbench");
  await page.getByRole("tab", { name: "Terminal", exact: true }).click();
  const screen = page.locator(".xterm-screen").last();
  await expect(screen).toBeVisible();
  const rows = screen.locator(".xterm-rows");
  await expect(rows).toContainText("root@nebula:/workspace#");
  const promptRow = rows.locator(":scope > div").filter({ hasText: "root@nebula:/workspace#" }).first();
  await expect(promptRow).toBeVisible();
  const box = await promptRow.boundingBox();
  expect(box).toBeTruthy();
  const y = box!.y + box!.height / 2;
  await page.mouse.dblclick(box!.x + 12, y, { delay: 75 });
  await expect(screen.locator(".xterm-selection > div").first()).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath("terminal-visible-selection.png") });
  const selectionRects = await screen.locator(".xterm-selection > div").evaluateAll((rectangles) =>
    rectangles.map((rectangle) => getComputedStyle(rectangle).backgroundColor),
  );
  expect(selectionRects.length).toBeGreaterThan(0);
  expect(selectionRects.some((background) => background !== "transparent"
    && background !== "rgba(0, 0, 0, 0)"
    && background !== "rgb(0, 0, 0)")).toBe(true);
});

test("assistant context pack stays exact, compact, and accessible", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "desktop" && !testInfo.project.name.startsWith("mobile-"), "Covered by one desktop and the permanent mobile browser projects.");
  await openWorkspace(page, "/project", "Scratch Project");
  const heading = page.getByRole("heading", { name: "Scratch Project", exact: true });
  await heading.evaluate((element) => {
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

  const pack = page.getByRole("region", { name: "Selected context pack" });
  await expect(pack).toBeVisible();
  await expect(pack).toContainText("Project selection");
  await expect(pack).not.toContainText("Scratch Project", { useInnerText: true });
  await pack.getByRole("button", { name: "Expand Project selection" }).click();
  await expect(pack.getByText("Scratch Project", { exact: true })).toBeVisible();
  const geometry = await pack.evaluate((element) => {
    const bounds = element.getBoundingClientRect();
    return {
      left: bounds.left,
      right: bounds.right,
      viewportWidth: innerWidth,
      scrollWidth: element.scrollWidth,
      clientWidth: element.clientWidth,
    };
  });
  expect(geometry.left).toBeGreaterThanOrEqual(0);
  expect(geometry.right).toBeLessThanOrEqual(geometry.viewportWidth + 1);
  expect(geometry.scrollWidth).toBeLessThanOrEqual(geometry.clientWidth + 1);
  const accessibility = await new AxeBuilder({ page }).include(".chat-context-pack").analyze();
  expect(accessibility.violations).toEqual([]);
});

test("hidden terminal views stop emitting resize frames", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name === "narrow", "The persistent terminal is intentionally unavailable in the mobile companion.");
  await openWorkspace(page, "/", "Workbench");
  await expect.poll(() => page.evaluate(() => (
    (globalThis as typeof globalThis & { __terminalFrames?: Array<{ type?: string }> }).__terminalFrames
      ?.filter((frame) => frame.type === "resize").length ?? 0
  ))).toBeGreaterThan(0);

  await page.getByRole("tab", { name: "Workspace code editor", exact: true }).click();
  await expect(page.locator(".persistent-terminal")).toBeHidden();
  await page.waitForTimeout(50);
  const before = await page.evaluate(() => (
    (globalThis as typeof globalThis & { __terminalFrames?: Array<{ type?: string }> }).__terminalFrames
      ?.filter((frame) => frame.type === "resize") ?? []
  ));
  await page.setViewportSize({ width: 1320, height: 820 });
  await page.waitForTimeout(100);
  const after = await page.evaluate(() => (
    (globalThis as typeof globalThis & { __terminalFrames?: Array<{ columns?: number; rows?: number; type?: string }> }).__terminalFrames
      ?.filter((frame) => frame.type === "resize") ?? []
  ));

  expect(after).toHaveLength(before.length);
  expect(after.every((frame) => (
    Number.isInteger(frame.columns)
    && Number.isInteger(frame.rows)
    && frame.columns! >= 1
    && frame.columns! <= 1_000
    && frame.rows! >= 1
    && frame.rows! <= 1_000
  ))).toBe(true);
});

test(firstRunThemeTest, async ({ page }) => {
  await openWorkspace(page, "/", "Workbench");
  await expect(page.locator("html")).toHaveAttribute("data-theme", "zero-dark");
  await expect(page.getByRole("region", { name: "Zero Layer context" })).toHaveCount(0);
  expect(await page.evaluate(() => localStorage.getItem("nebula.theme"))).toBeNull();
});

test("Workbench omits the removed Human controlled badge in every theme", async ({ page }) => {
  await openWorkspace(page, "/", "Workbench");
  for (const theme of ["zero-dark", "zero-light"] as const) {
    await setTheme(page, theme);
    await expect(page.getByText("Human controlled", { exact: true })).toHaveCount(0);
    await expect(page.locator('[title^="Human controlled"]')).toHaveCount(0);
  }
});

test("Zero keeps one navigable panoramic shell with a clear light-theme status and search cluster", async ({ page }) => {
  await openWorkspace(page, "/", "Workbench");
  await setTheme(page, "zero-light");

  const ready = page.getByRole("button", { name: "Nebula Core ready" });
  const search = page.getByRole("button", { name: "Search commands" });
  await expect(ready).toBeVisible();
  await expect(search).toBeVisible();

  const contrast = await page.locator(".zero-status-band").evaluate((band) => {
    const parse = (value: string) => {
      const channels = value.match(/[\d.]+/g)?.map(Number);
      if (!channels || channels.length !== 3) throw new Error(`Expected an opaque RGB color, received ${value}`);
      return channels;
    };
    const luminance = (value: string) => {
      const channels = parse(value).map((channel) => {
        const normalized = channel / 255;
        return normalized <= 0.04045 ? normalized / 12.92 : ((normalized + 0.055) / 1.055) ** 2.4;
      });
      return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2];
    };
    const ratio = (foreground: string, background: string) => {
      const lighter = Math.max(luminance(foreground), luminance(background));
      const darker = Math.min(luminance(foreground), luminance(background));
      return (lighter + 0.05) / (darker + 0.05);
    };
    const measure = (selector: string) => {
      const element = band.querySelector<HTMLElement>(selector)!;
      const style = getComputedStyle(element);
      return {
        background: style.backgroundColor,
        foreground: style.color,
        ratio: ratio(style.color, style.backgroundColor),
      };
    };
    return {
      ready: measure(".connection-chip"),
      search: measure(".command-trigger"),
      shortcut: measure(".command-trigger kbd"),
    };
  });

  expect(contrast.ready.ratio, JSON.stringify(contrast.ready)).toBeGreaterThanOrEqual(4.5);
  expect(contrast.search.ratio, JSON.stringify(contrast.search)).toBeGreaterThanOrEqual(4.5);
  expect(contrast.shortcut.ratio, JSON.stringify(contrast.shortcut)).toBeGreaterThanOrEqual(4.5);
  expect(contrast.ready.background).not.toBe(contrast.search.background);
});

test("primary navigation exposes only the five task destinations", async ({ page }) => {
  await openWorkspace(page, "/", "Workbench");
  if ((page.viewportSize()?.width ?? 1440) <= 760) {
    await page.getByRole("button", { name: "Show sidebar" }).click();
  }
  const navigation = page.getByRole("complementary", { name: "Primary navigation" });
  for (const label of ["Workbench", "Findings", "Reports", "Project", "Settings"]) {
    await expect(navigation.getByRole("link", { name: label, exact: true })).toBeVisible();
  }
  for (const stale of ["Sessions", "Missions", "Assets", "Evidence", "Knowledge"]) {
    await expect(navigation.getByRole("link", { name: stale, exact: true })).toHaveCount(0);
  }
});

test("Missions explains missing runtime setup and provides a working next action", async ({ page }) => {
  await openWorkspace(page, "/?view=missions", "Workbench");

  const controls = page.getByRole("region", { name: "Mission controls" });
  await expect(controls.getByText("Missions need an enabled model provider or agent harness with a verified model.")).toBeVisible();
  await expect(controls.getByRole("link", { name: "Configure runtime" })).toHaveAttribute("href", "/settings#models-settings");
  await expect(controls.getByRole("button", { name: "Automate task" })).toBeDisabled();
  const widths = await page.locator(".session-layout.missions").evaluate((layout) => {
    const workspace = layout.querySelector<HTMLElement>(".session-workspace")!.getBoundingClientRect();
    const missions = layout.querySelector<HTMLElement>(".agents-page")!.getBoundingClientRect();
    return { layout: layout.getBoundingClientRect().width, workspace: workspace.width, missions: missions.width };
  });
  expect(widths.workspace).toBeGreaterThanOrEqual(widths.layout - 2);
  expect(widths.missions).toBeGreaterThanOrEqual(widths.workspace - 26);
});

test("mission workflow freezes harness options, stages, and URL identity", async ({ page }) => {
  const harness = {
    ...entity,
    id: "harness-mission",
    name: "Codex harness",
    kind: "codex_app_server",
    connection_mode: "spawn",
    transport: "stdio",
    executable: "codex",
    endpoint: null,
    auth_mode: "existing_session",
    secret_ref: null,
    default_model: "gpt-5.6-sol",
    enabled: true,
    privacy: { local_only: true, permits_sensitive_data: true },
    native_capabilities: { workspace_access: "write", shell: true, skills: true, subagents: true },
    capabilities: {
      models: ["gpt-5.6-sol"],
      model_options: [{
        model: "gpt-5.6-sol",
        reasoning_efforts: [{ id: "high", label: "High", description: "Thorough review." }],
        default_reasoning_effort: "high",
        service_tiers: [{ id: "priority", label: "Priority", description: "Faster execution." }],
        default_service_tier: "priority",
      }],
      checked_at: entity.updated_at,
      harness_version: "0.149.0",
    },
  };
  let submitted: Record<string, unknown> | undefined;
  let runs: unknown[] = [];
  await page.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path.endsWith("/harnesses") && request.method() === "GET") {
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify([harness]) });
      return;
    }
    if (path.endsWith("/harness-sessions") && request.method() === "GET") {
      await route.fulfill({ status: 200, contentType: "application/json", body: "[]" });
      return;
    }
    if (path.endsWith("/runs") && request.method() === "GET") {
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(runs) });
      return;
    }
    if (path.endsWith("/missions") && request.method() === "POST") {
      submitted = request.postDataJSON() as Record<string, unknown>;
      const created = {
        ...entity,
        id: "mission-url-authority",
        engagement_id: "scratch-project",
        objective: "Review the bounded project",
        status: "queued",
        backend: "harness",
        supervisor_provider_id: null,
        supervisor_model: "gpt-5.6-sol",
        harness_profile_id: "harness-mission",
        harness_session_id: "mission-session",
        budget: {
          max_duration_seconds: null,
          max_tokens: null,
          max_cost_usd: null,
          max_tool_calls: null,
          max_artifact_queries: null,
          max_concurrency: 1,
          max_delegation_depth: 0,
          max_retries_per_task: 0,
        },
        runtime_snapshot: { runtime_options: { reasoning_effort: "high", service_tier: "priority" } },
        metadata: {
          name: "Staged security review",
          stages: [{ title: "Verify", objective: "Verify the strongest observation" }],
        },
        started_at: null,
        completed_at: null,
      };
      runs = [created];
      await route.fulfill({ status: 202, contentType: "application/json", body: JSON.stringify(created) });
      return;
    }
    await route.fallback();
  });

  await openWorkspace(page, "/?view=missions", "Workbench");
  await page.getByRole("region", { name: "Mission controls" }).getByRole("button", { name: "Automate task" }).click();
  const dialog = page.getByRole("dialog", { name: "Automate task" });
  await dialog.getByLabel("Mission name").fill("Staged security review");
  await dialog.getByLabel("Objective", { exact: true }).first().fill("Review the bounded project");
  await dialog.getByText("Advanced", { exact: true }).click();
  await expect(dialog.getByRole("combobox", { name: "Mission runtime" })).toHaveValue("harness");
  await expect(dialog.getByRole("combobox", { name: "Mission harness effort" })).toHaveValue("high");
  await expect(dialog.getByRole("combobox", { name: "Mission harness speed" })).toHaveValue("priority");
  await expect(dialog.getByLabel("Duration (minutes)")).toHaveValue("");
  await expect(dialog.getByLabel("Token limit")).toHaveValue("");
  await expect(dialog.getByLabel("Cost limit (USD)")).toHaveValue("");
  await expect(dialog.getByLabel("Maximum execution calls")).toHaveValue("");
  await dialog.getByRole("button", { name: "Add stage" }).click();
  await dialog.getByRole("group", { name: "Stage 1" }).getByLabel("Name").fill("Verify");
  await dialog.getByRole("group", { name: "Stage 1" }).getByLabel("Objective").fill("Verify the strongest observation");
  await dialog.getByRole("button", { name: "Automate task" }).click();

  await expect.poll(() => submitted).toMatchObject({
    backend: "harness",
    harness_profile_id: "harness-mission",
    model: "gpt-5.6-sol",
    harness_reasoning_effort: "high",
    harness_service_tier: "priority",
    stages: [{ title: "Verify", objective: "Verify the strongest observation" }],
  });
  expect(submitted).not.toHaveProperty("max_duration_seconds");
  expect(submitted).not.toHaveProperty("max_tokens");
  expect(submitted).not.toHaveProperty("max_cost_usd");
  expect(submitted).not.toHaveProperty("max_tool_calls");
  await expect.poll(() => new URL(page.url()).searchParams.get("mission")).toBe("mission-url-authority");
  await expect(page.getByRole("navigation", { name: "Mission history" }).getByText("Staged security review")).toBeVisible();
  const missionLedger = page.getByRole("region", { name: "Mission activity" });
  await expect(missionLedger.getByRole("list", { name: "Work phases" }).getByText("Verify")).toBeVisible();
  await expect(missionLedger).not.toContainText("item upsert");
  const accessibility = await new AxeBuilder({ page }).include(".agents-page").analyze();
  expect(accessibility.violations).toEqual([]);
});

test("Diagnostics explains and focuses a requested failure at every breakpoint", async ({ page }) => {
  await openWorkspace(page, "/settings?diagnostic=err_preview_123#diagnostics-settings", "Settings");
  await expect(page.getByRole("heading", { name: "Diagnostics", exact: true })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Current status" })).toBeVisible();
  await expect(page.getByText("Core is responding")).toBeVisible();
  await expect(page.getByText("The configured model provider stopped the stream.")).toBeVisible();
  await expect(page.getByText("Review the technical evidence and correlation identifiers in this incident.")).toBeVisible();
  await expect(page.getByText("Showing requested failure")).toBeVisible();
  await expect(page.locator(".diagnostic-failure-card.targeted")).toBeFocused();
  await expect(page.locator(".diagnostic-technical-details dd", { hasText: "err_preview_123" })).toBeVisible();
  expect(await page.locator(".diagnostics-panel").evaluate((element) => element.scrollWidth <= element.clientWidth + 1)).toBe(true);
  const accessibility = await new AxeBuilder({ page }).include(".diagnostics-panel").analyze();
  expect(accessibility.violations).toEqual([]);
});

test("paired-device settings explain host-only pairing and use a compact dialog", async ({ page }) => {
  await openWorkspace(page, "/settings#identity-security-settings", "Settings");
  await expect(page.getByText("Pair new devices from the Nebula host")).toBeVisible();
  await expect(page.getByRole("button", { name: "Pair device" })).toHaveCount(0);

  // Change the document URL, not only its fragment, so runtime resolution
  // consumes the host token in a fresh browser document.
  await page.goto("/settings?host-runtime=1#token=preview-host-token");
  await expect(page.getByRole("heading", { name: "Settings", exact: true })).toBeVisible({ timeout: 15_000 });
  await openWorkspace(page, "/settings#identity-security-settings", "Settings");
  const opener = page.getByRole("button", { name: "Pair device" });
  await opener.click();
  const dialog = page.getByRole("dialog", { name: "Pair a device" });
  await expect(dialog).toBeVisible();
  await expect.poll(() => dialog.evaluate((element) => element.contains(document.activeElement))).toBe(true);
  const bounds = await dialog.boundingBox();
  expect(bounds).toBeTruthy();
  expect(bounds!.x).toBeGreaterThanOrEqual(0);
  expect(bounds!.y).toBeGreaterThanOrEqual(0);
  expect(bounds!.x + bounds!.width).toBeLessThanOrEqual((page.viewportSize()?.width ?? 0) + 1);
  expect(bounds!.y + bounds!.height).toBeLessThanOrEqual((page.viewportSize()?.height ?? 0) + 1);
  const accessibility = await new AxeBuilder({ page }).include(".device-pairing-dialog").analyze();
  expect(accessibility.violations).toEqual([]);
  await page.keyboard.press("Escape");
  await expect(dialog).toBeHidden();
  await expect(opener).toBeFocused();
});

test("critical workspaces remain visually stable", async ({ page }, testInfo) => {
  test.setTimeout(90_000);
  for (const [name, route, heading] of workspaces) {
    await openWorkspace(page, route, heading);
    await expect(page).toHaveScreenshot(`${name}-${testInfo.project.name}.png`, { fullPage: true });
  }
});

test("all task workspaces keep responsive content inside its owning surface", async ({ page }) => {
  test.setTimeout(60_000);
  for (const [, route, heading] of workspaces) {
    await openWorkspace(page, route, heading);
    const overflow = await page.locator("body").evaluate(() => {
      const selector = [
        ".page",
        ".metric-grid",
        ".metric-card",
        ".session-toolbar",
        ".session-workspace",
        ".project-tabs",
        ".settings-tabs",
        ".finding-summary-grid",
        ".summary-strip",
        ".data-toolbar",
        ".callout",
        ".overview-grid",
      ].join(", ");
      return [...document.querySelectorAll<HTMLElement>(selector)]
        .filter((element) => {
          const rect = element.getBoundingClientRect();
          const style = getComputedStyle(element);
          return rect.width > 0
            && rect.height > 0
            && style.display !== "none"
            && element.scrollWidth > element.clientWidth + 2;
        })
        .map((element) => `${element.tagName.toLowerCase()}.${element.className}: ${element.clientWidth}/${element.scrollWidth}`);
    });
    expect(overflow, `${route} contains horizontally clipped UI`).toEqual([]);
    expect(await findPathologicalText(page), `${route} renders prose in a pathologically narrow column`).toEqual([]);
  }
});

test("all assistant states remain fully visible inside mobile Workbench navigation and desktop viewports", async ({ page }, testInfo) => {
  if (testInfo.project.name === "desktop") await page.setViewportSize({ width: 2048, height: 868 });
  await page.addInitScript(() => localStorage.setItem("nebula.theme", "zero-dark"));
  await openWorkspace(page, "/?view=chat", "Workbench");

  if ((page.viewportSize()?.width ?? 1_000) <= 760) await page.getByRole("button", { name: "Open conversations" }).click();
  else await page.getByRole("button", { name: "Show conversations" }).click();
  const conversationRow = page.locator(".session-list nav > button").first();
  await expect(conversationRow).toBeVisible();
  expect(await conversationRow.evaluate((element) => element.getBoundingClientRect().height)).toBeLessThanOrEqual(50);
  if ((page.viewportSize()?.width ?? 1_000) <= 760) await page.getByRole("button", { name: "Chat", exact: true }).click();

  const workspace = page.locator(".session-layout.chat .session-workspace");
  const emptyState = page.locator(".chat-empty-state");
  const startChat = emptyState.getByRole("button", { name: "Start new chat" });
  await expect(emptyState).toBeVisible();
  await expect(startChat).toBeVisible();

  const emptyBounds = await workspace.evaluate((element) => {
    const workspaceRect = element.getBoundingClientRect();
    const emptyRect = element.querySelector<HTMLElement>(".chat-empty-state")!.getBoundingClientRect();
    const buttonRect = element.querySelector<HTMLElement>(".chat-empty-state .button")!.getBoundingClientRect();
    return {
      workspaceBottom: workspaceRect.bottom,
      emptyBottom: emptyRect.bottom,
      buttonBottom: buttonRect.bottom,
      clientHeight: element.clientHeight,
      scrollHeight: element.scrollHeight,
    };
  });
  expect(emptyBounds.emptyBottom).toBeLessThanOrEqual(emptyBounds.workspaceBottom + 1);
  expect(emptyBounds.buttonBottom).toBeLessThanOrEqual(emptyBounds.workspaceBottom + 1);
  expect(emptyBounds.scrollHeight).toBeLessThanOrEqual(emptyBounds.clientHeight + 1);

  await startChat.click();
  const composer = page.locator(".chat-composer");
  await expect(composer).toBeVisible();
  await page.getByRole("button", { name: "Assistant settings" }).click();
  const settings = page.getByRole("dialog", { name: "Assistant settings" });
  await expect(settings).toBeVisible();
  const settingsGeometry = await settings.evaluate((popover) => {
    const panel = popover.closest<HTMLElement>(".chat-panel")!.getBoundingClientRect();
    const composer = popover.parentElement!.querySelector<HTMLElement>(".chat-composer")!.getBoundingClientRect();
    const bounds = popover.getBoundingClientRect();
    return {
      centerDelta: Math.abs((bounds.left + bounds.right) / 2 - (composer.left + composer.right) / 2),
      verticalGap: composer.top - bounds.bottom,
      leftInset: bounds.left - panel.left,
      rightInset: panel.right - bounds.right,
    };
  });
  expect(settingsGeometry.centerDelta).toBeLessThanOrEqual(1);
  expect(settingsGeometry.verticalGap).toBeGreaterThanOrEqual(5);
  expect(settingsGeometry.leftInset).toBeGreaterThanOrEqual(5);
  expect(settingsGeometry.rightInset).toBeGreaterThanOrEqual(5);
  await page.getByRole("button", { name: "Close assistant settings" }).click();
  const messageInput = page.locator("#analyst-message");
  const collapsedHeight = await messageInput.evaluate((element) => element.getBoundingClientRect().height);
  expect(collapsedHeight).toBeLessThanOrEqual(48);

  await messageInput.evaluate((element) => element.removeAttribute("disabled"));
  await messageInput.fill(Array.from({ length: 12 }, (_, index) => `Line ${index + 1}`).join("\n"));
  await expect.poll(() => messageInput.evaluate((element) => element.getBoundingClientRect().height)).toBeGreaterThan(collapsedHeight);
  const expandedInput = await messageInput.evaluate((element) => ({
    height: element.getBoundingClientRect().height,
    overflowY: getComputedStyle(element).overflowY,
  }));
  expect(expandedInput.height).toBeLessThanOrEqual(160);
  expect(expandedInput.overflowY).toBe("auto");

  await messageInput.evaluate((element) => element.removeAttribute("disabled"));
  await messageInput.fill("");
  await expect.poll(() => messageInput.evaluate((element) => element.getBoundingClientRect().height)).toBeLessThanOrEqual(collapsedHeight + 1);

  const composerBounds = await workspace.evaluate((element) => {
    const workspaceRect = element.getBoundingClientRect();
    const panel = element.querySelector<HTMLElement>(".chat-panel")!;
    const scroll = element.querySelector<HTMLElement>(".chat-scroll")!;
    const composer = element.querySelector<HTMLElement>(".chat-composer")!;
    const panelRect = panel.getBoundingClientRect();
    const scrollRect = scroll.getBoundingClientRect();
    const composerRect = composer.getBoundingClientRect();
    return {
      workspaceTop: workspaceRect.top,
      workspaceBottom: workspaceRect.bottom,
      panelTop: panelRect.top,
      panelBottom: panelRect.bottom,
      panelClientHeight: panel.clientHeight,
      panelScrollHeight: panel.scrollHeight,
      scrollHeight: scrollRect.height,
      composerTop: composerRect.top,
      composerBottom: composerRect.bottom,
      viewportHeight: window.innerHeight,
      clientHeight: element.clientHeight,
      workspaceScrollHeight: element.scrollHeight,
    };
  });
  const geometry = JSON.stringify(composerBounds);
  expect(composerBounds.composerTop, geometry).toBeGreaterThanOrEqual(composerBounds.workspaceTop - 1);
  expect(composerBounds.composerBottom, geometry).toBeLessThanOrEqual(composerBounds.workspaceBottom + 1);
  expect(composerBounds.composerBottom).toBeLessThanOrEqual(composerBounds.viewportHeight + 1);
  expect(composerBounds.workspaceScrollHeight).toBeLessThanOrEqual(composerBounds.clientHeight + 1);
});

test("conversation pane defaults closed and restores its device preference without changing URL identity", async ({ page }) => {
  test.skip((page.viewportSize()?.width ?? 1440) <= 760, "Desktop conversation-pane contract");
  await openWorkspace(page, "/?view=chat", "Workbench");

  await expect(page.getByRole("complementary", { name: "Conversations" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Show conversations" })).toHaveAttribute("aria-expanded", "false");
  expect(await page.evaluate(() => localStorage.getItem("nebula.conversations.open"))).toBeNull();

  await page.getByRole("button", { name: "Show conversations" }).click();
  const conversations = page.getByRole("complementary", { name: "Conversations" });
  await expect(conversations).toBeVisible();
  expect(await conversations.evaluate((element) => Math.round(element.getBoundingClientRect().width))).toBe(280);
  expect(await page.evaluate(() => localStorage.getItem("nebula.conversations.open"))).toBe("true");
  await expect(page).toHaveURL(/\?view=chat$/);

  await page.reload();
  await expect(conversations).toBeVisible();
  await expect(page.getByRole("button", { name: "Hide conversations" }).first()).toHaveAttribute("aria-expanded", "true");
  await expect(page.getByText("0 tokens", { exact: true })).toHaveCount(0);

  await page.getByRole("button", { name: "Hide conversations" }).last().click();
  await expect(conversations).toHaveCount(0);
  expect(await page.evaluate(() => localStorage.getItem("nebula.conversations.open"))).toBe("false");
  await expect(page).toHaveURL(/\?view=chat$/);
});

test("conversation More actions remain usable on mobile Workbench navigation", async ({ page }) => {
  await page.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path.endsWith("/chat-sessions") && request.method() === "GET") {
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify([{
        ...entity,
        id: "conversation-actions-test",
        engagement_id: "scratch-project",
        title: "Conversation action review",
        backend: "provider",
        provider_profile_id: null,
        harness_profile_id: null,
        harness_session_id: null,
        model: null,
        metadata: {},
      }]) });
      return;
    }
    await route.fallback();
  });

  await openWorkspace(page, "/?view=chat", "Workbench");
  if ((page.viewportSize()?.width ?? 1_000) <= 760) {
    await page.getByRole("button", { name: "Open conversations", exact: true }).click();
  } else await page.getByRole("button", { name: "Show conversations" }).click();
  const row = page.locator(".session-list-item").filter({ hasText: "Conversation action review" });
  const trigger = row.getByRole("button", { name: "More actions for Conversation action review" });
  await row.hover();
  await expect(trigger).toHaveCSS("opacity", "1");

  await trigger.click();
  const menu = page.getByRole("menu", { name: "Actions for Conversation action review" });
  await expect(menu).toBeVisible();
  await expect(menu.getByRole("menuitem", { name: "Copy link" })).toBeFocused();
  await page.keyboard.press("ArrowDown");
  await expect(menu.getByRole("menuitem", { name: "Export transcript" })).toBeFocused();
  await page.keyboard.press("End");
  await expect(menu.getByRole("menuitem", { name: "Delete" })).toBeFocused();
  await page.keyboard.press("ArrowUp");
  await expect(menu.getByRole("menuitem", { name: "Rename" })).toBeFocused();
  await page.keyboard.press("Escape");
  await expect(menu).toHaveCount(0);
  await expect(trigger).toBeFocused();

  await trigger.click();
  await page.getByRole("menuitem", { name: "Rename" }).click();
  await expect(page.getByRole("textbox", { name: "Rename conversation Conversation action review" })).toBeVisible();
  await page.keyboard.press("Escape");

  await trigger.click();
  await page.getByRole("menuitem", { name: "Delete" }).click();
  const confirmation = page.getByRole("dialog", { name: "Delete Conversation action review?" });
  await expect(confirmation).toBeVisible();
  await confirmation.getByRole("button", { name: "Cancel" }).click();

  if ((page.viewportSize()?.width ?? 1_000) <= 760) {
    const triggerBox = await trigger.boundingBox();
    expect(triggerBox?.width).toBeGreaterThanOrEqual(44);
    expect(triggerBox?.height).toBeGreaterThanOrEqual(44);
  }
  expect(await page.locator("body").evaluate((body) => body.scrollWidth - body.clientWidth)).toBeLessThanOrEqual(1);
});

test("the 320px mobile companion keeps controls visible and the composer above navigation", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "desktop", "One browser run covers the explicit 320px boundary.");
  await page.setViewportSize({ width: 320, height: 700 });
  await page.addInitScript(() => localStorage.setItem("nebula.theme", "zero-dark"));
  await openWorkspace(page, "/?view=chat", "Workbench");
  await page.getByRole("button", { name: "Start new chat", exact: true }).click();

  const geometry = await page.locator(".sessions-page").evaluate((element) => {
    const toolbar = element.querySelector<HTMLElement>(".session-toolbar")!.getBoundingClientRect();
    const composer = element.querySelector<HTMLElement>(".chat-composer")!.getBoundingClientRect();
    const navigation = element.querySelector<HTMLElement>(".mobile-companion-nav")!.getBoundingClientRect();
    const composerInput = element.querySelector<HTMLTextAreaElement>(".chat-composer textarea")!;
    const duplicateDock = document.querySelector<HTMLElement>(".side-nav.zero-anchor-dock");
    const navigationIcons = [...element.querySelectorAll<SVGElement>(".mobile-companion-nav svg")]
      .map((icon) => icon.getBoundingClientRect())
      .map(({ width, height }) => ({ width, height }));
    return {
      composerBottom: composer.bottom,
      navigationTop: navigation.top,
      toolbarHeight: toolbar.height,
      pageWidth: element.scrollWidth,
      viewportWidth: window.innerWidth,
      duplicateDockDisplay: duplicateDock ? getComputedStyle(duplicateDock).display : "missing",
      composerFontSize: Number.parseFloat(getComputedStyle(composerInput).fontSize),
      navigationIcons,
    };
  });
  expect(geometry.composerBottom).toBeLessThanOrEqual(geometry.navigationTop - 1);
  expect(geometry.toolbarHeight).toBeLessThanOrEqual(58);
  expect(geometry.pageWidth).toBeLessThanOrEqual(geometry.viewportWidth);
  expect(geometry.duplicateDockDisplay).toBe("none");
  expect(geometry.composerFontSize).toBeGreaterThanOrEqual(16);
  expect(geometry.navigationIcons).toHaveLength(4);
  expect(geometry.navigationIcons.every(({ width, height }) => width >= 18 && height >= 18)).toBe(true);

  await page.getByRole("button", { name: "More workbench views" }).click();
  const focusAction = page.getByRole("dialog", { name: "More views" }).getByRole("button", { name: "Enter focus mode" });
  await expect(focusAction).toBeVisible();
  await focusAction.click();
  const fullScreenGeometry = await page.locator(".sessions-page.full-screen").evaluate((element) => {
    const bounds = element.getBoundingClientRect();
    return {
      top: bounds.top,
      right: bounds.right,
      bottom: bounds.bottom,
      left: bounds.left,
      navigationDisplay: getComputedStyle(element.querySelector<HTMLElement>(".mobile-companion-nav")!).display,
      viewportWidth: window.innerWidth,
      viewportHeight: window.innerHeight,
    };
  });
  expect(fullScreenGeometry).toEqual({
    top: 0,
    right: 320,
    bottom: 700,
    left: 0,
    navigationDisplay: "none",
    viewportWidth: 320,
    viewportHeight: 700,
  });
  await page.getByRole("button", { name: "Exit full screen workbench" }).click();

  await page.getByRole("button", { name: "More workbench views" }).click();
  const more = page.getByRole("dialog", { name: "More views" });
  await expect(more).toBeVisible();
  for (const label of ["Files", "Notes", "Missions", "Terminal", "Code", "Browser"]) {
    await expect(more.getByRole("button", { name: new RegExp(`^${label}`) })).toBeVisible();
  }
  const accessibility = await new AxeBuilder({ page }).include(".mobile-more-sheet").analyze();
  expect(accessibility.violations).toEqual([]);
  await more.getByRole("button", { name: /^Files/ }).click();
  await expect(page.getByRole("button", { name: "More workbench views" })).toBeVisible();
});

test("host folder picker remains usable as a bounded project workflow", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "desktop" && !testInfo.project.name.startsWith("mobile-"), "Covered by the permanent desktop and mobile browser projects.");
  await page.addInitScript(() => localStorage.setItem("nebula.theme", "zero-dark"));
  await openWorkspace(page, "/settings", "Settings");
  if (testInfo.project.name.startsWith("mobile-")) {
    await page.getByRole("button", { name: "Show sidebar" }).click();
  }
  await page.getByRole("button", { name: "Switch project" }).click();
  const switcher = page.getByRole("dialog", { name: "Project switcher" });
  await switcher.getByRole("button", { name: "New project" }).click();
  await switcher.getByLabel("Name", { exact: true }).fill("Folder picker acceptance");
  await switcher.getByRole("button", { name: "Browse folders" }).click();

  const dialog = page.getByRole("dialog", { name: "Choose project folder" });
  await expect(dialog).toBeVisible();
  await expect(dialog.getByText("Nebula host")).toBeVisible();
  await expect(dialog.getByText("/home/agent", { exact: true })).toBeVisible();
  const geometry = await dialog.evaluate((element) => {
    const bounds = element.getBoundingClientRect();
    const footerButtons = [...element.querySelectorAll<HTMLElement>("footer button")].map((button) => button.getBoundingClientRect());
    return {
      left: bounds.left,
      right: bounds.right,
      top: bounds.top,
      bottom: bounds.bottom,
      scrollWidth: element.scrollWidth,
      clientWidth: element.clientWidth,
      viewportWidth: window.innerWidth,
      viewportHeight: window.innerHeight,
      footerButtons: footerButtons.map(({ height, left, right }) => ({ height, left, right })),
    };
  });
  expect(geometry.left).toBeGreaterThanOrEqual(0);
  expect(geometry.top).toBeGreaterThanOrEqual(0);
  expect(geometry.right).toBeLessThanOrEqual(geometry.viewportWidth + 1);
  expect(geometry.bottom).toBeLessThanOrEqual(geometry.viewportHeight + 1);
  expect(geometry.scrollWidth).toBeLessThanOrEqual(geometry.clientWidth + 1);
  const minimumActionHeight = testInfo.project.name.startsWith("mobile-") ? 44 : 32;
  expect(
    geometry.footerButtons.every(({ height, left, right }) => height >= minimumActionHeight && left >= 0 && right <= geometry.viewportWidth + 1),
    JSON.stringify(geometry),
  ).toBe(true);

  await dialog.getByRole("button", { name: "a-very-long-project-folder-name-that-must-not-expand-the-dialog" }).click();
  await expect(dialog.getByText("/home/agent/a-very-long-project-folder-name-that-must-not-expand-the-dialog", { exact: true })).toBeVisible();
  await dialog.getByRole("button", { name: "Select folder" }).click();
  await expect(dialog).toHaveCount(0);
  await expect(switcher.getByLabel("Project folder", { exact: true })).toHaveValue("/home/agent/a-very-long-project-folder-name-that-must-not-expand-the-dialog");
  await expect(switcher.getByRole("button", { name: "Browse folders" })).toBeFocused();

  await switcher.getByRole("button", { name: "Browse folders" }).click();
  await expect(page.getByRole("dialog", { name: "Choose project folder" }).getByText("/home/agent/a-very-long-project-folder-name-that-must-not-expand-the-dialog", { exact: true })).toBeVisible();
  if (testInfo.project.name.startsWith("mobile-")) {
    await page.getByRole("button", { name: "Close folder browser" }).click();
  } else {
    await page.keyboard.press("Escape");
  }
  await expect(page.getByRole("dialog", { name: "Choose project folder" })).toHaveCount(0);

  const accessibility = await new AxeBuilder({ page }).include(".engagement-menu").analyze();
  expect(accessibility.violations).toEqual([]);
});

test("project scope normalizes root URLs and confirms all-target mode", async ({ page }) => {
  let durableScope = {
    ...entity,
    id: "scope-scratch",
    engagement_id: "scratch-project",
    allowed_cidrs: [] as string[],
    allowed_domains: ["example.com"],
    allowed_urls: [] as string[],
    allowed_ports: [443],
    allow_all_targets: false,
    not_before: null,
    not_after: null,
    prohibited_actions: [] as string[],
    local_only: true,
    max_concurrency: 1,
    grants: [] as unknown[],
    revision: 3,
  };
  await page.context().route("**/api/v1/engagements/scratch-project/scope", async (route) => {
    if (route.request().method() === "PUT") {
      const request = route.request().postDataJSON() as typeof durableScope & { expected_revision: number };
      expect(request.expected_revision).toBe(durableScope.revision);
      durableScope = {
        ...durableScope,
        ...request,
        allowed_domains: request.allowed_domains.map((value) => new URL(value.includes("://") ? value : `https://${value}`).hostname.toLowerCase()),
        revision: durableScope.revision + 1,
      };
    }
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(durableScope) });
  });

  await openWorkspace(page, "/settings", "Settings");
  await page.getByRole("link", { name: "Advanced settings", exact: true }).click();
  await page.locator("details.settings-group > summary", { hasText: "Project Policy" }).click();
  const domains = page.getByLabel("Allowed domains");
  await domains.fill("https://www.Google.com/");
  const allTargets = page.getByLabel("All targets and ports");
  await allTargets.check();
  await expect(page.getByText("All-targets mode overrides the destination and port allowlists below.")).toBeVisible();
  await expect(domains).toBeDisabled();
  const policyGeometry = await page.locator("#engagement-policy-settings").evaluate((section) => {
    const sectionBounds = section.getBoundingClientRect();
    const panels = [...section.querySelectorAll<HTMLElement>(".policy-form")];
    const offenders = panels.flatMap((panel, panelIndex) => {
      const panelBounds = panel.getBoundingClientRect();
      const visibleSettings = panel.querySelectorAll<HTMLElement>(
        "input, select, textarea, .provider-consent, .provider-consent span, .inline-validation-notice, .inline-validation-notice span, .resource-form-grid, footer",
      );
      return [...visibleSettings].flatMap((setting) => {
        const bounds = setting.getBoundingClientRect();
        return bounds.left < panelBounds.left - 1 || bounds.right > panelBounds.right + 1
          ? [`panel ${panelIndex}: ${setting.tagName.toLowerCase()}.${setting.className}`]
          : [];
      });
    });
    return {
      offenders,
      sectionClientWidth: section.clientWidth,
      sectionScrollWidth: section.scrollWidth,
      sectionLeft: sectionBounds.left,
      sectionRight: sectionBounds.right,
      viewportWidth: window.innerWidth,
    };
  });
  expect(policyGeometry.offenders).toEqual([]);
  expect(policyGeometry.sectionScrollWidth).toBeLessThanOrEqual(policyGeometry.sectionClientWidth + 1);
  expect(policyGeometry.sectionLeft).toBeGreaterThanOrEqual(-1);
  expect(policyGeometry.sectionRight).toBeLessThanOrEqual(policyGeometry.viewportWidth + 1);
  await page.getByRole("button", { name: "Save scope" }).click();
  const confirmation = page.getByRole("dialog", { name: "Allow every network target and port?" });
  await expect(confirmation).toBeVisible();
  const accessibility = await new AxeBuilder({ page }).include(".dialog-backdrop").analyze();
  expect(accessibility.violations).toEqual([]);
  await confirmation.getByRole("button", { name: "Allow all targets" }).click();
  await expect(page.getByRole("status")).toContainText("Network scope updated");
  expect(durableScope.allowed_domains).toEqual(["www.google.com"]);
  expect(durableScope.allow_all_targets).toBe(true);

  await openWorkspace(page, "/", "Workbench");
  await openWorkspace(page, "/settings", "Settings");
  await page.getByRole("link", { name: "Advanced settings", exact: true }).click();
  await page.locator("details.settings-group > summary", { hasText: "Project Policy" }).click();
  await expect(page.getByLabel("All targets and ports")).toBeChecked();
  await expect(page.getByLabel("Allowed domains")).toHaveValue("www.google.com");
  await expect(page.getByLabel("Allowed domains")).toBeDisabled();
});

test("streaming chat follows the bottom without overriding reader scroll intent", async ({ page }, testInfo) => {
  test.skip(!["desktop", "webkit"].includes(testInfo.project.name), "Scroll intent needs one desktop interaction run.");
  const provider = {
    ...entity,
    id: "provider-scroll-test",
    name: "Scroll test provider",
    provider_type: "vllm",
    endpoint: "http://127.0.0.1:8000/v1",
    enabled: true,
    is_local: true,
    secret_ref: null,
    model_allowlist: ["scroll-test-model"],
    capabilities: { streaming: true },
    privacy: { local_only: true, permits_sensitive_data: true },
    metadata: { default_model: "scroll-test-model" },
  };
  await page.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path.endsWith("/providers") && request.method() === "GET") {
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify([provider]) });
      return;
    }
    if (path.endsWith("/chat-sessions") && request.method() === "GET") {
      await route.fulfill({ status: 200, contentType: "application/json", body: "[]" });
      return;
    }
    await route.fallback();
  });
  await page.addInitScript(() => {
    const nativeFetch = globalThis.fetch.bind(globalThis);
    globalThis.fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
      if (!url.endsWith("/chat/completions")) return nativeFetch(input, init);
      const encoder = new TextEncoder();
      const paragraph = "Streaming output keeps extending this response while the analyst reads the transcript. ";
      const deltas = Array.from({ length: 120 }, (_, index) => `${index + 1}. ${paragraph.repeat(3)}\n\n`);
      const content = deltas.join("");
      const stream = new ReadableStream<Uint8Array>({
        start(controller) {
          const frames: unknown[] = [
            { type: "started", provider_id: "provider-scroll-test", model: "scroll-test-model", session_id: "scroll-session", turn_id: "scroll-turn" },
            ...deltas.map((delta) => ({ type: "delta", provider_id: "provider-scroll-test", model: "scroll-test-model", delta })),
            {
              type: "done",
              provider_id: "provider-scroll-test",
              model: "scroll-test-model",
              session_id: "scroll-session",
              turn_id: "scroll-turn",
              message: { id: "scroll-assistant", role: "assistant", content },
              usage: { input_tokens: 4, output_tokens: 1200, total_tokens: 1204 },
              finish_reason: "stop",
              citations: [],
            },
          ];
          let index = 0;
          const timer = globalThis.setInterval(() => {
            const frame = frames[index++];
            if (frame) controller.enqueue(encoder.encode(`data: ${JSON.stringify(frame)}\n\n`));
            if (index >= frames.length) {
              globalThis.clearInterval(timer);
              controller.enqueue(encoder.encode("data: [DONE]\n\n"));
              controller.close();
            }
          }, 20);
        },
      });
      return new Response(stream, { status: 200, headers: { "content-type": "text/event-stream" } });
    };
  });

  await openWorkspace(page, "/?view=chat", "Workbench");
  await page.getByRole("button", { name: "New chat", exact: true }).click();
  const composer = page.getByPlaceholder("Ask about this project…");
  await expect(composer).toBeEnabled();
  await composer.fill("Stream a long response for scroll testing.");
  await page.getByRole("button", { name: "Send message" }).click();
  const chatScroll = page.locator(".chat-scroll");
  await expect.poll(() => chatScroll.evaluate((element) => getComputedStyle(element).overscrollBehaviorY)).toBe("none");
  await expect.poll(() => chatScroll.evaluate((element) => element.scrollHeight - element.clientHeight)).toBeGreaterThan(500);
  await chatScroll.hover();
  let previousTrackpadPosition = await chatScroll.evaluate((element) => element.scrollTop);
  for (let index = 0; index < 8; index += 1) {
    await page.mouse.wheel(0, 180);
    await page.waitForTimeout(25);
    const currentTrackpadPosition = await chatScroll.evaluate((element) => element.scrollTop);
    expect(currentTrackpadPosition).toBeGreaterThanOrEqual(previousTrackpadPosition - 2);
    previousTrackpadPosition = currentTrackpadPosition;
  }
  const distanceFromBottom = () => chatScroll.evaluate((element) => element.scrollHeight - element.scrollTop - element.clientHeight);
  await expect.poll(distanceFromBottom).toBeLessThanOrEqual(2);
  await page.mouse.wheel(0, -500);
  await expect.poll(distanceFromBottom).toBeGreaterThan(100);
  const scrollToLatest = page.getByRole("button", { name: "Scroll to latest message" });
  await expect(scrollToLatest).toBeEnabled();
  await scrollToLatest.click();
  await expect.poll(distanceFromBottom).toBeLessThanOrEqual(2);
  await page.waitForTimeout(300);
  expect(await distanceFromBottom()).toBeLessThanOrEqual(2);

  await expect(page.getByRole("button", { name: "Stop response" })).toHaveCount(0, { timeout: 10_000 });
  await expect.poll(distanceFromBottom).toBeLessThanOrEqual(2);

  await page.mouse.wheel(0, -500);
  await expect.poll(distanceFromBottom).toBeGreaterThan(100);
  const readerPosition = await chatScroll.evaluate((element) => element.scrollTop);
  await page.waitForTimeout(300);
  expect(await chatScroll.evaluate((element) => element.scrollTop)).toBeLessThanOrEqual(readerPosition + 2);
});

test("assistant follow-up queue sends ordered provider messages after the active turn", async ({ page }, testInfo) => {
  test.skip(!["desktop", "narrow", "mobile-chromium-small", "mobile-webkit"].includes(testInfo.project.name), "Covered by the permanent desktop and mobile assistant queue projects.");
  const provider = {
    ...entity,
    id: "provider-follow-up-queue",
    name: "Queue test provider",
    provider_type: "vllm",
    endpoint: "http://127.0.0.1:8000/v1",
    enabled: true,
    is_local: true,
    secret_ref: null,
    model_allowlist: ["queue-test-model"],
    capabilities: { streaming: true },
    privacy: { local_only: true, permits_sensitive_data: true },
    metadata: { default_model: "queue-test-model" },
  };
  await page.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path.endsWith("/providers") && request.method() === "GET") {
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify([provider]) });
      return;
    }
    if (path.endsWith(`/providers/${provider.id}/health`) && request.method() === "POST") {
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ provider_id: provider.id, healthy: true, models: ["queue-test-model"] }) });
      return;
    }
    if (path.endsWith("/chat-sessions") && request.method() === "GET") {
      await route.fulfill({ status: 200, contentType: "application/json", body: "[]" });
      return;
    }
    if (path.includes("/chat/sessions/") && (path.endsWith("/messages") || path.endsWith("/pending-turn"))) {
      await route.fulfill({ status: 200, contentType: "application/json", body: path.endsWith("/pending-turn") ? "null" : "[]" });
      return;
    }
    await route.fallback();
  });
  await page.addInitScript(() => {
    const nativeFetch = globalThis.fetch.bind(globalThis);
    let requestNumber = 0;
    globalThis.fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
      if (!url.endsWith("/chat/completions")) return nativeFetch(input, init);
      const request = JSON.parse(String(init?.body ?? "{}")) as { messages?: Array<{ content?: string }> };
      requestNumber += 1;
      const current = requestNumber;
      const requests = (globalThis as typeof globalThis & { __queueChatRequests?: unknown[] }).__queueChatRequests ??= [];
      requests.push(request);
      const encoder = new TextEncoder();
      const answer = current === 1 ? "The first response is still running." : "The queued follow-up completed.";
      const stream = new ReadableStream<Uint8Array>({
        start(controller) {
          const enqueue = (frame: unknown) => controller.enqueue(encoder.encode(`data: ${JSON.stringify(frame)}\n\n`));
          enqueue({ type: "started", provider_id: "provider-follow-up-queue", model: "queue-test-model", session_id: "queue-session", turn_id: `queue-turn-${current}` });
          enqueue({ type: "delta", provider_id: "provider-follow-up-queue", model: "queue-test-model", delta: answer });
          const finish = () => {
            enqueue({
              type: "done",
              provider_id: "provider-follow-up-queue",
              model: "queue-test-model",
              session_id: "queue-session",
              turn_id: `queue-turn-${current}`,
              message: { id: `queue-assistant-${current}`, role: "assistant", content: answer },
              usage: { input_tokens: 4, output_tokens: 8, total_tokens: 12 },
              finish_reason: "stop",
              citations: [],
            });
            controller.enqueue(encoder.encode("data: [DONE]\n\n"));
            controller.close();
          };
          if (current === 1) globalThis.setTimeout(finish, 6_000);
          else finish();
        },
      });
      return new Response(stream, { status: 200, headers: { "content-type": "text/event-stream" } });
    };
  });

  await openWorkspace(page, "/?view=chat", "Workbench");
  await page.getByRole("button", { name: "New chat", exact: true }).click();
  const composer = page.getByPlaceholder("Ask about this project…");
  await expect(composer).toBeEnabled();
  await composer.fill("Start the first response.");
  await page.getByRole("button", { name: "Send message" }).click();
  await expect(page.getByRole("button", { name: "Stop response" })).toBeVisible();

  const queueComposer = page.getByPlaceholder("Queue the next message while this response finishes…");
  await expect(queueComposer).toBeVisible();
  await queueComposer.fill("First queued follow-up.");
  await queueComposer.press("Enter");
  await queueComposer.fill("Second queued follow-up.");
  await queueComposer.press("Enter");
  await expect(page.getByRole("region", { name: "Queued follow-up messages" })).toContainText("2 messages");
  await expect(page.getByRole("region", { name: "Queued follow-up messages" })).toContainText("First queued follow-up.");
  await expect(page.getByRole("region", { name: "Queued follow-up messages" })).toContainText("Second queued follow-up.");
  const queue = page.getByRole("region", { name: "Queued follow-up messages" });
  const queueGeometry = await queue.evaluate((element) => ({
    scrollWidth: element.scrollWidth,
    clientWidth: element.clientWidth,
    viewportWidth: window.innerWidth,
  }));
  expect(queueGeometry.scrollWidth).toBeLessThanOrEqual(queueGeometry.clientWidth + 1);
  expect(queueGeometry.clientWidth).toBeLessThanOrEqual(queueGeometry.viewportWidth + 1);
  const queueAccessibility = await new AxeBuilder({ page }).include(".chat-follow-up-queue").analyze();
  expect(queueAccessibility.violations).toEqual([]);

  await expect.poll(() => page.evaluate(() => (globalThis as typeof globalThis & { __queueChatRequests?: Array<{ messages?: Array<{ content?: string }> }> }).__queueChatRequests?.length ?? 0), { timeout: 15_000 }).toBe(3);
  const requests = await page.evaluate(() => (globalThis as typeof globalThis & { __queueChatRequests?: Array<{ messages?: Array<{ content?: string }> }> }).__queueChatRequests ?? []);
  expect(requests.map((request) => request.messages?.at(-1)?.content)).toEqual([
    "Start the first response.",
    "First queued follow-up.",
    "Second queued follow-up.",
  ]);
  await expect(page.getByText("The queued follow-up completed.").first()).toBeVisible();
  await expect(page.getByRole("region", { name: "Queued follow-up messages" })).toHaveCount(0);
});

test("an idle resumed harness keeps routine telemetry quiet", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "desktop", "Harness status placement needs one desktop interaction run.");
  const harnessSessionId = "c9745e80-1111-4222-8333-444455556666";
  await page.route("**/api/v1/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith("/harnesses")) {
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify([{
        ...entity,
        id: "harness-ready",
        name: "Codex harness",
        kind: "codex_app_server",
        connection_mode: "spawn",
        transport: "stdio",
        executable: "codex",
        endpoint: null,
        auth_mode: "existing_session",
        secret_ref: null,
        default_model: "gpt-5-codex",
        enabled: true,
        privacy: { local_only: true, permits_sensitive_data: true },
        native_capabilities: { workspace_access: "write", shell: true, web_search: true, skills: true },
        capabilities: { models: ["gpt-5-codex"], checked_at: entity.updated_at, harness_version: "1.0" },
      }]) });
      return;
    }
    if (path.endsWith(`/harness-sessions/${harnessSessionId}/activity`)) {
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({
        session_id: harnessSessionId,
        session_status: "idle",
        busy: false,
        live: true,
        turn_id: null,
        turn_status: null,
        turn_origin: null,
        started_at: null,
        last_activity_at: entity.updated_at,
        detail: "This harness session is ready for another turn.",
      }) });
      return;
    }
    if (path.endsWith("/harness-sessions")) {
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify([{
        ...entity,
        id: harnessSessionId,
        engagement_id: "scratch-project",
        harness_profile_id: "harness-ready",
        model: "gpt-5-codex",
        status: "idle",
        mcp_server_ids: [],
        last_activity_at: entity.updated_at,
      }]) });
      return;
    }
    if (path.endsWith("/chat-sessions")) {
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify([{
        ...entity,
        id: "chat-ready",
        engagement_id: "scratch-project",
        title: "Ready harness conversation",
        backend: "harness",
        harness_profile_id: "harness-ready",
        harness_session_id: harnessSessionId,
        model: "gpt-5-codex",
        metadata: {},
      }]) });
      return;
    }
    if (path.endsWith("/chat/sessions/chat-ready/messages")) {
      await route.fulfill({ status: 200, contentType: "application/json", body: "[]" });
      return;
    }
    if (path.endsWith("/chat/sessions/chat-ready/pending-turn")) {
      await route.fulfill({ status: 200, contentType: "application/json", body: "null" });
      return;
    }
    await route.fallback();
  });

  await openWorkspace(page, "/?view=chat", "Workbench");
  await page.getByRole("button", { name: "Show conversations" }).click();
  await page.locator(".session-select").filter({ hasText: "Ready harness conversation" }).click();
  await expect(page.locator(".chat-composer footer")).toContainText("Codex harness");
  await expect(page.locator(".chat-composer footer")).toContainText("gpt-5-codex");
  await expect(page.locator(".chat-composer footer")).not.toContainText("0 MCP");
  await expect(page.locator(".harness-status-rail")).toHaveCount(0);
  await expect(page.locator(".chat-harness-progress")).toHaveCount(0);
  await page.getByRole("button", { name: "More Workbench actions" }).click();
  await page.getByRole("menuitem", { name: /Show session details/ }).click();
  await expect(page.locator(".session-inspector code").filter({ hasText: harnessSessionId })).toHaveText(harnessSessionId);

  await page.getByRole("button", { name: "New chat", exact: true }).click();
  await expect.poll(() => new URL(page.url()).searchParams.get("session")).toBeNull();
  await expect(page.getByRole("textbox", { name: "Message the analyst assistant" })).toBeVisible();
  await expect(page.locator(".session-list nav > button.active")).toContainText("New conversation");
  await page.getByRole("button", { name: "Assistant settings" }).click();
  await expect(page.getByRole("combobox", { name: "Chat runtime" })).toBeEnabled();
});

test("New chat detaches from an in-flight saved conversation load", async ({ page }) => {
  const savedSessionId = "chat-loading-while-detaching";
  await page.route("**/api/v1/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith("/chat-sessions")) {
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify([{
        ...entity,
        id: savedSessionId,
        engagement_id: "scratch-project",
        title: "Slow saved conversation",
        backend: "provider",
        provider_id: null,
        harness_profile_id: null,
        harness_session_id: null,
        model: null,
        metadata: {},
      }]) });
      return;
    }
    if (path.endsWith(`/chat/sessions/${savedSessionId}/messages`) || path.endsWith(`/chat/sessions/${savedSessionId}/pending-turn`)) {
      await new Promise((resolve) => setTimeout(resolve, 1_000));
      await route.fulfill({ status: 200, contentType: "application/json", body: path.endsWith("/pending-turn") ? "null" : "[]" });
      return;
    }
    await route.fallback();
  });

  await openWorkspace(page, `/?view=chat&session=${savedSessionId}`, "Workbench");
  await page.getByRole("button", { name: "New chat", exact: true }).click();
  await expect.poll(() => new URL(page.url()).searchParams.get("session")).toBeNull();
  await page.waitForTimeout(1_100);
  await expect(page.getByRole("textbox", { name: "Message the analyst assistant" })).toBeVisible();
  if ((page.viewportSize()?.width ?? 1_000) <= 760) {
    await page.getByRole("button", { name: "Open conversations", exact: true }).click();
  } else {
    await page.getByRole("button", { name: "Show conversations" }).click();
  }
  await expect(page.locator(".session-list nav > button.active")).toContainText("New conversation");
  if ((page.viewportSize()?.width ?? 1_000) <= 760) {
    await page.getByRole("button", { name: "Chat", exact: true }).click();
  }
  await page.getByRole("button", { name: "Assistant settings" }).click();
  await expect(page.getByRole("combobox", { name: "Chat runtime" })).toBeEnabled();
  await expect.poll(() => new URL(page.url()).searchParams.get("session")).toBeNull();
});

test("conversation switching commits URL identity before loading and defers saved work details", async ({ page }) => {
  const sourceSessionId = "chat-switch-source";
  const targetSessionId = "chat-switch-target";
  let sourceMessageLoads = 0;
  let targetActivityLoads = 0;
  await page.route("**/api/v1/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith("/chat-sessions")) {
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify([
        {
          ...entity,
          id: targetSessionId,
          engagement_id: "scratch-project",
          title: "Target conversation",
          backend: "harness",
          harness_profile_id: "harness-ready",
          harness_session_id: "harness-session-target",
          model: "gpt-5-codex",
          metadata: {},
        },
        {
          ...entity,
          id: sourceSessionId,
          engagement_id: "scratch-project",
          title: "Source conversation",
          backend: "harness",
          harness_profile_id: "harness-ready",
          harness_session_id: "harness-session-source",
          model: "gpt-5-codex",
          metadata: {},
        },
      ]) });
      return;
    }
    if (path.endsWith(`/chat/sessions/${sourceSessionId}/messages`)) {
      sourceMessageLoads += 1;
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify([{
        ...entity,
        id: "assistant-switch-source",
        engagement_id: "scratch-project",
        session_id: sourceSessionId,
        sequence: 1,
        role: "assistant",
        content: "Source transcript",
        citations: [],
        metadata: { harness_turn_id: "turn-switch-source" },
      }]) });
      return;
    }
    if (path.endsWith(`/chat/sessions/${targetSessionId}/messages`)) {
      await new Promise((resolve) => setTimeout(resolve, 250));
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify([{
        ...entity,
        id: "assistant-switch-target",
        engagement_id: "scratch-project",
        session_id: targetSessionId,
        sequence: 1,
        role: "assistant",
        content: "Target transcript",
        citations: [],
        metadata: { harness_turn_id: "turn-switch-target" },
      }]) });
      return;
    }
    if (path.endsWith("/pending-turn")) {
      await route.fulfill({ status: 200, contentType: "application/json", body: "null" });
      return;
    }
    if (path.includes("/harness-sessions/") && path.endsWith("/activity")) {
      const target = path.includes("harness-session-target");
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({
        session_id: target ? "harness-session-target" : "harness-session-source",
        session_status: "idle",
        busy: false,
        live: true,
        turn_id: null,
        turn_status: null,
        turn_origin: null,
        started_at: null,
        last_activity_at: entity.updated_at,
        detail: "This harness session is idle.",
      }) });
      return;
    }
    if (path.endsWith("/harness-turns/turn-switch-target/events")) {
      targetActivityLoads += 1;
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({
        events: [{
          id: "event-switch-target",
          schema_version: "nebula.harness-activity/v1",
          sequence: 1,
          type: "item_upsert",
          vendor: "codex_app_server",
          harness_session_id: "harness-session-target",
          harness_turn_id: "turn-switch-target",
          item_id: "command-switch-target",
          item_kind: "command",
          item_status: "completed",
          title: "Deferred command",
          artifact_ids: [],
          payload: {},
        }],
        next_sequence: 1,
      }) });
      return;
    }
    if (path.endsWith("/harness-turns/turn-switch-target/interactions")) {
      await route.fulfill({ status: 200, contentType: "application/json", body: "[]" });
      return;
    }
    await route.fallback();
  });

  await openWorkspace(page, `/?view=chat&session=${sourceSessionId}`, "Workbench");
  await expect(page.getByText("Source transcript")).toBeVisible();
  if ((page.viewportSize()?.width ?? 1_000) <= 760) await page.getByRole("button", { name: "Open conversations", exact: true }).click();
  else await page.getByRole("button", { name: "Show conversations" }).click();
  await page.locator(".session-select").filter({ hasText: "Target conversation" }).click();

  await expect.poll(() => new URL(page.url()).searchParams.get("session")).toBe(targetSessionId);
  await expect(page.locator(".session-list-item.active")).toContainText("Target conversation");
  await expect(page.getByText("Target transcript")).toBeVisible();
  expect(sourceMessageLoads).toBe(1);
  expect(targetActivityLoads).toBe(0);

  await page.locator(".chat-message.assistant").filter({ hasText: "Target transcript" }).getByRole("button", { name: "Show activity" }).click();
  await expect(page.getByText("Deferred command")).toBeVisible();
  expect(targetActivityLoads).toBe(1);
});

test("conversation switching between projects detaches the provider viewer without stopping Core work", async ({ page }) => {
  let cancelRequests = 0;
  const projects = [
    { ...entity, id: "project-a", name: "Project A", description: "Active provider work", status: "active", tags: [], metadata: {} },
    { ...entity, id: "project-b", name: "Project B", description: "Parallel workspace", status: "active", tags: [], metadata: {} },
  ];
  await page.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path.endsWith("/engagements") && request.method() === "GET") {
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(projects) });
      return;
    }
    if (path.endsWith("/providers") && request.method() === "GET") {
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify([{
        ...entity,
        id: "provider-continuity",
        name: "Local provider",
        provider_type: "vllm",
        endpoint: "http://127.0.0.1:8000/v1",
        enabled: true,
        is_local: true,
        secret_ref: null,
        model_allowlist: ["model-a"],
        capabilities: { streaming: true },
        privacy: { local_only: true, permits_sensitive_data: true },
        metadata: { default_model: "model-a" },
      }]) });
      return;
    }
    if (path.includes("/chat/turns/") && path.endsWith("/cancel")) {
      cancelRequests += 1;
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({
        ...entity,
        id: "provider-turn-a",
        session_id: "provider-session-a",
        status: "cancelled",
        approval_id: null,
        harness_turn_id: null,
        tool_call_ids: [],
      }) });
      return;
    }
    await route.fallback();
  });
  await page.addInitScript(() => {
    const nativeFetch = globalThis.fetch.bind(globalThis);
    globalThis.fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
      if (!url.endsWith("/chat/completions")) return nativeFetch(input, init);
      const encoder = new TextEncoder();
      let streamController: ReadableStreamDefaultController<Uint8Array> | undefined;
      const stream = new ReadableStream<Uint8Array>({
        start(controller) {
          streamController = controller;
          controller.enqueue(encoder.encode(`data: ${JSON.stringify({ type: "started", provider_id: "provider-continuity", model: "model-a", session_id: "provider-session-a", turn_id: "provider-turn-a" })}\n\n`));
          controller.enqueue(encoder.encode(`data: ${JSON.stringify({ type: "delta", provider_id: "provider-continuity", model: "model-a", delta: "Inspecting Project A without blocking navigation." })}\n\n`));
        },
      });
      init?.signal?.addEventListener("abort", () => {
        (globalThis as typeof globalThis & { __providerViewerDetached?: boolean }).__providerViewerDetached = true;
        streamController?.error(new DOMException("Viewer detached", "AbortError"));
        globalThis.setTimeout(() => {
          (globalThis as typeof globalThis & { __providerCoreCompleted?: boolean }).__providerCoreCompleted = true;
        }, 60);
      }, { once: true });
      return new Response(stream, { status: 200, headers: { "content-type": "text/event-stream" } });
    };
  });

  await openWorkspace(page, "/?view=chat", "Workbench");
  await page.getByRole("button", { name: "New chat", exact: true }).click();
  const composer = page.getByPlaceholder("Ask about this project…");
  await composer.fill("Continue while I inspect another project");
  await page.getByRole("button", { name: "Send message" }).click();
  await expect(page.getByText("Inspecting Project A without blocking navigation.")).toBeVisible();

  const mobileViewport = (page.viewportSize()?.width ?? 1_000) <= 760;
  if (mobileViewport) await page.getByRole("button", { name: "Show sidebar" }).click();
  await page.getByRole("button", { name: "Switch project" }).click();
  await page.getByRole("dialog", { name: "Project switcher" }).getByRole("button", { name: /Project B/ }).click();
  await expect(page.getByRole("button", { name: "Switch project" })).toContainText("Project B");
  await expect.poll(() => page.evaluate(() => (globalThis as typeof globalThis & { __providerViewerDetached?: boolean }).__providerViewerDetached)).toBe(true);
  await expect.poll(() => page.evaluate(() => (globalThis as typeof globalThis & { __providerCoreCompleted?: boolean }).__providerCoreCompleted)).toBe(true);
  expect(cancelRequests).toBe(0);

  await page.getByRole("button", { name: "Switch project" }).click();
  await page.getByRole("dialog", { name: "Project switcher" }).getByRole("button", { name: /Project A/ }).click();
  if (mobileViewport) {
    await page.getByRole("button", { name: "Close sidebar" }).click({
      position: { x: (page.viewportSize()?.width ?? 390) - 8, y: 80 },
    });
  }
  await page.getByRole("button", { name: "New chat", exact: true }).click();
  await page.getByPlaceholder("Ask about this project…").fill("Stop this response explicitly");
  await page.getByRole("button", { name: "Send message" }).click();
  await page.getByRole("button", { name: "Stop response" }).click();
  await expect.poll(() => cancelRequests).toBe(1);
});

test("harness model controls expose only the selected runtime's advertised options", async ({ page }, testInfo) => {
  const harnesses = [{
    ...entity,
    id: "harness-grok-options",
    name: "Grok ACP",
    kind: "grok_acp",
    connection_mode: "spawn",
    transport: "stdio",
    executable: "grok",
    endpoint: null,
    auth_mode: "existing_session",
    secret_ref: null,
    default_model: "grok-4.6",
    enabled: true,
    privacy: { local_only: true, permits_sensitive_data: true },
    native_capabilities: { workspace_access: "write", shell: true, web_search: true, skills: true },
    capabilities: {
      models: ["grok-4.6", "grok-4.5"],
      model_options: [
        {
          model: "grok-4.6",
          reasoning_efforts: [
            { id: "xhigh", label: "Extra high", description: "Maximum reasoning depth." },
            { id: "high", label: "High", description: "Thorough reasoning." },
            { id: "medium", label: "Medium", description: "Balanced reasoning." },
            { id: "low", label: "Low", description: "Light reasoning." },
          ],
          default_reasoning_effort: "high",
          service_tiers: [],
          default_service_tier: null,
        },
        {
          model: "grok-4.5",
          reasoning_efforts: [
            { id: "high", label: "High", description: null },
            { id: "medium", label: "Medium", description: null },
            { id: "low", label: "Low", description: null },
          ],
          default_reasoning_effort: "high",
          service_tiers: [],
          default_service_tier: null,
        },
      ],
      checked_at: entity.updated_at,
      harness_version: "1.0.5",
    },
  }, {
    ...entity,
    id: "harness-codex-options",
    name: "Codex",
    kind: "codex_app_server",
    connection_mode: "spawn",
    transport: "stdio",
    executable: "codex",
    endpoint: null,
    auth_mode: "existing_session",
    secret_ref: null,
    default_model: "gpt-5.6",
    enabled: true,
    privacy: { local_only: true, permits_sensitive_data: true },
    native_capabilities: { workspace_access: "write", shell: true, web_search: true, skills: true },
    capabilities: {
      models: ["gpt-5.6"],
      model_options: [{
        model: "gpt-5.6",
        reasoning_efforts: [
          { id: "medium", label: "Medium", description: null },
          { id: "high", label: "High", description: null },
        ],
        default_reasoning_effort: "medium",
        service_tiers: [
          { id: "default", label: "Default", description: "Standard service tier." },
          { id: "fast", label: "Fast", description: "Priority service tier." },
        ],
        default_service_tier: "default",
      }],
      checked_at: entity.updated_at,
      harness_version: "0.149.0",
    },
  }];
  await page.route("**/api/v1/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith("/harnesses")) {
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(harnesses) });
      return;
    }
    await route.fallback();
  });

  await openWorkspace(page, "/?view=chat", "Workbench");
  await page.getByRole("button", { name: "New chat", exact: true }).click();
  const settingsButton = page.getByRole("button", { name: "Assistant settings", exact: true });
  await expect(page.getByRole("dialog", { name: "Assistant settings" })).toHaveCount(0);
  const triggerStyle = await settingsButton.evaluate((element) => {
    const style = getComputedStyle(element);
    return {
      borderWidths: [style.borderTopWidth, style.borderRightWidth, style.borderBottomWidth, style.borderLeftWidth],
      background: style.backgroundColor,
    };
  });
  expect(triggerStyle.borderWidths).toEqual(["0px", "0px", "0px", "0px"]);
  expect(triggerStyle.background).toBe("rgba(0, 0, 0, 0)");
  await settingsButton.click();
  const settingsDialog = page.getByRole("dialog", { name: "Assistant settings" });
  await expect(settingsDialog).toBeVisible();
  const expandedTriggerStyle = await settingsButton.evaluate((element) => {
    const style = getComputedStyle(element);
    return { background: style.backgroundColor, borderWidths: [style.borderTopWidth, style.borderRightWidth, style.borderBottomWidth, style.borderLeftWidth] };
  });
  expect(expandedTriggerStyle.background).toBe("rgba(0, 0, 0, 0)");
  expect(expandedTriggerStyle.borderWidths).toEqual(["0px", "0px", "0px", "0px"]);
  const settingsGeometry = await settingsDialog.evaluate((element) => {
    const bounds = element.getBoundingClientRect();
    const header = element.querySelector<HTMLElement>("header")!.getBoundingClientRect();
    const fields = element.querySelector<HTMLElement>(".chat-settings-fields")!;
    const firstControl = fields.querySelector<HTMLSelectElement>("select")!.getBoundingClientRect();
    return {
      width: bounds.width,
      left: bounds.left,
      right: bounds.right,
      top: bounds.top,
      bottom: bounds.bottom,
      headerHeight: header.height,
      columns: getComputedStyle(fields).gridTemplateColumns.split(" ").length,
      controlHeight: firstControl.height,
      coarsePointer: matchMedia("(hover: none), (pointer: coarse)").matches,
      scrollWidth: element.scrollWidth,
      clientWidth: element.clientWidth,
    };
  });
  expect(settingsGeometry.width).toBeLessThanOrEqual(721);
  expect(settingsGeometry.headerHeight).toBeLessThanOrEqual(45);
  expect(settingsGeometry.columns).toBe(settingsGeometry.width >= 430 ? 2 : 1);
  expect(settingsGeometry.controlHeight).toBeGreaterThanOrEqual(settingsGeometry.coarsePointer ? 43.5 : 33.5);
  expect(settingsGeometry.controlHeight).toBeLessThanOrEqual(settingsGeometry.coarsePointer ? 44.5 : 34.5);
  expect(settingsGeometry.left).toBeGreaterThanOrEqual(0);
  expect(settingsGeometry.right).toBeLessThanOrEqual((page.viewportSize()?.width ?? 1440) + 1);
  expect(settingsGeometry.top).toBeGreaterThanOrEqual(0);
  expect(settingsGeometry.bottom).toBeLessThanOrEqual((page.viewportSize()?.height ?? 900) + 1);
  expect(settingsGeometry.scrollWidth).toBeLessThanOrEqual(settingsGeometry.clientWidth + 1);
  expect(settingsGeometry.bottom).toBeLessThanOrEqual(await page.locator(".chat-composer").evaluate((element) => element.getBoundingClientRect().top + 1));
  expect((await new AxeBuilder({ page }).include("#assistant-settings-popover").analyze()).violations).toEqual([]);
  await page.getByRole("combobox", { name: "Chat runtime" }).selectOption("harness");
  await page.getByRole("combobox", { name: "Chat harness", exact: true }).selectOption("harness-grok-options");
  await expect.poll(() => settingsButton.evaluate((element) => getComputedStyle(element).backgroundColor)).toBe("rgba(0, 0, 0, 0)");
  await page.screenshot({ path: testInfo.outputPath("assistant-settings.png") });

  const model = page.getByRole("combobox", { name: "Chat harness model" });
  const effort = page.getByRole("combobox", { name: "Harness reasoning effort" });
  await expect(model).toHaveValue("grok-4.6");
  await expect(model.locator("option")).toHaveText(["Select model", "grok-4.6", "grok-4.5"]);
  await expect(effort).toHaveValue("high");
  await expect(effort.locator("option")).toHaveText(["Harness default", "Extra high", "High", "Medium", "Low"]);
  await expect(page.getByRole("combobox", { name: "Harness speed" })).toHaveCount(0);

  await page.getByRole("combobox", { name: "Chat harness", exact: true }).selectOption("harness-codex-options");
  await expect(model).toHaveValue("gpt-5.6");
  await expect(effort).toHaveValue("medium");
  await expect(page.getByRole("combobox", { name: "Harness speed" })).toHaveValue("default");
  await expect(page.getByRole("combobox", { name: "Harness speed" }).locator("option"))
    .toHaveText(["Harness default", "Default", "Fast"]);

  await page.keyboard.press("Escape");
  await expect(page.getByRole("dialog", { name: "Assistant settings" })).toHaveCount(0);
  await expect(settingsButton).toBeFocused();

  expect(await page.locator("body").evaluate((body) => body.scrollWidth - body.clientWidth)).toBeLessThanOrEqual(1);
});

test("AI writing submits the visible supported model", async ({ page }, testInfo) => {
  const report = {
    ...entity,
    id: "report-ai-model",
    engagement_id: "scratch-project",
    title: "Tested report",
    status: "draft",
    executive_summary: "",
    finding_ids: [],
    observation_ids: [],
    note_transforms: [],
    artifact_ids: [],
    executive_summary_provenance: null,
    signed_off_by: null,
    signed_off_at: null,
    metadata: {},
  };
  const grokHarness = {
    ...entity,
    id: "harness-grok-writing",
    name: "grok",
    kind: "grok_acp",
    connection_mode: "spawn",
    transport: "stdio",
    executable: "grok",
    endpoint: null,
    auth_mode: "existing_session",
    secret_ref: null,
    default_model: "grok-4.6",
    enabled: true,
    privacy: { local_only: true, permits_sensitive_data: true },
    native_capabilities: { workspace_access: "write", shell: true, web_search: true, skills: true },
    capabilities: { models: ["grok-4.6"], checked_at: entity.updated_at, harness_version: "1.0.5" },
  };
  let submittedModel = "";
  await page.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path.endsWith("/reports") && request.method() === "GET") {
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify([report]) });
      return;
    }
    if (path.endsWith("/harnesses") && request.method() === "GET") {
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify([grokHarness]) });
      return;
    }
    if (path.endsWith("/writing/transform") && request.method() === "POST") {
      submittedModel = (request.postDataJSON() as { model: string }).model;
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({
        content: "Generated executive summary.",
        provenance: {
          backend_kind: "harness",
          provider_profile_id: grokHarness.id,
          harness_profile_id: grokHarness.id,
          model: submittedModel,
          prompt_version: "writing-transform/v1",
          source_sha256: "a".repeat(64),
          instruction: "Draft a concise executive summary.",
          generated_at: entity.updated_at,
          provider_request_id: "turn-writing-model",
        },
        usage: { input_tokens: 20, output_tokens: 8, total_tokens: 28 },
      }) });
      return;
    }
    await route.fallback();
  });

  await openWorkspace(page, "/reports", "Reports");
  await page.getByRole("button", { name: "Draft executive summary with AI" }).click();
  const dialog = page.getByRole("dialog", { name: "Draft executive summary with AI" });
  await expect(dialog).toBeVisible();
  const model = dialog.getByRole("combobox", { name: "AI writing model" });
  await expect(model).toHaveValue("grok-4.6");
  await dialog.getByRole("button", { name: "Generate draft" }).click();
  await expect(dialog.getByRole("textbox", { name: "AI writing draft" })).toHaveValue("Generated executive summary.");
  expect(submittedModel).toBe(await model.inputValue());
  expect(submittedModel).toBe("grok-4.6");

  const geometry = await dialog.evaluate((element) => {
    const bounds = element.getBoundingClientRect();
    const actions = [...element.querySelectorAll<HTMLElement>("button")].filter((button) => getComputedStyle(button).display !== "none").map((button) => button.getBoundingClientRect().height);
    return { left: bounds.left, right: bounds.right, scrollWidth: element.scrollWidth, clientWidth: element.clientWidth, viewportWidth: innerWidth, actionHeights: actions };
  });
  expect(geometry.left).toBeGreaterThanOrEqual(0);
  expect(geometry.right).toBeLessThanOrEqual(geometry.viewportWidth + 1);
  expect(geometry.scrollWidth).toBeLessThanOrEqual(geometry.clientWidth + 1);
  if (testInfo.project.name.startsWith("mobile-")) expect(geometry.actionHeights.every((height) => height >= 44)).toBe(true);
  const accessibility = await new AxeBuilder({ page }).include(".ai-writing-dialog").analyze();
  expect(accessibility.violations.filter((violation) => violation.id !== "aria-allowed-role")).toEqual([]);
});

test("oversized harness activity fails compactly without blocking mobile chat", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "desktop" && !testInfo.project.name.startsWith("mobile-"), "Covered by permanent desktop and mobile browser projects.");
  const detail = [{
    type: "string_too_long",
    loc: ["response", "summary"],
    msg: "String should have at most 4000 characters",
    input: `chat · MCP grok/${"x".repeat(8_000)}`,
  }];
  await page.route("**/api/v1/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith("/chat-sessions")) {
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify([{
        ...entity,
        id: "chat-verbose-activity",
        engagement_id: "scratch-project",
        title: "Verbose Grok activity",
        backend: "harness",
        harness_profile_id: "harness-verbose",
        harness_session_id: "session-verbose",
        model: "grok-build",
        metadata: {},
      }]) });
      return;
    }
    if (path.endsWith("/chat/sessions/chat-verbose-activity/messages")) {
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify([{
        ...entity,
        id: "assistant-verbose",
        engagement_id: "scratch-project",
        session_id: "chat-verbose-activity",
        sequence: 1,
        role: "assistant",
        content: "Earlier Grok response",
        citations: [],
        metadata: { harness_turn_id: "turn-verbose" },
      }]) });
      return;
    }
    if (path.endsWith("/harness-turns/turn-verbose/events")) {
      await route.fulfill({ status: 422, contentType: "application/json", headers: { "x-request-id": "req_verbose" }, body: JSON.stringify({ detail }) });
      return;
    }
    if (path.endsWith("/harness-turns/turn-verbose/interactions")) {
      await route.fulfill({ status: 200, contentType: "application/json", body: "[]" });
      return;
    }
    if (path.endsWith("/chat/sessions/chat-verbose-activity/pending-turn")) {
      await route.fulfill({ status: 200, contentType: "application/json", body: "null" });
      return;
    }
    if (path.endsWith("/harness-sessions/session-verbose/activity")) {
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({
        session_id: "session-verbose",
        session_status: "idle",
        busy: false,
        live: true,
        turn_id: null,
        turn_status: null,
        turn_origin: null,
        started_at: null,
        last_activity_at: entity.updated_at,
        detail: "This harness session is idle.",
      }) });
      return;
    }
    await route.fallback();
  });

  await openWorkspace(page, "/?view=chat&session=chat-verbose-activity", "Workbench");
  await page.getByRole("button", { name: "Show activity" }).click();
  const notice = page.locator(".chat-panel .diagnostic-error-notice.compact");
  await expect(notice).toBeVisible();
  await expect(notice.locator("strong")).toHaveText("The supplied summary exceeded its validated length limit.");
  await expect(notice).not.toContainText("x".repeat(100));
  await expect(page.getByRole("textbox", { name: "Message the analyst assistant" })).toBeVisible();
  const geometry = await notice.evaluate((element) => {
    const bounds = element.getBoundingClientRect();
    return {
      left: bounds.left,
      right: bounds.right,
      width: bounds.width,
      scrollWidth: element.scrollWidth,
      clientWidth: element.clientWidth,
      viewportWidth: innerWidth,
    };
  });
  expect(geometry.left).toBeGreaterThanOrEqual(0);
  expect(geometry.right).toBeLessThanOrEqual(geometry.viewportWidth + 1);
  expect(geometry.scrollWidth).toBeLessThanOrEqual(geometry.clientWidth + 1);
  const accessibility = await new AxeBuilder({ page }).include(".diagnostic-error-notice.compact").analyze();
  expect(accessibility.violations).toEqual([]);
});

test("activity ledger groups repeated work into a compact operator receipt", async ({ page }, testInfo) => {
  test.skip(!["desktop", "compact", "narrow"].includes(testInfo.project.name) && !testInfo.project.name.startsWith("mobile-"), "Covered by permanent desktop and mobile browser projects.");
  const turnId = "turn-activity-ledger";
  let activityLoads = 0;
  await page.route("**/api/v1/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith("/chat-sessions")) {
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify([{
        ...entity,
        id: "chat-activity-ledger",
        engagement_id: "scratch-project",
        title: "Activity ledger",
        backend: "harness",
        harness_profile_id: "harness-activity-ledger",
        harness_session_id: "session-activity-ledger",
        model: "security-model",
        metadata: {},
      }]) });
      return;
    }
    if (path.endsWith("/chat/sessions/chat-activity-ledger/messages")) {
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify([{
        ...entity,
        id: "assistant-activity-ledger",
        engagement_id: "scratch-project",
        session_id: "chat-activity-ledger",
        sequence: 1,
        role: "assistant",
        content: "All bounded records were saved.",
        citations: [],
        metadata: { harness_turn_id: turnId },
      }]) });
      return;
    }
    if (path.endsWith(`/harness-turns/${turnId}/events`)) {
      activityLoads += 1;
      const events = Array.from({ length: 36 }, (_, index) => ({
        id: `activity-${index + 1}`,
        type: "item_upsert",
        schema_version: "nebula.harness-activity/v2",
        sequence: index + 1,
        vendor: "codex_app_server",
        harness_session_id: "session-activity-ledger",
        harness_turn_id: turnId,
        item_id: `tool-${index + 1}`,
        item_kind: "tool",
        item_status: "completed",
        title: "item upsert",
        summary: `Saved bounded record ${index + 1}.`,
        occurred_at: new Date(Date.UTC(2026, 7, 26, 12, 0, index)).toISOString(),
        artifact_ids: [],
        payload: { record: index + 1 },
      }));
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ events, next_sequence: 36 }) });
      return;
    }
    if (path.endsWith(`/harness-turns/${turnId}/interactions`)) {
      await route.fulfill({ status: 200, contentType: "application/json", body: "[]" });
      return;
    }
    if (path.endsWith("/harness-sessions/session-activity-ledger/activity")) {
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({
        session_id: "session-activity-ledger",
        session_status: "idle",
        busy: false,
        live: true,
        turn_id: turnId,
        turn_status: "complete",
        turn_origin: "chat",
        started_at: "2026-08-26T12:00:00Z",
        last_activity_at: "2026-08-26T12:00:35Z",
        detail: "Harness is ready.",
      }) });
      return;
    }
    if (path.endsWith("/chat/sessions/chat-activity-ledger/pending-turn")) {
      await route.fulfill({ status: 200, contentType: "application/json", body: "null" });
      return;
    }
    await route.fallback();
  });

  await openWorkspace(page, "/?view=chat&session=chat-activity-ledger", "Workbench");
  const ledger = page.getByRole("region", { name: "Work summary" });
  await expect(ledger.getByText(/0 actions/).first()).toBeVisible();
  expect(activityLoads).toBe(0);
  const showActivity = ledger.getByRole("button", { name: "Show activity" });
  if (testInfo.project.name.startsWith("mobile-") || testInfo.project.name === "narrow") {
    const bounds = await showActivity.boundingBox();
    expect(bounds?.height).toBeGreaterThanOrEqual(44);
  }
  const geometry = await ledger.evaluate((element) => ({
    left: element.getBoundingClientRect().left,
    right: element.getBoundingClientRect().right,
    viewportWidth: innerWidth,
    scrollWidth: element.scrollWidth,
    clientWidth: element.clientWidth,
  }));
  expect(geometry.left).toBeGreaterThanOrEqual(0);
  expect(geometry.right).toBeLessThanOrEqual(geometry.viewportWidth + 1);
  expect(geometry.scrollWidth).toBeLessThanOrEqual(geometry.clientWidth + 1);
  const accessibility = await new AxeBuilder({ page }).include(".activity-ledger").analyze();
  expect(accessibility.violations).toEqual([]);
  await showActivity.click();
  await expect(ledger.getByText(/36 actions/).first()).toBeVisible();
  await expect(ledger).not.toContainText("item upsert");
  await expect(ledger.locator(".activity-ledger-audit > ol > li")).toHaveCount(36);
  expect(activityLoads).toBe(1);
});

test("native assistant tools use the shared activity ledger", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "desktop", "One desktop stream proves the native-provider adapter; responsive ledger behavior is covered separately.");
  const provider = {
    ...entity,
    id: "provider-native-ledger",
    name: "Native ledger provider",
    provider_type: "vllm",
    endpoint: "http://127.0.0.1:8000/v1",
    enabled: true,
    is_local: true,
    secret_ref: null,
    model_allowlist: ["native-ledger-model"],
    capabilities: { streaming: true, tools: true },
    privacy: { local_only: true, permits_sensitive_data: true },
    metadata: { default_model: "native-ledger-model" },
  };
  let replayDurableReceipt = false;
  await page.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path.endsWith("/providers") && request.method() === "GET") {
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify([provider]) });
      return;
    }
    if (path.endsWith("/chat-sessions") && request.method() === "GET") {
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(replayDurableReceipt ? [{
        ...entity,
        id: "native-ledger-session",
        engagement_id: "scratch-project",
        title: "Review the saved evidence.",
        backend: "provider",
        provider_profile_id: provider.id,
        model: "native-ledger-model",
        metadata: {},
      }] : []) });
      return;
    }
    if (path.endsWith("/chat/sessions/native-ledger-session/messages")) {
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify([
        { ...entity, id: "native-ledger-user", engagement_id: "scratch-project", session_id: "native-ledger-session", sequence: 1, role: "user", content: "Review the saved evidence.", citations: [], metadata: {} },
        { ...entity, id: "native-ledger-assistant", engagement_id: "scratch-project", session_id: "native-ledger-session", sequence: 2, role: "assistant", content: "Evidence review complete.", citations: [], metadata: { tool_results: [{ tool_call_id: "native-tool-1", capability: "Search evidence", status: "complete", summary: "Found two relevant evidence records.", evidence_ids: ["evidence-ledger"], result_artifact_id: null }] } },
      ]) });
      return;
    }
    if (path.endsWith("/chat/sessions/native-ledger-session/pending-turn")) {
      await route.fulfill({ status: 200, contentType: "application/json", body: "null" });
      return;
    }
    await route.fallback();
  });
  await page.addInitScript(() => {
    const nativeFetch = globalThis.fetch.bind(globalThis);
    globalThis.fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
      if (!url.endsWith("/chat/completions")) return nativeFetch(input, init);
      const encoder = new TextEncoder();
      const frames = [
        { type: "started", provider_id: "provider-native-ledger", model: "native-ledger-model", session_id: "native-ledger-session", turn_id: "native-ledger-turn" },
        { type: "tool_started", turn_id: "native-ledger-turn", tool_call_id: "native-tool-1", capability: "Search evidence", arguments: { query: "TLS" }, step: 1 },
        { type: "tool_completed", turn_id: "native-ledger-turn", tool_call_id: "native-tool-1", capability: "Search evidence", status: "complete", summary: "Found two relevant evidence records.", evidence_ids: ["evidence-ledger"], artifacts: [], receipt: { matches: 2 }, step: 1 },
        { type: "done", provider_id: "provider-native-ledger", model: "native-ledger-model", session_id: "native-ledger-session", turn_id: "native-ledger-turn", message: { id: "native-ledger-assistant", role: "assistant", content: "Evidence review complete." }, usage: { input_tokens: 4, output_tokens: 5, total_tokens: 9 }, finish_reason: "stop", citations: [] },
      ];
      const stream = new ReadableStream<Uint8Array>({
        start(controller) {
          frames.forEach((frame) => controller.enqueue(encoder.encode(`data: ${JSON.stringify(frame)}\n\n`)));
          controller.enqueue(encoder.encode("data: [DONE]\n\n"));
          controller.close();
        },
      });
      return new Response(stream, { status: 200, headers: { "content-type": "text/event-stream" } });
    };
  });

  await openWorkspace(page, "/?view=chat", "Workbench");
  await page.getByRole("button", { name: "New chat", exact: true }).click();
  const composer = page.getByPlaceholder("Ask about this project…");
  await composer.fill("Review the saved evidence.");
  await page.getByRole("button", { name: "Send message" }).click();
  const ledger = page.getByRole("region", { name: "Work summary" });
  await expect(ledger.getByText(/1 action/).first()).toBeVisible();
  await ledger.getByRole("button", { name: "Show activity" }).click();
  await ledger.getByText("Search evidence", { exact: true }).click();
  await expect(ledger.getByText("Found two relevant evidence records.")).toBeVisible();
  await expect(ledger.getByRole("link", { name: /Evidence evidence/ })).toHaveAttribute("href", "/evidence?id=evidence-ledger");

  replayDurableReceipt = true;
  await page.goto("/?view=chat&session=native-ledger-session");
  const restoredLedger = page.getByRole("region", { name: "Work summary" });
  await expect(restoredLedger.getByText(/1 action/).first()).toBeVisible();
  await expect(restoredLedger).not.toContainText("item upsert");
  await restoredLedger.getByRole("button", { name: "Show activity" }).click();
  await expect(restoredLedger.getByText("Search evidence", { exact: true })).toBeVisible();
});

test("completed harness output keeps one continuous transcript scroll", async ({ page }, testInfo) => {
  test.skip(!["desktop", "compact"].includes(testInfo.project.name) && !testInfo.project.name.startsWith("mobile-"), "Covered by the permanent desktop and mobile harness projects.");
  const harnessSessionId = "c9745e80-3333-4444-8555-666677778888";
  const harnessTurnId = "c9745e80-4444-4555-8666-777788889999";
  let steeringBody: unknown;
  await page.route("**/api/v1/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith(`/harness-turns/${harnessTurnId}/steer`)) {
      steeringBody = route.request().postDataJSON();
      await route.fulfill({ status: 204, body: "" });
      return;
    }
    if (path.endsWith("/harnesses/harness-completion/skills")) {
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify([{
        name: "review",
        path: "/workspace/.agents/skills/review/SKILL.md",
        source: "project",
      }]) });
      return;
    }
    if (path.endsWith("/harnesses")) {
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify([{
        ...entity,
        id: "harness-completion",
        name: "Codex harness",
        kind: "codex_app_server",
        connection_mode: "spawn",
        transport: "stdio",
        executable: "codex",
        endpoint: null,
        auth_mode: "existing_session",
        secret_ref: null,
        default_model: "gpt-5-codex",
        enabled: true,
        privacy: { local_only: true, permits_sensitive_data: true },
        native_capabilities: { workspace_access: "write", shell: true, web_search: true, skills: true },
        capabilities: { models: ["gpt-5-codex"], checked_at: entity.updated_at, harness_version: "1.0", live_command_output: true, planning_mode: true, goal_monitoring: true, skill_invocation: true, steering: true, interruption: true, modes: ["default", "plan"] },
      }]) });
      return;
    }
    if (path.endsWith(`/harness-sessions/${harnessSessionId}/activity`)) {
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({
        session_id: harnessSessionId,
        session_status: "running",
        busy: true,
        live: true,
        turn_id: harnessTurnId,
        turn_status: "running",
        turn_origin: "chat",
        started_at: entity.created_at,
        last_activity_at: entity.updated_at,
        detail: "A harness turn is currently running.",
        plan: [
          { id: "inspect", title: "Inspect the current operator workflow", status: "completed" },
          { id: "implement", title: "Implement the expandable status rail", status: "in_progress" },
          { id: "verify", title: "Verify a deliberately long plan step remains readable without horizontal clipping on compact screens", status: "pending" },
        ],
      }) });
      return;
    }
    if (path.endsWith("/chat-sessions") && route.request().method() === "GET") {
      await route.fulfill({ status: 200, contentType: "application/json", body: "[]" });
      return;
    }
    await route.fallback();
  });
  await page.addInitScript(({ harnessSessionId: session, harnessTurnId: turn }) => {
    const nativeFetch = globalThis.fetch.bind(globalThis);
    globalThis.fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
      if (!url.endsWith("/chat/completions")) return nativeFetch(input, init);
      (globalThis as typeof globalThis & { __harnessTurnRequest?: unknown }).__harnessTurnRequest = JSON.parse(String(init?.body ?? "{}"));
      const encoder = new TextEncoder();
      const output = "Completed command output stays available behind its disclosure without creating a second transcript scroll.\n".repeat(80);
      const answer = "Verification completed successfully. The operator remains in control of the next action.\n\n".repeat(30);
      const frames: unknown[] = [
        { type: "started", harness_profile_id: "harness-completion", harness_session_id: session, harness_turn_id: turn, model: "gpt-5-codex", session_id: "chat-harness-completion", turn_id: "chat-turn-completion" },
        { type: "output_delta", schema_version: "nebula.harness-activity/v1", sequence: 1, vendor: "codex_app_server", harness_session_id: session, harness_turn_id: turn, item_id: "commentary-1", item_kind: "reasoning", item_status: "streaming", title: "Commentary", stream: "commentary", delta: "I found the verification path. I’m checking the production behavior before changing anything.", artifact_ids: [], payload: {} },
        { type: "item_upsert", schema_version: "nebula.harness-activity/v1", sequence: 2, vendor: "codex_app_server", harness_session_id: session, harness_turn_id: turn, item_id: "command-1", item_kind: "command", item_status: "running", title: "Run verification", artifact_ids: [], payload: { command: "npm test" } },
        { type: "output_delta", schema_version: "nebula.harness-activity/v1", sequence: 3, vendor: "codex_app_server", harness_session_id: session, harness_turn_id: turn, item_id: "command-1", item_kind: "command", item_status: "streaming", title: "Run verification", stream: "stdout", delta: output, artifact_ids: [], payload: {} },
        { type: "item_upsert", schema_version: "nebula.harness-activity/v1", sequence: 4, vendor: "codex_app_server", harness_session_id: session, harness_turn_id: turn, item_id: "command-1", item_kind: "command", item_status: "completed", title: "Run verification", summary: "Verification passed.", artifact_ids: [], payload: { command: "npm test", exit_code: 0 } },
        { type: "completed", harness_session_id: session, harness_turn_id: turn, payload: {} },
        { type: "done", session_id: "chat-harness-completion", harness_profile_id: "harness-completion", harness_session_id: session, harness_turn_id: turn, model: "gpt-5-codex", message: { id: "assistant-harness-completion", role: "assistant", content: answer }, usage: { input_tokens: 4, output_tokens: 8, total_tokens: 12 }, finish_reason: "stop", citations: [] },
      ];
      const stream = new ReadableStream<Uint8Array>({
        start(controller) {
          let index = 0;
          const enqueue = () => {
            const frame = frames[index++];
            if (frame) controller.enqueue(encoder.encode(`data: ${JSON.stringify(frame)}\n\n`));
            if (index >= frames.length) {
              controller.enqueue(encoder.encode("data: [DONE]\n\n"));
              controller.close();
              return;
            }
            globalThis.setTimeout(enqueue, index === 3 ? 2_000 : 80);
          };
          enqueue();
        },
      });
      return new Response(stream, { status: 200, headers: { "content-type": "text/event-stream" } });
    };
  }, { harnessSessionId, harnessTurnId });

  await openWorkspace(page, "/?view=chat", "Workbench");
  await page.getByRole("button", { name: "New chat", exact: true }).click();
  await page.getByRole("button", { name: "Assistant settings" }).click();
  await page.getByLabel("Chat harness mode", { exact: true }).selectOption("plan");
  const composer = page.getByPlaceholder("Ask about this project…");
  await expect(composer).toBeEnabled();
  await composer.fill("$rev");
  await expect(page.getByRole("listbox", { name: "Available skills" })).toBeVisible();
  await expect(page.getByRole("option", { name: /\$review/ })).toBeVisible();
  const autocompleteAccessibility = await new AxeBuilder({ page }).include(".chat-composer").analyze();
  expect(autocompleteAccessibility.violations).toEqual([]);
  await page.getByRole("option", { name: /\$review/ }).click();
  await expect(composer).toHaveValue("$review");
  await composer.fill("$review Run the verification.");
  await page.getByRole("button", { name: "Send message" }).click();
  const commentary = page.getByLabel("Assistant commentary");
  await expect(commentary).toContainText("I found the verification path. I’m checking the production behavior before changing anything.");
  await expect(commentary).toBeVisible();
  await expect(page.getByText("Tool started", { exact: true })).toHaveCount(0);
  await expect.poll(() => page.evaluate(() => (globalThis as typeof globalThis & { __harnessTurnRequest?: { harness_mode?: string } }).__harnessTurnRequest?.harness_mode)).toBe("plan");
  expect(await page.evaluate(() => (globalThis as typeof globalThis & { __harnessTurnRequest?: { harness_skill?: unknown } }).__harnessTurnRequest?.harness_skill)).toEqual({
    name: "review",
    path: "/workspace/.agents/skills/review/SKILL.md",
  });
  const guidanceComposer = page.getByPlaceholder("Add guidance while the harness works…");
  await guidanceComposer.fill("Prioritize the TLS boundary and preserve exact output.");
  await page.getByRole("button", { name: "Send guidance now" }).click();
  await expect.poll(() => steeringBody).toEqual({ text: "Prioritize the TLS boundary and preserve exact output." });
  await expect(guidanceComposer).toHaveValue("");
  await expect(page.getByText("Guidance sent to the active harness turn.")).toBeVisible();
  const planToggle = page.getByRole("button", { name: "Expand plan steps, 1 of 3 completed" });
  await expect(planToggle).toBeVisible();
  await planToggle.click();
  const collapsePlan = page.getByRole("button", { name: "Collapse plan steps, 1 of 3 completed" });
  await expect(collapsePlan).toHaveAttribute("aria-expanded", "true");
  const planSteps = page.getByRole("list", { name: "Plan steps" });
  await expect(planSteps).toBeVisible();
  await expect(planSteps.getByText("Implement the expandable status rail")).toBeVisible();
  await expect(planSteps.getByText("In progress")).toBeVisible();
  const planGeometry = await page.locator(".harness-status-rail").evaluate((element) => ({
    left: element.getBoundingClientRect().left,
    right: element.getBoundingClientRect().right,
    viewportWidth: innerWidth,
    scrollWidth: element.scrollWidth,
    clientWidth: element.clientWidth,
  }));
  expect(planGeometry.left).toBeGreaterThanOrEqual(0);
  expect(planGeometry.right).toBeLessThanOrEqual(planGeometry.viewportWidth + 1);
  expect(planGeometry.scrollWidth).toBeLessThanOrEqual(planGeometry.clientWidth + 1);
  if (testInfo.project.name.startsWith("mobile-")) {
    const planToggleBounds = await collapsePlan.boundingBox();
    expect(planToggleBounds?.height).toBeGreaterThanOrEqual(44);
  }
  const planAccessibility = await new AxeBuilder({ page }).include(".harness-status-rail").analyze();
  expect(planAccessibility.violations).toEqual([]);
  await collapsePlan.click();
  await expect(page.getByRole("list", { name: "Plan steps" })).toHaveCount(0);
  const ledger = page.getByRole("region", { name: "Work summary" });
  await expect(ledger.getByText("Completed", { exact: true }).first()).toBeVisible({ timeout: 5_000 });
  await ledger.getByRole("button", { name: "Show activity" }).click();
  const activity = ledger.locator(".activity-ledger-entry-content details", { hasText: "Run verification" });
  await expect(activity).not.toHaveAttribute("open", "");
  await expect(activity.getByText("Completed", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "Stop response" })).toHaveCount(0, { timeout: 5_000 });
  await activity.locator(":scope > summary").click();
  await expect(activity).toHaveAttribute("open", "");
  const chatScroll = page.locator(".chat-scroll");
  const commandOutput = activity.locator(".harness-output pre");
  await expect.poll(() => commandOutput.evaluate((element) => element.scrollHeight - element.clientHeight)).toBeLessThanOrEqual(2);
  await commandOutput.hover();
  const outerBefore = await chatScroll.evaluate((element) => element.scrollTop);
  if (!testInfo.project.name.startsWith("mobile-webkit")) {
    await page.mouse.wheel(0, 500);
    await expect.poll(() => chatScroll.evaluate((element) => element.scrollTop)).toBeGreaterThan(outerBefore + 2);
  }
  const completedMessage = page.locator(".chat-message.assistant").filter({ hasText: "Verification completed successfully" });
  const forkAction = completedMessage.getByRole("button", { name: "Fork conversation here" });
  await expect(completedMessage.locator("header").getByRole("button", { name: "Fork conversation here" })).toHaveCount(0);
  await expect(forkAction.locator("xpath=..")).toHaveClass(/chat-message-actions/);
  await completedMessage.hover();
  await expect(completedMessage.locator(".chat-message-actions")).toHaveCSS("opacity", "1");
  if (testInfo.project.name.startsWith("mobile-")) {
    const bounds = await forkAction.boundingBox();
    expect(bounds?.width).toBeGreaterThanOrEqual(44);
    expect(bounds?.height).toBeGreaterThanOrEqual(44);
  }
});

test("completed harness output surfaces Grok commentary as live narrative", async ({ page }) => {
  const sessionId = "grok-commentary-session";
  const turnId = "grok-commentary-turn";
  await page.route("**/api/v1/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith("/harnesses")) {
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify([{
        ...entity,
        id: "harness-grok-commentary",
        name: "Grok ACP",
        kind: "grok_acp",
        connection_mode: "spawn",
        transport: "stdio",
        executable: "grok",
        endpoint: null,
        auth_mode: "existing_session",
        secret_ref: null,
        default_model: "grok-4.6",
        enabled: true,
        privacy: { local_only: true, permits_sensitive_data: true },
        native_capabilities: { workspace_access: "write", shell: true, web_search: true, skills: false },
        capabilities: { models: ["grok-4.6"], checked_at: entity.updated_at, harness_version: "1.0.5", interruption: true },
      }]) });
      return;
    }
    if (path.endsWith(`/harness-sessions/${sessionId}/activity`)) {
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({
        session_id: sessionId,
        session_status: "running",
        busy: true,
        live: true,
        turn_id: turnId,
        turn_status: "running",
        turn_origin: "chat",
        started_at: entity.created_at,
        last_activity_at: entity.updated_at,
        detail: "Grok is working.",
      }) });
      return;
    }
    await route.fallback();
  });
  await page.addInitScript(({ session, turn }) => {
    const nativeFetch = globalThis.fetch.bind(globalThis);
    globalThis.fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
      if (!url.endsWith("/chat/completions")) return nativeFetch(input, init);
      const encoder = new TextEncoder();
      const frames = [
        { type: "started", harness_profile_id: "harness-grok-commentary", harness_session_id: session, harness_turn_id: turn, model: "grok-4.6", session_id: "chat-grok-commentary", turn_id: "chat-turn-grok-commentary" },
        { type: "output_delta", schema_version: "nebula.harness-activity/v1", sequence: 1, vendor: "grok_acp", harness_session_id: session, harness_turn_id: turn, item_id: "commentary", item_kind: "reasoning", item_status: "streaming", title: "Commentary", stream: "commentary", delta: "I’ve mapped the workspace. Next I’m validating the affected files before I make the change.", artifact_ids: [], payload: {} },
        { type: "tool_started", schema_version: "nebula.harness-activity/v1", sequence: 2, vendor: "grok_acp", harness_session_id: session, harness_turn_id: turn, item_id: "tool-1", item_kind: "tool", item_status: "running", title: "Inspect workspace", artifact_ids: [], payload: {} },
        { type: "completed", harness_session_id: session, harness_turn_id: turn, payload: {} },
        { type: "done", session_id: "chat-grok-commentary", harness_profile_id: "harness-grok-commentary", harness_session_id: session, harness_turn_id: turn, model: "grok-4.6", message: { id: "assistant-grok-commentary", role: "assistant", content: "The workspace validation is complete." }, usage: { input_tokens: 4, output_tokens: 8, total_tokens: 12 }, finish_reason: "stop", citations: [] },
      ];
      const stream = new ReadableStream<Uint8Array>({
        start(controller) {
          frames.forEach((frame) => controller.enqueue(encoder.encode(`data: ${JSON.stringify(frame)}\n\n`)));
          controller.enqueue(encoder.encode("data: [DONE]\n\n"));
          controller.close();
        },
      });
      return new Response(stream, { status: 200, headers: { "content-type": "text/event-stream" } });
    };
  }, { session: sessionId, turn: turnId });

  await openWorkspace(page, "/?view=chat", "Workbench");
  await page.getByRole("button", { name: "New chat", exact: true }).click();
  await page.getByRole("button", { name: "Assistant settings" }).click();
  await page.getByRole("combobox", { name: "Chat runtime" }).selectOption("harness");
  await page.keyboard.press("Escape");
  const composer = page.getByPlaceholder("Ask about this project…");
  await composer.fill("Inspect the workspace and explain what you are doing");
  await page.getByRole("button", { name: "Send message" }).click();

  const commentary = page.getByLabel("Assistant commentary");
  await expect(commentary).toContainText("I’ve mapped the workspace. Next I’m validating the affected files before I make the change.");
  await expect(commentary).toBeVisible();
  await expect(page.getByText("Tool started", { exact: true })).toHaveCount(0);
  const commentaryGeometry = await commentary.evaluate((element) => ({
    left: element.getBoundingClientRect().left,
    right: element.getBoundingClientRect().right,
    viewportWidth: innerWidth,
    scrollWidth: element.scrollWidth,
    clientWidth: element.clientWidth,
  }));
  expect(commentaryGeometry.left).toBeGreaterThanOrEqual(0);
  expect(commentaryGeometry.right).toBeLessThanOrEqual(commentaryGeometry.viewportWidth + 1);
  expect(commentaryGeometry.scrollWidth).toBeLessThanOrEqual(commentaryGeometry.clientWidth + 1);
  expect((await new AxeBuilder({ page }).include(".assistant-commentary").analyze()).violations).toEqual([]);
});

test("the workbench expands to the full viewport and restores in place", async ({ page }) => {
  await page.addInitScript(() => localStorage.setItem("nebula.theme", "zero-dark"));
  await openWorkspace(page, "/?view=chat", "Workbench");

  const mobile = (page.viewportSize()?.width ?? 1440) <= 760;
  if (mobile) {
    await page.getByRole("button", { name: "More workbench views" }).click();
    await page.getByRole("dialog", { name: "More views" }).getByRole("button", { name: "Enter focus mode" }).click();
  } else {
    await page.getByRole("button", { name: "More Workbench actions" }).click();
    await page.getByRole("menuitem", { name: /Enter focus mode/ }).click();
  }
  const workbench = page.locator(".sessions-page.full-screen");
  await expect(workbench).toBeVisible();
  const geometry = await workbench.evaluate((element) => {
    const rect = element.getBoundingClientRect();
    return {
      position: getComputedStyle(element).position,
      top: rect.top,
      left: rect.left,
      right: window.innerWidth - rect.right,
      bottom: window.innerHeight - rect.bottom,
    };
  });
  expect(geometry.position).toBe("fixed");
  expect(Math.abs(geometry.top)).toBeLessThanOrEqual(1);
  expect(Math.abs(geometry.left)).toBeLessThanOrEqual(1);
  expect(Math.abs(geometry.right)).toBeLessThanOrEqual(1);
  expect(Math.abs(geometry.bottom)).toBeLessThanOrEqual(1);
  await expect(page.getByRole("heading", { name: "Workbench" })).toBeHidden();

  const fullScreenViews = [
    ["Terminal", ".persistent-terminal"],
    ["Workspace code editor", ".persistent-code-editor"],
    ["Project browser", ".persistent-browser"],
    ["Analyst chat", ".session-workspace > .chat-empty-state"],
    ["Workspace files", ".workspace-browser"],
    ["Project notes", ".notes-panel"],
    ["Autonomous missions", ".agents-page"],
    ["Activity history", ".workbench-activity-stack"],
  ] as const;
  for (const [tabName, contentSelector] of mobile
    ? fullScreenViews.filter(([tabName]) => tabName === "Analyst chat")
    : fullScreenViews) {
    if (!mobile) await page.getByRole("tab", { name: tabName, exact: true }).click();
    const content = page.locator(contentSelector);
    await expect(content).toBeVisible();
    const bounds = await content.evaluate((element) => {
      const root = element.getBoundingClientRect();
      const workspace = element.closest(".session-workspace")!.getBoundingClientRect();
      return {
        contentWidth: root.width,
        contentHeight: root.height,
        workspaceWidth: workspace.width,
        workspaceHeight: workspace.height,
      };
    });
    expect(bounds.contentWidth, tabName).toBeGreaterThanOrEqual(bounds.workspaceWidth - 26);
    expect(bounds.contentHeight, tabName).toBeGreaterThanOrEqual(bounds.workspaceHeight - 26);
  }

  await page.keyboard.press("Escape");
  if (mobile) await expect(page.getByRole("button", { name: "More workbench views" })).toBeVisible();
  else await expect(page.getByRole("button", { name: "More Workbench actions" })).toBeVisible();
  await expect(page.locator(".sessions-page")).not.toHaveClass(/full-screen/);
});

test("both Zero themes snap primary workbench surfaces to the available screen", async ({ page }) => {
  test.skip((page.viewportSize()?.width ?? 1440) <= 760, "phone layouts use the mobile workbench navigation");

  await openWorkspace(page, "/", "Workbench");
  const surfaces = [
    ["Terminal", ".persistent-terminal"],
    ["Workspace code editor", ".persistent-code-editor"],
    ["Workspace files", ".workspace-browser"],
  ] as const;

  for (const theme of ["zero-dark", "zero-light"] as const) {
    await setTheme(page, theme);
    for (const [tabName, surfaceSelector] of surfaces) {
      await page.getByRole("tab", { name: tabName, exact: true }).click();
      const workbench = page.locator(".sessions-page.screen-fit");
      const surface = page.locator(surfaceSelector);
      await expect(workbench).toBeVisible();
      await expect(surface).toBeVisible();
      const geometry = await surface.evaluate((element) => {
        const surface = element.getBoundingClientRect();
        const workspace = element.closest(".session-workspace")!.getBoundingClientRect();
        const main = element.closest(".main-content")!;
        const mainBounds = main.getBoundingClientRect();
        const workbench = element.closest(".sessions-page")!.getBoundingClientRect();
        return {
          mainOverflow: main.scrollHeight - main.clientHeight,
          workbenchBottomGap: mainBounds.bottom - workbench.bottom,
          surfaceBottomGap: workspace.bottom - surface.bottom,
          surfaceHeight: surface.height,
          workspaceHeight: workspace.height,
        };
      });
      expect(geometry.mainOverflow, `${theme} ${tabName} should not make the Workbench page scroll`).toBeLessThanOrEqual(1);
      expect(geometry.workbenchBottomGap, `${theme} ${tabName} should end at the viewport edge`).toBeLessThanOrEqual(1);
      expect(Math.abs(geometry.surfaceBottomGap), `${theme} ${tabName} should fill its workspace`).toBeLessThanOrEqual(1);
      expect(geometry.surfaceHeight, `${theme} ${tabName} should use the available workspace height`).toBeGreaterThanOrEqual(geometry.workspaceHeight - 16);
    }
  }
});

test("the code editor keeps its caret and syntax layers aligned while typing", async ({ page }, testInfo) => {
  test.slow();
  await page.addInitScript(() => localStorage.setItem("nebula.theme", "zero-dark"));
  await page.goto("/?view=code");
  if ((page.viewportSize()?.width ?? 1_000) <= 760) {
    await expect(page.getByRole("navigation", { name: "Mobile operator navigation" })).toBeVisible({ timeout: 15_000 });
    await expect(page.locator(".code-editor-panel")).toBeVisible();
  } else {
    await expect(page.getByRole("tab", { name: "Workspace code editor", exact: true })).toBeVisible({ timeout: 15_000 });
  }
  await page.getByRole("tab", { name: "Changes" }).click();
  await expect(page.getByText("research/mock", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Working diff" }).click();
  const sourceDiff = page.getByRole("dialog", { name: "scanner.py" });
  await expect(sourceDiff.getByLabel("Diff for scanner.py")).toContainText("+return 'changed'");
  await sourceDiff.getByRole("button", { name: "Close source-control diff" }).click();
  await page.getByRole("tab", { name: "Files", exact: true }).click();
  await page.evaluate(() => document.fonts.ready);
  await page.getByRole("button", { name: "New file", exact: true }).first().click();
  const filePath = page.getByRole("textbox", { name: "File path" });
  await filePath.evaluate((input: HTMLInputElement) => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set?.call(input, "example.c");
    input.dispatchEvent(new Event("input", { bubbles: true }));
  });
  await expect(filePath).toHaveValue("example.c");
  await expect(page.locator(".code-mirror-host")).toHaveAttribute("data-language-ready", "example.c");

  const toolbarControls = await page.locator(".code-editor-file-row > label:visible, .code-editor-toolbar button:visible, .code-editor-toolbar .code-editor-dirty:visible").evaluateAll((elements) => elements.map((element) => {
    const box = element.getBoundingClientRect();
    const clip = element.closest(".code-editor-secondary-actions")?.getBoundingClientRect();
    return { label: element.getAttribute("aria-label") || element.textContent?.trim() || element.tagName, left: Math.max(box.left, clip?.left ?? box.left), right: Math.min(box.right, clip?.right ?? box.right), top: box.top, bottom: box.bottom };
  }).filter((box) => box.right - box.left > 1));
  for (let left = 0; left < toolbarControls.length; left += 1) {
    for (let right = left + 1; right < toolbarControls.length; right += 1) {
      const a = toolbarControls[left];
      const b = toolbarControls[right];
      const intersects = a.left < b.right - 1 && a.right > b.left + 1 && a.top < b.bottom - 1 && a.bottom > b.top + 1;
      expect(intersects, `${a.label} overlaps ${b.label}`).toBe(false);
    }
  }

  if ((page.viewportSize()?.width ?? 1_000) <= 760) {
    await page.keyboard.press("Control+Shift+P");
    const palette = page.getByRole("dialog", { name: "Command palette" });
    await palette.getByRole("textbox", { name: "Search commands" }).fill("Editor: Workspace Environment");
    await palette.getByRole("option", { name: /Editor: Workspace Environment/ }).click();
  } else {
    await page.getByRole("button", { name: "Environment", exact: true }).click();
  }
  const environment = page.getByRole("dialog", { name: "Workspace environment" });
  await expect(environment.getByText("Nebula-managed project workspace")).toBeVisible();
  await expect(environment.getByText("Kali runtime ready")).toBeVisible();
  await expect(environment).toContainText("/workspace");
  const environmentAccessibility = await new AxeBuilder({ page }).include(".editor-environment-dialog").analyze();
  expect(environmentAccessibility.violations).toEqual([]);
  await environment.getByRole("button", { name: "Close", exact: true }).click();

  if ((page.viewportSize()?.width ?? 1_000) <= 760) {
    await page.getByRole("button", { name: "More editor actions" }).click();
    const editorOptions = page.getByLabel("Editor options");
    const optionBounds = await editorOptions.locator("label, button").evaluateAll((elements) => elements.map((element) => {
      const bounds = element.getBoundingClientRect();
      return { bottom: bounds.bottom, height: bounds.height, left: bounds.left, right: bounds.right, top: bounds.top };
    }));
    optionBounds.forEach((bounds) => expect(bounds.height).toBeGreaterThanOrEqual(44));
    for (let first = 0; first < optionBounds.length; first += 1) {
      for (let second = first + 1; second < optionBounds.length; second += 1) {
        const left = optionBounds[first];
        const right = optionBounds[second];
        expect(left.left < right.right - 1 && left.right > right.left + 1 && left.top < right.bottom - 1 && left.bottom > right.top + 1).toBe(false);
      }
    }
    await editorOptions.getByRole("button", { name: "Find", exact: true }).click();
  } else {
    await page.getByRole("button", { name: "Find", exact: true }).click();
  }
  const findInput = page.getByRole("textbox", { name: "Find", exact: true });
  await expect(findInput).toBeVisible();
  await findInput.press("Escape");
  await expect(findInput).toBeHidden();

  await page.keyboard.press("Control+Shift+B");
  const compatibleTasks = page.getByRole("dialog", { name: "Project tasks and tests" });
  await expect(compatibleTasks.getByRole("option", { name: /Inspect Python/ })).toBeEnabled();
  await expect(compatibleTasks.getByRole("option", { name: /Extension-owned task/ })).toBeDisabled();
  await expect(compatibleTasks).toContainText("requires a VS Code extension");
  await compatibleTasks.getByRole("button", { name: "Close project tasks" }).click();
  await expect(compatibleTasks).toBeHidden();

  if ((page.viewportSize()?.width ?? 1_000) <= 760) {
    const panel = page.locator(".code-editor-panel");
    await expect(panel).toHaveClass(/has-buffer/);
    await expect(page.getByRole("complementary", { name: "Editor files" })).toBeHidden();
    await expect(page.locator(".code-editor-mobile-title")).toHaveText("Code");
    for (const name of ["More editor actions", "Show editor files", "Save"]) {
      const control = page.getByRole("button", { name, exact: true });
      await expect(control).toBeVisible();
      const box = await control.boundingBox();
      expect(box?.height, name).toBeGreaterThanOrEqual(44);
    }
    const controls = await page.locator(".code-editor-toolbar input:visible, .code-editor-toolbar button:visible, .code-editor-toolbar .code-editor-dirty:visible").evaluateAll((elements) => elements.map((element) => {
      const box = element.getBoundingClientRect();
      return { label: element.getAttribute("aria-label") || element.textContent?.trim() || element.tagName, left: box.left, right: box.right, top: box.top, bottom: box.bottom };
    }));
    for (let left = 0; left < controls.length; left += 1) {
      for (let right = left + 1; right < controls.length; right += 1) {
        const a = controls[left];
        const b = controls[right];
        const intersects = a.left < b.right - 1 && a.right > b.left + 1 && a.top < b.bottom - 1 && a.bottom > b.top + 1;
        expect(intersects, `${a.label} overlaps ${b.label}`).toBe(false);
      }
    }
    const viewport = await page.evaluate(() => ({ client: document.documentElement.clientWidth, scroll: document.documentElement.scrollWidth }));
    expect(viewport.scroll).toBeLessThanOrEqual(viewport.client);

    await page.getByRole("button", { name: "More editor actions" }).click();
    await expect(page.getByLabel("Editor options")).toBeVisible();
    await page.getByRole("button", { name: "Show editor files" }).click();
    await expect(page.getByRole("complementary", { name: "Editor files" })).toBeVisible();
    await page.getByRole("button", { name: "Hide editor files" }).click();
    await expect(page.getByRole("complementary", { name: "Editor files" })).toBeHidden();
    await page.screenshot({ path: testInfo.outputPath("mobile-code-editor.png"), fullPage: true });
  }

  const inputSurface = page.getByRole("textbox", { name: "Code editor" });
  await inputSurface.click({ force: true });
  const editor = inputSurface.locator("..").locator("..");
  const enterText = (text: string) => page.keyboard.insertText(text);
  await enterText("#include <stdio.h>");
  await page.keyboard.press("Enter");
  await expect(page.locator(".cm-line")).toHaveCount(2);
  await inputSurface.click({ force: true });
  await page.keyboard.press("End");
  await page.keyboard.press("Enter");
  await expect(page.locator(".cm-line")).toHaveCount(3);
  await enterText("int main(void) ");
  await page.keyboard.insertText("{");
  await page.keyboard.press("Enter");
  await expect(page.locator(".cm-line")).toHaveCount(5);
  await page.locator(".cm-line").nth(3).click({ force: true });
  await page.keyboard.press("End");
  await inputSurface.focus();
  await expect(inputSurface).toBeFocused();
  await page.keyboard.press("Shift+Tab");
  await expect(page.locator(".cm-activeLine")).toHaveText("");
  await inputSurface.focus();
  await expect(inputSurface).toBeFocused();
  await page.keyboard.press("Tab");
  await expect(page.locator(".cm-activeLine")).toHaveText("  ");
  await inputSurface.focus();
  await enterText("return 0;");
  await expect(page.locator(".cm-activeLine")).toHaveText("  return 0;");
  await page.keyboard.press("Escape");
  await expect(page.getByText("C", { exact: true })).toBeVisible();

  await expect(page.locator(".cm-line")).toHaveCount(5);
  expect(await page.locator(".cm-line").nth(3).textContent()).toBe("  return 0;");
  expect(await page.locator(".cm-line").nth(4).textContent()).toBe("}");
  const syntaxColors = await page.locator(".cm-line").nth(3).evaluate((line) => ({
    line: getComputedStyle(line).color,
    tokens: [...line.querySelectorAll("span")].map((token) => getComputedStyle(token).color),
  }));
  expect(syntaxColors.tokens.some((color) => color !== syntaxColors.line)).toBe(true);
  const geometry = await page.locator(".code-mirror-host").evaluate((host) => {
    const root = host.shadowRoot!;
    const lines = [...root.querySelectorAll<HTMLElement>(".cm-line")].map((line) => line.getBoundingClientRect());
    const numbers = [...root.querySelectorAll<HTMLElement>(".cm-lineNumbers .cm-gutterElement")]
      .filter((element) => Number(element.textContent) > 0 && getComputedStyle(element).visibility !== "hidden")
      .map((number) => number.getBoundingClientRect());
    return {
      hasShadowBoundary: Boolean(root),
      hostHeight: host.getBoundingClientRect().height,
      lineTops: lines.map((line) => line.top),
      numberTops: numbers.map((number) => number.top),
    };
  });
  expect(geometry.hasShadowBoundary).toBe(true);
  const compactEditor = testInfo.project.name === "compact" || (page.viewportSize()?.width ?? 1_000) <= 760;
  expect(geometry.hostHeight).toBeGreaterThan(compactEditor ? 240 : 400);
  expect(geometry.lineTops).toHaveLength(5);
  expect(geometry.numberTops).toHaveLength(5);
  geometry.lineTops.forEach((lineTop, index) => expect(Math.abs(lineTop - geometry.numberTops[index])).toBeLessThan(2));
  await expect(editor).toHaveCSS("outline-style", "none");
  await expect(editor).toHaveCSS("border-top-width", "0px");
  await expect(editor).toHaveCSS("box-shadow", "none");
  await expect(inputSurface).toHaveCSS("outline-style", "none");
  await expect(inputSurface).toHaveCSS("box-shadow", "none");
  await expect(inputSurface).not.toHaveCSS("caret-color", "rgba(0, 0, 0, 0)");
  await expect(page.getByText(/Ln 4, Col 12/, { exact: true })).toBeVisible();
  await expect(editor.locator(".cm-cursor-primary")).toHaveCount(0);

  if ((page.viewportSize()?.width ?? 1_000) <= 760) {
    await page.getByRole("button", { name: "More editor actions" }).click();
    await page.getByRole("button", { name: "Editor settings" }).click();
  } else {
    await page.getByRole("button", { name: "Editor settings" }).click();
  }
  const editorSettings = page.getByRole("dialog", { name: "Editor settings and keybindings" });
  await editorSettings.getByLabel("Font size").selectOption("16");
  await editorSettings.getByLabel("Tab size").selectOption("4");
  await editorSettings.getByRole("checkbox", { name: /Word wrap/ }).check();
  await editorSettings.getByRole("button", { name: "Apply settings" }).click();
  await expect.poll(() => page.locator(".code-mirror-host").evaluate((host) => getComputedStyle(host.shadowRoot!.querySelector(".cm-editor")!).fontSize)).toBe("16px");

  await page.keyboard.press("Control+Shift+P");
  const editorPalette = page.getByRole("dialog", { name: "Command palette" });
  await expect(editorPalette).toBeVisible();
  await editorPalette.getByRole("textbox", { name: "Search commands" }).fill("Editor: Debug Saved Python");
  const unavailableDebugger = editorPalette.getByRole("option", { name: /Editor: Debug Saved Python/ });
  await expect(unavailableDebugger).toBeDisabled();
  await expect(unavailableDebugger).toContainText("Debugging requires an open Python file");
  await expect(unavailableDebugger).toContainText("F5");
  await page.keyboard.press("Escape");
  await expect(editorPalette).toBeHidden();

  await page.getByRole("button", { name: "New editor file" }).click();
  if ((page.viewportSize()?.width ?? 1_000) <= 760) {
    await page.getByRole("button", { name: "More editor actions" }).click();
    await page.getByRole("button", { name: "Split editor" }).click();
  } else {
    await page.getByRole("button", { name: "Split" }).click();
  }
  await expect(page.getByRole("textbox", { name: "Primary code editor: untitled.txt" })).toBeVisible();
  await expect(page.getByRole("textbox", { name: "Secondary code editor: example.c" })).toBeVisible();
  await page.getByRole("button", { name: "Focus example.c editor" }).click();
  await expect(page.getByRole("textbox", { name: "File path" })).toHaveValue("example.c");
  await page.getByRole("button", { name: "Close split editor" }).click();
  await expect(page.getByRole("textbox", { name: "Code editor" })).toBeVisible();
});

test("settings shows the live Kali preparation stage instead of a passive runtime check", async ({ page }) => {
  await page.route("**/api/v1/setup/status", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        core: { status: "ready", detail: null },
        scratch_project_id: "scratch-project",
        terminal: {
          status: "preparing_image",
          runner_profile_id: "local",
          candidates: [],
          image_preparation: {
            phase: "preparing_image",
            operation_id: "00000000-0000-4000-8000-000000000001",
            project_id: "scratch-project",
            progress_percent: null,
            progress_indeterminate: true,
            can_cancel: true,
            can_retry: false,
            detail: "Downloading the official Kali base image.",
          },
          detail: "Downloading the official Kali base image.",
        },
        assistant: { status: "needs_model", provider_profile_id: null, detail: null },
      }),
    });
  });

  await page.goto("/settings");
  await expect(page.getByRole("heading", { name: "Preparing Kali runtime…" })).toBeVisible();
  await expect(page.locator("#setup-settings").getByText("Downloading the official Kali base image.")).toBeVisible();
  await expect(page.getByRole("progressbar", { name: "Kali runtime preparation progress" })).toHaveAttribute(
    "aria-valuetext",
    "Downloading the official Kali base image.",
  );
  await expect(page.getByRole("button", { name: "Cancel", exact: true })).toBeVisible();
  await expect(page.getByText("Downloading and installing the pinned Kali toolset", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "Preparing Kali…" })).toBeDisabled();
});

test("remote Core mode keeps the native Browser and command worker on this desktop", async ({ browser }, testInfo) => {
  test.skip(testInfo.project.name !== "desktop", "Desktop Core selection is a native-shell capability.");
  const context = await browser.newContext({ viewport: { width: 1024, height: 900 }, colorScheme: "dark" });
  const page = await context.newPage();
  await installTruthfulCore(page);
  await page.addInitScript(() => {
    localStorage.setItem("nebula.theme", "zero-dark");
    const calls: string[] = [];
    Object.assign(window, { __NEBULA_REMOTE_CORE_CALLS__: calls });
    Object.assign(window, {
      __TAURI_INTERNALS__: {
        invoke: async (command: string) => {
          calls.push(command);
          if (command === "resolve_backend_connection") return { endpoint: `${location.origin}/api/v1`, token: "", protocol: "nebula-remote-core-v1", source: "remote" };
          if (command === "desktop_core_connection") return { mode: "remote", endpoint: `${location.origin}/api/v1`, tokenAvailable: true, deviceId: "desktop-remote-test" };
          if (command === "desktop_device_id") return "desktop-remote-test";
          if (command === "browser_capabilities") return {
            engine: "Remote-Core local native mock",
            projectStorage: "persistent",
            identityPartitions: true,
            devtools: true,
            interceptionProxy: true,
            http2Capture: true,
            websocketCapture: true,
            autonomousCommands: true,
          };
          if (command === "browser_proxy_ca_status") return {
            certificatePath: "/protected/project-ca.pem",
            fingerprint: "aa:bb:cc:dd",
            state: "generated",
            trustInstructions: "Trust this Project CA on the desktop before intercepting HTTPS.",
          };
          return undefined;
        },
        transformCallback: () => 1,
        unregisterCallback: () => undefined,
        convertFileSrc: (value: string) => value,
      },
    });
  });

  await openWorkspace(page, "/settings", "Settings");
  const card = page.locator(".core-connection-settings");
  await expect(card.getByRole("heading", { name: "Remote Core selected" })).toBeVisible();
  await expect(card.getByText(/Native execution stays on this desktop/)).toBeVisible();
  const geometry = await card.evaluate((element) => {
    const cardRect = element.getBoundingClientRect();
    const controls = [...element.querySelectorAll<HTMLElement>("input, button")].map((control) => control.getBoundingClientRect());
    return {
      card: { left: cardRect.left, right: cardRect.right, width: cardRect.width },
      scrollWidth: element.scrollWidth,
      clientWidth: element.clientWidth,
      controls: controls.map((rect) => ({ left: rect.left, right: rect.right, top: rect.top, bottom: rect.bottom })),
    };
  });
  expect(geometry.scrollWidth).toBeLessThanOrEqual(geometry.clientWidth + 1);
  for (const control of geometry.controls) {
    expect(control.left).toBeGreaterThanOrEqual(geometry.card.left - 1);
    expect(control.right).toBeLessThanOrEqual(geometry.card.right + 1);
  }
  for (let index = 1; index < geometry.controls.length; index += 1) {
    const previous = geometry.controls[index - 1];
    const current = geometry.controls[index];
    expect(current.top >= previous.bottom - 1 || current.left >= previous.right - 1).toBe(true);
  }
  await expect.poll(() => page.evaluate(() => (window as Window & { __NEBULA_REMOTE_CORE_CALLS__?: string[] }).__NEBULA_REMOTE_CORE_CALLS__?.includes("desktop_device_id"))).toBe(true);

  await page.goto("/");
  const ownershipRequest = page.waitForRequest((request) => request.method() === "PUT" && request.url().includes("/browser-sessions/browser-session-preview/tabs"));
  await page.getByRole("tab", { name: "Project browser", exact: true }).click();
  await expect(page.getByRole("tablist", { name: "Browser tabs" })).toBeVisible();
  await expect(page.getByRole("textbox", { name: "Start browsing" })).toBeEnabled();
  expect((await ownershipRequest).postDataJSON()).toMatchObject({ device_owner: "desktop-remote-test" });
  const nativeCalls = await page.evaluate(() => (window as Window & { __NEBULA_REMOTE_CORE_CALLS__?: string[] }).__NEBULA_REMOTE_CORE_CALLS__ ?? []);
  expect(nativeCalls).toContain("browser_capabilities");
  expect(nativeCalls).toContain("desktop_device_id");
  await context.close();
});

test("terminal and notes keep a visible focused caret", async ({ page }, testInfo) => {
  test.setTimeout(60_000);
  await page.addInitScript(() => localStorage.setItem("nebula.theme", "zero-dark"));
  await openWorkspace(page, "/", "Workbench");

  const terminalSurface = page.locator(".xterm-shell").first();
  await terminalSurface.click({ force: true });
  await expect(page.locator(".xterm").first()).toHaveCSS("cursor", "text");
  const terminalInput = page.getByRole("textbox", { name: "Terminal input" }).first();
  await terminalInput.click({ force: true });
  await expect(terminalInput).toBeFocused();

  if ((page.viewportSize()?.width ?? 1_000) <= 760) {
    await page.getByRole("button", { name: "More workbench views" }).click();
    await page.getByRole("dialog", { name: "More views" }).getByRole("button", { name: /Notes/ }).click();
  } else {
    await page.getByRole("tab", { name: "Project notes", exact: true }).click();
  }
  await page.getByRole("button", { name: "New note", exact: true }).click();
  const noteBody = page.getByRole("textbox", { name: "Note body" });
  await noteBody.click();
  await expect(noteBody).toBeFocused();
  const caretColor = await noteBody.evaluate((element) => getComputedStyle(element).caretColor);
  expect(caretColor).not.toBe("auto");
  expect(caretColor).not.toBe("rgba(0, 0, 0, 0)");
});

test("the populated finding editor stays contained and accessible", async ({ page }) => {
  const finding = {
    ...entity,
    id: "finding-editor",
    engagement_id: "scratch-project",
    title: "Externally reachable script injection",
    description: "Untrusted search input is reflected into an executable response context.",
    severity: "high",
    severity_rationale: "An unauthenticated remote user can execute script in another user's session.",
    status: "validated",
    asset_ids: ["asset-editor"],
    evidence_ids: ["evidence-editor"],
    cve_ids: ["CVE-2026-1234"],
    cwe_ids: ["CWE-79"],
    verifier_id: null,
    verified_at: null,
  };
  await page.route("**/api/v1/findings**", async (route) => {
    if (route.request().method() !== "GET") return route.fallback();
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify([finding]) });
  });
  await page.route("**/api/v1/assets**", async (route) => {
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify([{
      ...entity,
      id: "asset-editor",
      engagement_id: "scratch-project",
      asset_type: "domain",
      name: "portal.example.test",
      address: null,
      hostname: "portal.example.test",
      criticality: "high",
      exposed: true,
      tags: [],
      metadata: {},
    }]) });
  });
  await page.route("**/api/v1/evidence**", async (route) => {
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify([{
      ...entity,
      id: "evidence-editor",
      engagement_id: "scratch-project",
      evidence_type: "operator_upload",
      title: "browser-response.html",
      description: "Captured response",
      artifact_id: null,
      finding_id: "finding-editor",
      asset_ids: ["asset-editor"],
      sha256: "a".repeat(64),
      captured_at: entity.updated_at,
      captured_by: null,
      source_version: null,
      metadata: {},
    }]) });
  });

  await openWorkspace(page, "/findings", "Findings");
  await page.getByRole("button", { name: "Edit Externally reachable script injection" }).click();
  const inspector = page.getByRole("complementary", { name: "Externally reachable script injection" });
  await expect(inspector).toBeVisible();
  await expect(inspector.getByLabel("Title")).toHaveValue("Externally reachable script injection");
  await expect(inspector.getByRole("button", { name: "Save finding" })).toBeDisabled();

  const containment = await inspector.evaluate((element) => {
    const bounds = element.getBoundingClientRect();
    const clipped = [...element.querySelectorAll<HTMLElement>("input, textarea, select, button, fieldset, footer")]
      .filter((control) => {
        const rect = control.getBoundingClientRect();
        return rect.width > 0 && (rect.left < bounds.left - 2 || rect.right > bounds.right + 2);
      })
      .map((control) => control.getAttribute("aria-label") || control.textContent?.trim().slice(0, 40) || control.tagName);
    return { horizontalOverflow: element.scrollWidth > element.clientWidth + 2, clipped };
  });
  expect(containment).toEqual({ horizontalOverflow: false, clipped: [] });
  const results = await new AxeBuilder({ page }).include(".finding-dialog").withTags(["wcag2a", "wcag2aa"]).analyze();
  expect(results.violations, results.violations.map((violation) => `${violation.id}: ${violation.help}`).join("\n")).toEqual([]);
});

test("Project Overview empty activity keeps its copy in a readable content track", async ({ page }) => {
  await openWorkspace(page, "/project", "Scratch Project");
  const emptyState = page.locator(".mission-events-empty");
  await expect(emptyState).toBeVisible();
  await expect(emptyState.getByText("No mission activity", { exact: true })).toBeVisible();
  await expect(emptyState.getByText("Events appear after Core records a transition.", { exact: true })).toBeVisible();
  const geometry = await emptyState.evaluate((element) => {
    const container = element.getBoundingClientRect();
    const copy = element.querySelector<HTMLElement>("div")!.getBoundingClientRect();
    const detailElement = element.querySelector<HTMLElement>("small")!;
    const detail = detailElement.getBoundingClientRect();
    const detailStyle = getComputedStyle(detailElement);
    const lineHeight = Number.parseFloat(detailStyle.lineHeight) || Number.parseFloat(detailStyle.fontSize) * 1.2;
    return {
      containerWidth: container.width,
      copyWidth: copy.width,
      detailWidth: detail.width,
      detailLines: detail.height / lineHeight,
      clipped: element.scrollWidth > element.clientWidth + 2,
      renderedInsideListGrid: element.closest("li") !== null,
    };
  });
  expect(geometry.containerWidth).toBeGreaterThan(250);
  expect(geometry.copyWidth).toBeGreaterThan(140);
  expect(geometry.detailWidth).toBeGreaterThan(140);
  expect(geometry.detailLines).toBeLessThanOrEqual(2.1);
  expect(geometry.clipped).toBe(false);
  expect(geometry.renderedInsideListGrid).toBe(false);
  expect(await findPathologicalText(page)).toEqual([]);
});

test("top toolbar controls do not collide at compact breakpoint edges", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "desktop", "Explicit breakpoint coverage only needs one browser project.");
  for (const width of [900, 768]) {
    await page.setViewportSize({ width, height: 800 });
    await openWorkspace(page, "/project", "Scratch Project");
    await expect(page.locator(".top-bar")).toBeVisible();
    const issues = await page.locator(".top-bar").evaluate((toolbar) => {
      const tolerance = 1;
      const toolbarRect = toolbar.getBoundingClientRect();
      const groups = [...toolbar.children]
        .filter((child): child is HTMLElement => child instanceof HTMLElement)
        .filter((child) => {
          const rect = child.getBoundingClientRect();
          return rect.width > 0 && rect.height > 0 && getComputedStyle(child).visibility !== "hidden";
        });
      const controls = [...toolbar.querySelectorAll<HTMLElement>("button, a")]
        .filter((control) => {
          const rect = control.getBoundingClientRect();
          return rect.width > 0 && rect.height > 0 && getComputedStyle(control).visibility !== "hidden";
        });
      const problems: string[] = [];
      for (const element of [...groups, ...controls]) {
        const rect = element.getBoundingClientRect();
        if (rect.left < toolbarRect.left - tolerance || rect.right > toolbarRect.right + tolerance
          || rect.top < toolbarRect.top - tolerance || rect.bottom > toolbarRect.bottom + tolerance) {
          problems.push(`${element.className || element.tagName} escapes toolbar bounds`);
        }
      }
      for (let index = 0; index < groups.length; index += 1) {
        const first = groups[index].getBoundingClientRect();
        for (let next = index + 1; next < groups.length; next += 1) {
          const second = groups[next].getBoundingClientRect();
          const overlapX = Math.min(first.right, second.right) - Math.max(first.left, second.left);
          const overlapY = Math.min(first.bottom, second.bottom) - Math.max(first.top, second.top);
          if (overlapX > tolerance && overlapY > tolerance) {
            problems.push(`${groups[index].className} overlaps ${groups[next].className} by ${Math.round(overlapX)}px`);
          }
        }
      }
      const actionHost = toolbar.querySelector<HTMLElement>(".top-bar-page-actions");
      if (actionHost) {
        const hostRect = actionHost.getBoundingClientRect();
        for (const control of actionHost.querySelectorAll<HTMLElement>("button, a")) {
          const rect = control.getBoundingClientRect();
          if (rect.width > 0 && (rect.left < hostRect.left - tolerance || rect.right > hostRect.right + tolerance)) {
            problems.push(`${control.textContent?.trim() || control.className} is clipped by the page-action track`);
          }
        }
      }
      return problems;
    });
    expect(issues, `${width}px toolbar has overlapping or clipped children`).toEqual([]);
  }
});

for (const theme of ["zero-dark", "zero-light"] as const) {
  test(`critical workspaces meet automated accessibility checks in ${theme} mode`, async ({ page }) => {
    test.setTimeout(60_000);
    await openWorkspace(page, "/", "Workbench");
    await setTheme(page, theme);
    for (const [, route, heading] of workspaces) {
      await openWorkspace(page, route, heading);
      await expect(page.locator("html")).toHaveAttribute("data-theme", theme);
      const results = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa"]).analyze();
      expect(results.violations, results.violations.map((violation) => `${violation.id}: ${violation.help}`).join("\n")).toEqual([]);
      const undersizedText = await page.locator("body").evaluate(() => [...document.querySelectorAll<HTMLElement>("body *")]
        .filter((element) => [...element.childNodes].some((node) => node.nodeType === Node.TEXT_NODE && node.textContent?.trim()))
        .filter((element) => element.getClientRects().length > 0 && getComputedStyle(element).visibility !== "hidden")
        .filter((element) => Number.parseFloat(getComputedStyle(element).fontSize) < 11)
        .map((element) => `${element.tagName.toLowerCase()}.${element.className}:"${element.textContent?.trim().slice(0, 60)}":${getComputedStyle(element).fontSize}`));
      expect(undersizedText, `${theme} ${route} renders text below 11px`).toEqual([]);
      {
        const overflow = await page.locator("body").evaluate(() => {
          const selectors = [
            ".page",
            ".metric-grid",
            ".session-toolbar",
            ".session-workspace",
            ".project-tabs",
            ".settings-tabs",
            ".finding-summary-grid",
            ".summary-strip",
            ".data-toolbar",
            ".overview-grid",
          ].join(", ");
          const clipped = [...document.querySelectorAll<HTMLElement>(selectors)]
            .filter((element) => {
              const rect = element.getBoundingClientRect();
              const style = getComputedStyle(element);
              return rect.width > 0 && rect.height > 0 && style.display !== "none" && element.scrollWidth > element.clientWidth + 2;
            })
            .map((element) => `${element.tagName.toLowerCase()}.${element.className}: ${element.clientWidth}/${element.scrollWidth}`);
          if (document.documentElement.scrollWidth > window.innerWidth + 1) clipped.push(`document: ${window.innerWidth}/${document.documentElement.scrollWidth}`);
          return clipped;
        });
        expect(overflow, `${theme} ${route} contains unintended horizontal overflow`).toEqual([]);
      }
    }
  });
}

test("Zero keeps its themed shell without the removed context deck", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "desktop", "The full shell contract only needs one desktop browser project.");
  await page.addInitScript(() => localStorage.setItem("nebula.theme", "zero-dark"));
  await openWorkspace(page, "/", "Workbench");

  await expect(page.locator(".app-shell.zero-layer-shell")).toHaveCount(1);
  await expect(page.locator(".zero-route-flare, .zero-anchor-dock, .zero-status-band")).toHaveCount(3);
  await expect(page.getByRole("region", { name: "Zero Layer context" })).toHaveCount(0);

  await page.getByRole("button", { name: "Search commands" }).click();
  await expect(page.getByRole("dialog", { name: "Command palette" })).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(page.getByRole("dialog", { name: "Command palette" })).toBeHidden();
  await setTheme(page, "zero-light");
  await expect(page.locator(".app-shell.zero-layer-shell")).toHaveCount(1);
  await expect(page.locator(".zero-route-flare, .zero-anchor-dock, .zero-status-band")).toHaveCount(3);
});

test("Zero keeps one navigable panoramic shell at every breakpoint", async ({ page }, testInfo) => {
  await page.addInitScript(() => localStorage.setItem("nebula.theme", "zero-dark"));
  await openWorkspace(page, "/", "Workbench");
  const mobile = (page.viewportSize()?.width ?? 1440) <= 760;

  await expect(page.getByRole("region", { name: "Zero Layer context" })).toHaveCount(0);
  await expect(page.locator("main#main-content")).toHaveCount(1);
  if (mobile) {
    await expect(page.getByRole("complementary", { name: "Primary navigation" })).toBeHidden();
    const navigation = page.getByRole("navigation", { name: "Mobile operator navigation" });
    await expect(navigation).toBeVisible();
    for (const label of ["Chat", "Open conversations", "Activity", "More workbench views"]) {
      await expect(navigation.getByRole("button", { name: label, exact: true })).toBeVisible();
    }
  } else {
    await expect(page.getByRole("complementary", { name: "Primary navigation" })).toHaveCount(1);
    for (const label of ["Workbench", "Findings", "Reports", "Project", "Settings"]) {
      await expect(page.getByRole("complementary", { name: "Primary navigation" }).getByRole("link", { name: label, exact: true })).toBeVisible();
    }
  }

  const geometry = await page.locator(".app-shell").evaluate((shell, mobileView) => {
    const viewport = { width: window.innerWidth, height: window.innerHeight };
    const bounds = (selector: string) => {
      const rect = shell.querySelector<HTMLElement>(selector)!.getBoundingClientRect();
      return { top: rect.top, right: rect.right, bottom: rect.bottom, left: rect.left, width: rect.width, height: rect.height };
    };
    return {
      viewport,
      shellOverflow: document.documentElement.scrollWidth > viewport.width || document.documentElement.scrollHeight > viewport.height,
      status: bounds(".top-bar"),
      main: bounds(".main-content"),
      navigation: bounds(mobileView ? ".mobile-companion-nav" : ".side-nav"),
    };
  }, mobile);
  expect(geometry.shellOverflow).toBe(false);
  expect(geometry.main.top - geometry.status.bottom).toBeLessThanOrEqual(10);
  for (const surface of [geometry.main, geometry.navigation]) {
    expect(surface.left).toBeGreaterThanOrEqual(0);
    expect(surface.right).toBeLessThanOrEqual(geometry.viewport.width + 1);
    expect(surface.top).toBeGreaterThanOrEqual(0);
    expect(surface.bottom).toBeLessThanOrEqual(geometry.viewport.height + 1);
    expect(surface.width).toBeGreaterThan(0);
    expect(surface.height).toBeGreaterThan(0);
  }

  const workbenchLink = mobile
    ? page.getByRole("navigation", { name: "Mobile operator navigation" }).getByRole("button", { name: "Chat", exact: true })
    : page.getByRole("complementary", { name: "Primary navigation" }).getByRole("link", { name: "Workbench", exact: true });
  if (testInfo.project.name.startsWith("mobile-webkit")) {
    const touchBounds = await workbenchLink.evaluate((element) => {
      const bounds = element.getBoundingClientRect();
      return { width: bounds.width, height: bounds.height };
    });
    expect(touchBounds.width).toBeGreaterThanOrEqual(44);
    expect(touchBounds.height).toBeGreaterThanOrEqual(44);
  } else {
    await workbenchLink.focus();
    const focusStyle = await workbenchLink.evaluate((element) => {
      const style = getComputedStyle(element);
      return { style: style.outlineStyle, width: Number.parseFloat(style.outlineWidth), color: style.outlineColor };
    });
    expect(focusStyle.style).toBe("solid");
    expect(focusStyle.width).toBeGreaterThanOrEqual(2);
    expect(focusStyle.color).not.toBe("rgba(0, 0, 0, 0)");
    await workbenchLink.evaluate((element) => element.blur());
  }

  if (testInfo.project.name !== "desktop") {
    await expect(page).toHaveScreenshot("workbench-zero-dark-responsive.png", { fullPage: true });
  }

  const persistentSurface = mobile ? page.locator(".sessions-page") : page.locator(".persistent-terminal");
  await expect(persistentSurface).toBeVisible();
  await persistentSurface.evaluate((element) => { (window as typeof window & { __zeroTerminal?: Element }).__zeroTerminal = element; });
  await page.getByRole("button", { name: "Search commands" }).click();
  await page.getByRole("textbox", { name: "Search commands" }).fill("Zero Light theme");
  await page.getByRole("option", { name: /Use Zero Light theme/ }).click();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "zero-light");
  expect(await persistentSurface.evaluate((element) => (window as typeof window & { __zeroTerminal?: Element }).__zeroTerminal === element)).toBe(true);
});

for (const [name, route, heading] of workspaces) {
  test(`Zero Dark preserves the ${name} desktop hierarchy`, async ({ page }, testInfo) => {
    test.skip(testInfo.project.name !== "desktop", "Zero Dark visual baselines are captured at the reference desktop size.");
    await page.addInitScript(() => localStorage.setItem("nebula.theme", "zero-dark"));
    await openWorkspace(page, route, heading);
    await expect(page.locator("html")).toHaveAttribute("data-theme", "zero-dark");
    await page.waitForTimeout(360);
    await expect(page).toHaveScreenshot(`${name}-zero-dark.png`, { fullPage: true });
  });
}

test("Zero Dark preserves representative desktop overlays", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "desktop", "Zero Dark visual baselines are captured at the reference desktop size.");
  await page.addInitScript(() => localStorage.setItem("nebula.theme", "zero-dark"));
  await openWorkspace(page, "/", "Workbench");
  await page.getByRole("button", { name: "Search commands" }).click();
  await expect(page.getByRole("dialog", { name: "Command palette" })).toBeVisible();
  await expect(page).toHaveScreenshot("workbench-zero-dark-command-palette.png", { fullPage: true });

  await page.keyboard.press("Escape");
  await page.keyboard.press("Control+Alt+i");
  await expect(page.getByRole("complementary", { name: "Activity inspector" })).toBeVisible();
  await expect(page).toHaveScreenshot("workbench-zero-dark-activity-drawer.png", { fullPage: true });
  await page.getByRole("button", { name: "Close activity center" }).click();
});

test("Zero Dark preserves a representative resource dialog", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "desktop", "Zero Dark visual baselines are captured at the reference desktop size.");
  await page.addInitScript(() => localStorage.setItem("nebula.theme", "zero-dark"));
  await openWorkspace(page, "/findings", "Findings");
  await page.waitForTimeout(360);
  await page.getByRole("button", { name: "New finding" }).click();
  await expect(page.getByRole("dialog", { name: "Create candidate finding" })).toBeVisible();
  await expect(page).toHaveScreenshot("findings-zero-dark-dialog.png", { fullPage: true });
  await page.getByRole("button", { name: "Close candidate finding dialog" }).click();
});

test("Zero Dark preserves the appearance selector", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "desktop", "Zero Dark visual baselines are captured at the reference desktop size.");
  await page.addInitScript(() => localStorage.setItem("nebula.theme", "zero-dark"));
  await openWorkspace(page, "/settings#appearance-settings", "Settings");
  await expect(page.getByRole("link", { name: "Advanced settings" })).toHaveAttribute("aria-current", "page");
  await expect(page.locator(".appearance-panel")).toBeVisible();
  await expect(page.locator(".appearance-panel")).toHaveScreenshot("settings-zero-dark-appearance.png");
});

test("advanced settings keeps the binary inventory collapsed until requested", async ({ page }) => {
  await page.route("**/api/v1/automation/runtime", async (route) => route.fulfill({
    status: 200,
    contentType: "application/json",
    body: JSON.stringify({
      configured: true,
      ready: true,
      detail: "Prepared runtime is ready",
      digest: `sha256:${"a".repeat(64)}`,
      runner_profile_id: "local",
      inventory: [
        { name: "nmap", path: "/usr/bin/nmap", version: "7.95" },
        { name: "sqlmap", path: "/usr/bin/sqlmap", version: "1.8" },
      ],
    }),
  }));
  await openWorkspace(page, "/settings#automation-settings", "Settings");
  await expect(page.getByRole("link", { name: "Advanced settings", exact: true })).toHaveAttribute("aria-current", "page");

  const inventory = page.locator("details.inventory-disclosure");
  await expect(inventory).not.toHaveAttribute("open", "");
  await expect(inventory.getByText("2", { exact: true })).toBeVisible();
  await expect(inventory.getByText("nmap", { exact: true })).toBeHidden();
  await inventory.locator("summary").click();
  await expect(inventory).toHaveAttribute("open", "");
  await expect(inventory.getByText("nmap", { exact: true })).toBeVisible();
});

test("tool follow-up runtime lives in Settings and its Workbench toggles persist", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name === "narrow", "Compact Workbench toolbar toggles are intentionally desktop-only.");
  let postToolConfig = {
    suggest_next_steps: false,
    take_notes: false,
    backend_kind: "harness",
    provider_id: null,
    harness_profile_id: "harness-1",
    model: "gpt-5-codex",
    cloud_confirmed: false,
  };
  const harness = {
    ...entity,
    id: "harness-1",
    name: "Codex harness",
    kind: "codex_app_server",
    connection_mode: "spawn",
    transport: "stdio",
    executable: "codex",
    endpoint: null,
    auth_mode: "existing_session",
    secret_ref: null,
    default_model: "gpt-5-codex",
    enabled: true,
    privacy: { local_only: true, permits_sensitive_data: true },
    native_capabilities: { workspace_access: "write", shell: true, web_search: true, skills: true },
    capabilities: { models: ["gpt-5-codex"], checked_at: entity.updated_at, harness_version: "1.0" },
  };
  await page.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path.endsWith("/harnesses")) {
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify([harness]) });
      return;
    }
    if (path.endsWith("/engagements/scratch-project/post-tool-assistant")) {
      if (request.method() === "PUT") postToolConfig = request.postDataJSON() as typeof postToolConfig;
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(postToolConfig) });
      return;
    }
    if (path.endsWith("/engagements/scratch-project/post-tool-results")) {
      await route.fulfill({ status: 200, contentType: "application/json", body: "[]" });
      return;
    }
    await route.fallback();
  });

  await openWorkspace(page, "/", "Workbench");
  await expect(page.getByRole("combobox", { name: "Post-tool analysis backend" })).toHaveCount(0);
  await page.getByRole("button", { name: "More Workbench actions" }).click();
  await page.getByRole("menuitem", { name: "Tool assistance" }).click();
  const suggestions = page.getByRole("checkbox", { name: "Suggest next steps" });
  await suggestions.click();
  await expect(suggestions).toBeChecked();
  await expect.poll(() => postToolConfig.suggest_next_steps).toBe(true);
  const enabledFeedback = page.getByRole("status");
  await expect(enabledFeedback).toContainText("Next-step suggestions enabled");
  await expect(enabledFeedback.getByRole("link", { name: "Open Settings" })).toHaveCount(0);
  if (testInfo.project.name === "desktop") await expect(page.locator(".session-toolbar")).toHaveScreenshot("tool-follow-up-workbench-toolbar.png");

  await openWorkspace(page, "/settings#post-tool-assistant-settings", "Settings");
  const panel = page.locator("#post-tool-assistant-settings");
  await expect(panel).toBeVisible();
  await expect(panel.getByRole("combobox", { name: "Tool follow-up runtime" })).toHaveValue("harness:harness-1");
  await expect(panel.getByLabel("Tool follow-up model")).toHaveValue("gpt-5-codex");
  expect(await findPathologicalText(page)).toEqual([]);
  if (testInfo.project.name === "desktop") await expect(panel).toHaveScreenshot("tool-follow-up-settings.png");
});

test("tool follow-up toggles explain missing runtime setup", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name === "narrow", "Compact Workbench toolbar toggles are intentionally desktop-only.");
  let postToolConfig = {
    suggest_next_steps: false,
    take_notes: false,
    backend_kind: "provider" as const,
    provider_id: null,
    harness_profile_id: null,
    model: null,
    cloud_confirmed: false,
  };
  await page.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path.endsWith("/engagements/scratch-project/post-tool-assistant")) {
      if (request.method() === "PUT") postToolConfig = request.postDataJSON() as typeof postToolConfig;
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(postToolConfig) });
      return;
    }
    if (path.endsWith("/engagements/scratch-project/post-tool-results")) {
      await route.fulfill({ status: 200, contentType: "application/json", body: "[]" });
      return;
    }
    await route.fallback();
  });

  await openWorkspace(page, "/", "Workbench");
  await page.getByRole("button", { name: "More Workbench actions" }).click();
  await page.getByRole("menuitem", { name: "Tool assistance" }).click();
  const notes = page.getByRole("checkbox", { name: "Take notes" });
  await notes.click();

  await expect(notes).not.toBeChecked();
  expect(postToolConfig.take_notes).toBe(false);
  const feedback = page.getByRole("alert");
  await expect(feedback).toContainText("Analysis runtime required");
  await expect(feedback).toContainText("Choose an enabled model provider or agent harness in Settings");
  await expect(feedback.getByRole("link", { name: "Open Settings" })).toBeVisible();
  await expect(feedback).toHaveScreenshot("tool-follow-up-runtime-required.png");
});

test("Zero Light preserves each critical workspace hierarchy", async ({ page }) => {
  test.setTimeout(60_000);
  for (const theme of ["zero-light"] as const) {
    await openWorkspace(page, "/", "Workbench");
    await setTheme(page, theme);
    for (const [name, route, heading] of workspaces) {
      await openWorkspace(page, route, heading);
      await expect(page.locator("html")).toHaveAttribute("data-theme", theme);
      await expect(page).toHaveScreenshot(`${name}-${theme}.png`, { fullPage: true });
    }
  }
});

test("audit every primary workspace view", async ({ page }, testInfo) => {
  test.setTimeout(150_000);
  if (testInfo.project.name === "desktop") {
    await page.setViewportSize({ width: 1756, height: 1194 });
  }

  const capture = async (name: string) => {
    await page.waitForTimeout(120);
    const overflow = await page.locator("body").evaluate(() => {
      const selector = [
        ".page",
        ".session-toolbar",
        ".session-layout",
        ".chat-context-bar",
        ".execution-history",
        ".workspace-browser",
        ".notes-panel",
        ".artifact-grid",
        ".report-empty-state",
        ".project-tabs",
        ".settings-tabs",
      ].join(", ");
      return [...document.querySelectorAll<HTMLElement>(selector)]
        .filter((element) => {
          const rect = element.getBoundingClientRect();
          const style = getComputedStyle(element);
          return rect.width > 0
            && rect.height > 0
            && style.display !== "none"
            && element.scrollWidth > element.clientWidth + 2;
        })
        .map((element) => `${element.tagName.toLowerCase()}.${element.className}: ${element.clientWidth}/${element.scrollWidth}`);
    });
    expect(overflow, `${name} contains horizontally clipped UI`).toEqual([]);
    expect(await findPathologicalText(page), `${name} renders prose in a pathologically narrow column`).toEqual([]);
    await page.screenshot({ path: testInfo.outputPath(`${name}.png`), fullPage: true });
  };

  const mobile = (page.viewportSize()?.width ?? 1440) <= 760;
  await openWorkspace(page, "/", "Workbench");
  for (const [name, label, view] of [
    ["workbench-terminal", "Terminal", "terminal"],
    ["workbench-code", "Workspace code editor", "code"],
    ["workbench-browser", "Project browser", "browser"],
    ["workbench-assistant", "Analyst chat", "chat"],
    ["workbench-files", "Workspace files", "workspace"],
    ["workbench-notes", "Project notes", "notes"],
    ["workbench-missions", "Autonomous missions", "missions"],
    ["workbench-activity", "Activity history", "activity"],
  ] as const) {
    if (mobile) await openWorkspace(page, `/?view=${view}`, "Workbench");
    else await page.getByRole("tab", { name: label, exact: true }).click();
    await capture(name);
  }

  for (const [name, route, heading] of [
    ["findings", "/findings", "Findings"],
    ["reports", "/reports", "Reports"],
    ["library", "/library", "Library"],
  ] as const) {
    await openWorkspace(page, route, heading);
    await capture(name);
  }

  await openWorkspace(page, "/project", "Scratch Project");
  for (const [name, label] of [
    ["project-overview", "Overview"],
    ["project-assets", "Assets"],
    ["project-evidence", "Evidence"],
    ["project-sources", "Sources"],
  ] as const) {
    await page.getByRole("button", { name: label, exact: true }).click();
    await capture(name);
  }

  await openWorkspace(page, "/settings", "Settings");
  for (const [name, label] of [
    ["settings-setup", "Setup"],
    ["settings-advanced", "Advanced settings"],
    ["settings-diagnostics", "Diagnostics settings and recent errors"],
  ] as const) {
    await page.getByRole("link", { name: label, exact: true }).click();
    await capture(name);
  }

  await page.getByRole("link", { name: "Advanced settings", exact: true }).click();
  for (const [name, label] of [
    ["settings-models", "Models"],
    ["settings-automation", "Automation"],
    ["settings-project-policy", "Project Policy"],
    ["settings-identity-security", "Identity & Security"],
    ["settings-release", "Release"],
  ] as const) {
    await page.locator("details.settings-group > summary", { hasText: label }).click();
    await expect(page.locator("details.settings-group[open]")).toHaveCount(1);
    await capture(name);
  }

  await page.locator("details.settings-group > summary", { hasText: "Models" }).click();
  const addProvider = page.getByRole("button", { name: "Add provider" });
  if (await addProvider.isEnabled()) {
    await addProvider.click();
    await expect(page.getByRole("dialog", { name: "Add model provider" })).toBeVisible();
    await capture("dialog-provider");
  }
});

test("mobile Workbench navigation has one authority and no duplicate tab strip", async ({ page }) => {
  test.skip((page.viewportSize()?.width ?? 1440) > 760, "Mobile navigation contract");
  await openWorkspace(page, "/?view=activity", "Workbench");

  await expect(page.locator(".session-tabs")).toBeHidden();
  const navigation = page.getByRole("navigation", { name: "Mobile operator navigation" });
  await expect(navigation.locator('[aria-current="page"]')).toHaveCount(1);
  await expect(navigation.getByRole("button", { name: "Activity" })).toHaveAttribute("aria-current", "page");

  await navigation.getByRole("button", { name: "More workbench views" }).click();
  await expect(navigation.locator('[aria-current="page"]')).toHaveCount(1);
  await expect(navigation.getByRole("button", { name: "More workbench views" })).toHaveAttribute("aria-current", "page");
  await expect(navigation.getByRole("button", { name: "Activity" })).not.toHaveAttribute("aria-current", "page");
  await expect(page.locator(".session-toolbar")).toBeHidden();
  await expect(page.getByRole("dialog", { name: "More views" }).getByRole("button", { name: "Enter focus mode" })).toBeVisible();

  await page.getByRole("dialog", { name: "More views" }).getByRole("button", { name: /Terminal/ }).click();
  await expect(page).toHaveURL(/view=terminal/);
  await expect(page.getByText("Connected", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "Screenshot" })).toBeVisible();
  await expect(page.getByText("Continue this view on desktop", { exact: true })).toHaveCount(0);
  await expect(navigation.locator('[aria-current="page"]')).toHaveCount(1);
  await expect(navigation.getByRole("button", { name: "More workbench views" })).toHaveAttribute("aria-current", "page");

  await navigation.getByRole("button", { name: "More workbench views" }).click();
  await page.getByRole("dialog", { name: "More views" }).getByRole("button", { name: /Code/ }).click();
  await expect(page).toHaveURL(/view=code/);
  await expect(page.locator(".code-editor-panel")).toBeVisible();

  await navigation.getByRole("button", { name: "More workbench views" }).click();
  await page.getByRole("dialog", { name: "More views" }).getByRole("button", { name: /Browser/ }).click();
  await expect(page).toHaveURL(/view=browser/);
  await expect(page.getByText("Browse from this device", { exact: true })).toBeVisible();
  await expect(page.getByText(/No target · Open a page to compare it with Project scope/)).toBeVisible();
  await expect(page.getByRole("textbox", { name: "Web address" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Add to Sources" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Ask Nebula about the live page" })).toHaveCount(0);
});

test("browser research tools expose durable workflows on paired clients", async ({ page }) => {
  await openWorkspace(page, "/?view=browser&browserTool=repeater", "Workbench");
  await page.getByRole("button", { name: "Research workbench" }).click();
  await expect(page.getByRole("heading", { name: "Repeater" })).toBeVisible();
  await expect(page.getByText("Profile lookup", { exact: true })).toBeVisible();
  await page.getByText("Profile lookup", { exact: true }).click();
  await expect(page.getByLabel("Headers JSON")).toHaveValue('{\n  "Accept": "application/json"\n}');
  await expect(page.getByRole("button", { name: "Send once" })).toBeDisabled();
  await expect(page.getByText(/paired desktop performs sends/i)).toBeVisible();
  await page.getByText("Result history (1)").click();
  await expect(page.getByText("128 bytes", { exact: false })).toBeVisible();

  await page.getByRole("button", { name: "Intruder", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Intruder" })).toBeVisible();
  await expect(page.getByLabel("Position names")).toBeVisible();
  await expect(page.locator("label", { hasText: "Payload sets" }).locator("small")).toContainText("separate sets with a line containing only");

  const suite = page.locator(".browser-suite");
  expect(await suite.evaluate((element) => element.scrollWidth <= element.clientWidth + 1)).toBe(true);
  const axe = await new AxeBuilder({ page }).include(".browser-suite").analyze();
  expect(axe.violations).toEqual([]);
});

test("Assistant session details are optional and persist as a shell preference", async ({ page }) => {
  test.skip((page.viewportSize()?.width ?? 1440) <= 1000, "The inspector is a wide-screen disclosure.");
  await openWorkspace(page, "/?view=chat", "Workbench");
  await expect(page.getByRole("complementary", { name: "Session inspector" })).toHaveCount(0);
  await page.getByRole("button", { name: "More Workbench actions" }).click();
  await page.getByRole("menuitem", { name: /Show session details/ }).click();
  await expect(page.getByRole("complementary", { name: "Session inspector" })).toBeVisible();
  expect(await page.evaluate(() => localStorage.getItem("nebula.session-inspector.open"))).toBe("true");
  await page.reload();
  await expect(page.getByRole("complementary", { name: "Session inspector" })).toBeVisible();
  await page.getByRole("button", { name: "More Workbench actions" }).click();
  await page.getByRole("menuitem", { name: /Hide session details/ }).click();
  expect(await page.evaluate(() => localStorage.getItem("nebula.session-inspector.open"))).toBe("false");
});

test("audit primary mutation dialogs through the shared dialog contract", async ({ page }, testInfo) => {
  test.setTimeout(120_000);
  const captureDialog = async (name: string, opener: ReturnType<Page["getByRole"]>, dialogName: string) => {
    await opener.scrollIntoViewIfNeeded();
    await opener.click();
    const dialog = page.getByRole("dialog", { name: dialogName, exact: true });
    await expect(dialog).toBeVisible();
    await expect.poll(() => dialog.evaluate((element) => element.contains(document.activeElement))).toBe(true);
    const bounds = await dialog.evaluate((element) => {
      const rect = element.getBoundingClientRect();
      return { top: rect.top, left: rect.left, right: rect.right, bottom: rect.bottom, viewportWidth: innerWidth, viewportHeight: innerHeight };
    });
    expect(bounds.left).toBeGreaterThanOrEqual(0);
    expect(bounds.top).toBeGreaterThanOrEqual(0);
    expect(bounds.right).toBeLessThanOrEqual(bounds.viewportWidth + 1);
    expect(bounds.bottom).toBeLessThanOrEqual(bounds.viewportHeight + 1);
    await page.screenshot({ path: testInfo.outputPath(`${name}.png`), fullPage: true });
    await page.keyboard.press("Escape");
    await expect(dialog).toBeHidden();
    await expect.poll(() => opener.evaluate((element) => element === document.activeElement)).toBe(true);
  };

  await openWorkspace(page, "/findings", "Findings");
  await captureDialog("dialog-finding", page.getByRole("button", { name: "New finding" }), "Create candidate finding");

  await openWorkspace(page, "/reports", "Reports");
  await captureDialog("dialog-report", page.getByRole("button", { name: "New report" }), "New report");

  await openWorkspace(page, "/project?view=assets", "Assets");
  await captureDialog("dialog-asset", page.getByRole("button", { name: "Add asset" }), "Add asset");

  await openWorkspace(page, "/project?view=evidence", "Evidence");
  await captureDialog("dialog-evidence", page.getByRole("button", { name: "Add evidence" }), "Add evidence");

  await openWorkspace(page, "/project?view=sources", "Knowledge");
  await captureDialog("dialog-source-url", page.getByRole("button", { name: "Add URL" }), "Add source from URL");

  await openWorkspace(page, "/settings#models-settings", "Settings");
  await captureDialog("dialog-provider", page.getByRole("button", { name: "Add provider" }), "Add model provider");

  await openWorkspace(page, "/settings#automation-settings", "Settings");
  await captureDialog("dialog-grok", page.getByRole("button", { name: "Add Grok" }), "Add Grok ACP");
  await captureDialog("dialog-codex", page.getByRole("button", { name: "Add Codex" }), "Add Codex");
  await captureDialog("dialog-mcp", page.getByRole("button", { name: "Add MCP server" }), "Add MCP server");

  await openWorkspace(page, "/settings#identity-security-settings", "Settings");
  await captureDialog("dialog-operator", page.getByRole("button", { name: "Add operator" }), "Add operator");
});

test("product typography and touch contracts hold on primary surfaces", async ({ page }) => {
  for (const [, route, heading] of workspaces) {
    await openWorkspace(page, route, heading);
    await expect.poll(() => page.evaluate(async () => {
      const [uiFaces, monoFaces] = await Promise.all([
        document.fonts.load('13px "Geist Variable"', "Nebula interface"),
        document.fonts.load('12px "Geist Mono Variable"', "sha256:0123456789abcdef"),
      ]);
      await document.fonts.ready;
      return uiFaces.length > 0 && monoFaces.length > 0;
    })).toBe(true);
    const typography = await page.locator("body").evaluate((body) => {
      const visible = (element: HTMLElement) => {
        const rect = element.getBoundingClientRect();
        const style = getComputedStyle(element);
        return rect.width > 0 && rect.height > 0 && style.display !== "none" && style.visibility !== "hidden";
      };
      const checks: Array<[string, number]> = [
        ["small, .eyebrow, .section-kicker, .count-badge, .status-badge, .data-table th", 11],
        [".panel-header p, .section-heading p, .empty-state p, .provider-message", 12],
        ["button, input, textarea, select, .text-link", 13],
      ];
      const scaleViolations = checks.flatMap(([selector, minimum]) => [...document.querySelectorAll<HTMLElement>(selector)]
        .filter(visible)
        .filter((element) => Number.parseFloat(getComputedStyle(element).fontSize) + .01 < minimum)
        .map((element) => `${selector}: ${element.className || element.tagName}=${getComputedStyle(element).fontSize}`));
      const weightViolations = [...document.querySelectorAll<HTMLElement>("body *")]
        .filter(visible)
        .filter((element) => ![400, 500, 600, 700].includes(Number.parseInt(getComputedStyle(element).fontWeight, 10)))
        .map((element) => `${element.className || element.tagName}=${getComputedStyle(element).fontWeight}`);
      const uiFamily = getComputedStyle(body).fontFamily;
      const monoProbe = document.createElement("code");
      monoProbe.textContent = "font-probe";
      body.append(monoProbe);
      const monoFamily = getComputedStyle(monoProbe).fontFamily;
      monoProbe.remove();
      return { scaleViolations, weightViolations, uiFamily, monoFamily };
    });
    expect(typography.scaleViolations, `${route} violates the semantic type scale`).toEqual([]);
    expect(typography.weightViolations, `${route} uses a weight outside the four-role typography contract`).toEqual([]);
    expect(typography.uiFamily).toContain("Geist Variable");
    expect(typography.monoFamily).toContain("Geist Mono Variable");
  }

  if ((page.viewportSize()?.width ?? 1440) <= 760) {
    await openWorkspace(page, "/?view=activity", "Workbench");
    const undersized = await page.locator(".mobile-companion-nav button, .settings-tabs a, .page .icon-button").evaluateAll((elements) => elements
      .filter((element) => {
        const rect = element.getBoundingClientRect();
        return rect.width > 0 && rect.height > 0 && getComputedStyle(element).display !== "none";
      })
      .filter((element) => element.getBoundingClientRect().width < 43.5 || element.getBoundingClientRect().height < 43.5)
      .map((element) => `${element.tagName.toLowerCase()}.${(element as HTMLElement).className}`));
    expect(undersized, "Mobile primary controls must provide 44px touch targets").toEqual([]);
  }
});

test("shared actions keep sleek geometry without weakening touch targets", async ({ page }) => {
  const mobile = (page.viewportSize()?.width ?? 1440) <= 760;
  for (const [route, name] of [["/findings", "New finding"], ["/?view=chat", "New chat"]] as const) {
    await openWorkspace(page, route, route === "/findings" ? "Findings" : "Workbench");
    const action = page.getByRole("button", { name, exact: true }).first();
    await expect(action).toBeVisible();
    const resting = await action.evaluate((element) => {
      const rect = element.getBoundingClientRect();
      const style = getComputedStyle(element);
      return {
        width: rect.width,
        height: rect.height,
        radius: Number.parseFloat(style.borderRadius),
        fontSize: Number.parseFloat(style.fontSize),
        shadow: style.boxShadow,
      };
    });
    expect(resting.height).toBeGreaterThanOrEqual(mobile ? 43.5 : 33.5);
    expect(resting.height).toBeLessThanOrEqual(mobile ? 44.5 : 36.5);
    expect(resting.radius).toBeLessThanOrEqual(8);
    expect(resting.fontSize).toBeGreaterThanOrEqual(13);
    expect(resting.shadow).toBe("none");

    await action.hover();
    const hovered = await action.boundingBox();
    expect(hovered?.width).toBeCloseTo(resting.width, 1);
    expect(hovered?.height).toBeCloseTo(resting.height, 1);
  }
});

test("calm structure avoids duplicate hierarchy and decorative nesting", async ({ page }) => {
  for (const [, route, heading] of workspaces) {
    await openWorkspace(page, route, heading);
    const contract = await page.locator("body").evaluate(() => {
      const visible = (element: HTMLElement) => {
        const rect = element.getBoundingClientRect();
        const style = getComputedStyle(element);
        return rect.width > 0 && rect.height > 0 && style.display !== "none" && style.visibility !== "hidden";
      };
      const primaryActions = [...document.querySelectorAll<HTMLElement>(".top-bar-page-actions .button.primary, .page > .page-header .button.primary")].filter(visible);
      const activePrimaryNavigation = [...document.querySelectorAll<HTMLElement>(".side-nav .nav-item.active, .mobile-companion-nav [aria-current='page']")].filter(visible);
      const nestedFrames = [...document.querySelectorAll<HTMLElement>(".standard-empty-state, .data-panel, .session-workspace, .settings-group-body")]
        .filter(visible)
        .flatMap((element) => {
          let ancestor = element.parentElement;
          let frames = 0;
          while (ancestor && ancestor.matches("#main-content *")) {
            const style = getComputedStyle(ancestor);
            if ([style.borderTopWidth, style.borderRightWidth, style.borderBottomWidth, style.borderLeftWidth].some((width) => Number.parseFloat(width) > 0)) frames += 1;
            ancestor = ancestor.parentElement;
          }
          return frames > 1 ? [`${element.className || element.tagName}: ${frames} bordered ancestors`] : [];
        });
      return { primaryActions: primaryActions.length, activePrimaryNavigation: activePrimaryNavigation.length, nestedFrames };
    });
    expect(contract.primaryActions, `${route} exposes duplicate primary actions`).toBeLessThanOrEqual(1);
    expect(contract.activePrimaryNavigation, `${route} exposes duplicate active primary navigation`).toBeLessThanOrEqual(1);
    expect(contract.nestedFrames, `${route} contains decorative frame nesting`).toEqual([]);
  }
});
