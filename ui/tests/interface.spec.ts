import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Page } from "@playwright/test";

const workspaces = [
  ["workbench", "/", "Workbench"],
  ["findings", "/findings", "Findings"],
  ["reports", "/reports", "Reports"],
  ["project", "/project", "Scratch Project"],
  ["settings", "/settings", "Settings"],
] as const;

const firstRunThemeTest = "Zero is the first-run default theme";

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

  await page.route("**/api/v1/**", async (route) => {
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
  await page.goto(route);
  if (heading === "Workbench") {
    await expect(page.getByRole("tab", { name: "Terminal", exact: true })).toBeVisible({ timeout: 15_000 });
    await expect(page.getByText("Start in Terminal, edit shared code, browse a target, ask the assistant, or open your project files.")).toHaveCount(0);
  } else {
    await expect(page.getByRole("heading", { name: heading, exact: true })).toBeVisible({ timeout: 15_000 });
  }
  await expect(page.getByText("Interface preview")).toHaveCount(0);
  await expect(page.getByText(/Jordan|Acme/i)).toHaveCount(0);
  if (route === "/") {
    await expect(page.getByText("Connected", { exact: true })).toBeVisible();
    await expect(page.getByRole("button", { name: "Screenshot" })).toBeVisible();
  }
  await page.evaluate(() => document.fonts.ready);
  await page.waitForTimeout(120);
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
      if (localStorage.getItem("nebula.theme") === null) localStorage.setItem("nebula.theme", "dark");
    });
  }
});

test("browser address bar stays above logical native bounds at 2x scale", async ({ browser }, testInfo) => {
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
    localStorage.setItem("nebula.theme", "dark");
    const calls: Array<{ command: string; args: Record<string, unknown> }> = [];
    Object.assign(window, { __NEBULA_BROWSER_CALLS__: calls });
    Object.assign(window, {
      __TAURI_INTERNALS__: {
        invoke: async (command: string, args: Record<string, unknown> = {}) => {
          calls.push({ command, args });
          if (command === "start_local_backend") {
            return { endpoint: `${location.origin}/api/v1`, token: "", protocol: "nebula-sidecar-v1" };
          }
          if (command === "browser_capabilities") {
            return { engine: "Playwright native-bounds mock", projectStorage: "persistent" };
          }
          return undefined;
        },
        transformCallback: () => 1,
        unregisterCallback: () => undefined,
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

  const geometry = await page.evaluate(() => {
    const toolbar = document.querySelector<HTMLElement>(".browser-toolbar")!;
    const address = document.querySelector<HTMLElement>("#browser-address")!;
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
  await page.screenshot({ path: testInfo.outputPath("browser-address-bar-2x.png") });
  await context.close();
});

test("terminal screenshot capture opens a full-height integrated editor", async ({ page }) => {
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
  const viewportHeight = page.viewportSize()?.height ?? 900;
  expect(dimensions.dialogHeight).toBeGreaterThan(Math.min(760, viewportHeight - 80));
  expect(dimensions.editorHeight).toBeGreaterThan(Math.min(650, viewportHeight - 160));
  expect(dimensions.viewportHeight).toBeGreaterThan(Math.min(440, viewportHeight - 290));
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
  await expect(page.getByRole("toolbar", { name: "Selected text actions" })).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath("terminal-visible-selection.png") });
  const selectionRects = await screen.locator(".xterm-selection > div").evaluateAll((rectangles) =>
    rectangles.map((rectangle) => {
      const rect = rectangle.getBoundingClientRect();
      return {
        background: getComputedStyle(rectangle).backgroundColor,
        width: rect.width,
        height: rect.height,
      };
    }),
  );
  expect(selectionRects.length).toBeGreaterThan(0);
  expect(selectionRects.some((rect) => rect.width > 20 && rect.height > 8)).toBe(true);
  expect(selectionRects.every((rect) => ["rgb(22, 139, 210)", "rgb(18, 111, 168)"].includes(rect.background))).toBe(true);
});

test("hidden terminal views stop emitting resize frames", async ({ page }) => {
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
  await expect(page.locator("html")).toHaveAttribute("data-theme", "zero");
  await expect(page.getByRole("region", { name: "Zero Layer context" })).toHaveCount(0);
  expect(await page.evaluate(() => localStorage.getItem("nebula.theme"))).toBeNull();
});

test("primary navigation exposes only the five task destinations", async ({ page }) => {
  await openWorkspace(page, "/", "Workbench");
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
  await page.getByRole("tab", { name: "Autonomous missions", exact: true }).click();

  const controls = page.getByRole("region", { name: "Mission controls" });
  await expect(controls.getByText("Missions need an enabled model provider or agent harness with a verified model.")).toBeVisible();
  await expect(controls.getByRole("link", { name: "Configure runtime" })).toHaveAttribute("href", "/settings#models-settings");
  await expect(controls.getByRole("button", { name: "Automate task" })).toBeDisabled();
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

test("all assistant states remain fully visible inside the workbench viewport", async ({ page }, testInfo) => {
  if (testInfo.project.name === "desktop") await page.setViewportSize({ width: 2048, height: 868 });
  await page.addInitScript(() => localStorage.setItem("nebula.theme", "zero"));
  await openWorkspace(page, "/", "Workbench");
  await page.getByRole("tab", { name: "Analyst chat", exact: true }).click();

  if (testInfo.project.name === "narrow") await page.getByRole("button", { name: "Conversations" }).click();
  const conversationRow = page.locator(".session-list nav > button").first();
  await expect(conversationRow).toBeVisible();
  expect(await conversationRow.evaluate((element) => element.getBoundingClientRect().height)).toBeLessThanOrEqual(50);
  if (testInfo.project.name === "narrow") await page.getByRole("button", { name: "Current chat" }).click();

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
    const settings = element.querySelector<HTMLElement>(".chat-settings")!;
    const scroll = element.querySelector<HTMLElement>(".chat-scroll")!;
    const composer = element.querySelector<HTMLElement>(".chat-composer")!;
    const panelRect = panel.getBoundingClientRect();
    const settingsRect = settings.getBoundingClientRect();
    const scrollRect = scroll.getBoundingClientRect();
    const composerRect = composer.getBoundingClientRect();
    return {
      workspaceTop: workspaceRect.top,
      workspaceBottom: workspaceRect.bottom,
      panelTop: panelRect.top,
      panelBottom: panelRect.bottom,
      panelClientHeight: panel.clientHeight,
      panelScrollHeight: panel.scrollHeight,
      settingsHeight: settingsRect.height,
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
  for (let index = 0; index < 40 && await distanceFromBottom() > 2; index += 1) {
    await page.mouse.wheel(0, 1000);
    await page.waitForTimeout(10);
  }
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

test("an idle resumed harness uses one compact ready status", async ({ page }, testInfo) => {
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
  await page.locator(".session-select").filter({ hasText: "Ready harness conversation" }).click();
  await expect(page.locator(".chat-composer footer")).toContainText("Harness ready · Resumed session · 0 MCP");
  await expect(page.locator(".chat-harness-progress")).toHaveCount(0);
  await expect(page.locator(".session-inspector code").filter({ hasText: harnessSessionId })).toHaveText(harnessSessionId);
});

test("the workbench expands to the full viewport and restores in place", async ({ page }) => {
  await page.addInitScript(() => localStorage.setItem("nebula.theme", "zero"));
  await openWorkspace(page, "/?view=chat", "Workbench");

  await page.getByRole("button", { name: "Enter full screen workbench" }).click();
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

  for (const [tabName, contentSelector] of [
    ["Terminal", ".persistent-terminal"],
    ["Workspace code editor", ".persistent-code-editor"],
    ["Project browser", ".persistent-browser"],
    ["Analyst chat", ".session-workspace > .chat-empty-state"],
    ["Workspace files", ".workspace-browser"],
    ["Project notes", ".notes-panel"],
    ["Autonomous missions", ".agents-page"],
    ["Activity history", ".workbench-activity-stack"],
  ] as const) {
    await page.getByRole("tab", { name: tabName, exact: true }).click();
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
  await expect(page.getByRole("button", { name: "Enter full screen workbench" })).toBeVisible();
  await expect(page.locator(".sessions-page")).not.toHaveClass(/full-screen/);
});

test("the code editor keeps its caret and syntax layers aligned while typing", async ({ page }) => {
  await page.addInitScript(() => localStorage.setItem("nebula.theme", "zero"));
  await page.goto("/?view=code");
  await expect(page.getByRole("tab", { name: "Workspace code editor", exact: true })).toBeVisible({ timeout: 15_000 });
  await page.evaluate(() => document.fonts.ready);
  await page.getByRole("button", { name: "New file", exact: true }).first().click();
  const filePath = page.getByRole("textbox", { name: "File path" });
  await filePath.evaluate((input: HTMLInputElement) => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set?.call(input, "example.c");
    input.dispatchEvent(new Event("input", { bubbles: true }));
  });
  await expect(filePath).toHaveValue("example.c");
  await expect(page.locator(".code-mirror-host")).toHaveAttribute("data-language-ready", "example.c");

  const inputSurface = page.getByRole("textbox", { name: "Code editor" });
  await inputSurface.click({ force: true });
  const editor = inputSurface.locator("..").locator("..");
  await page.keyboard.type("#include <stdio.h>", { delay: 10 });
  await page.keyboard.press("Enter");
  await expect(page.locator(".cm-line")).toHaveCount(2);
  await page.keyboard.press("Enter");
  await expect(page.locator(".cm-line")).toHaveCount(3);
  await page.keyboard.type("int main(void) ", { delay: 10 });
  await page.keyboard.insertText("{");
  await page.keyboard.press("Enter");
  await expect(page.locator(".cm-line")).toHaveCount(4);
  await page.keyboard.type("  return 0;", { delay: 10 });
  await page.keyboard.press("Enter");
  await expect(page.locator(".cm-line")).toHaveCount(5);
  await page.keyboard.insertText("}");
  await page.keyboard.press("Escape");
  await expect(page.getByText("C", { exact: true })).toBeVisible();

  await expect(page.locator(".cm-line")).toHaveCount(5);
  await expect(page.locator(".cm-line").nth(3)).toContainText("return 0;");
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
  expect(geometry.hostHeight).toBeGreaterThan(400);
  expect(geometry.lineTops).toHaveLength(5);
  expect(geometry.numberTops).toHaveLength(5);
  geometry.lineTops.forEach((lineTop, index) => expect(Math.abs(lineTop - geometry.numberTops[index])).toBeLessThan(2));
  await expect(editor).toHaveCSS("outline-style", "none");
  await expect(editor).toHaveCSS("border-top-width", "0px");
  await expect(editor).toHaveCSS("box-shadow", "none");
  await expect(inputSurface).toHaveCSS("outline-style", "none");
  await expect(inputSurface).toHaveCSS("box-shadow", "none");
  await expect(inputSurface).not.toHaveCSS("caret-color", "rgba(0, 0, 0, 0)");
  await expect(page.getByText(/Ln 5, Col [23]/, { exact: true })).toBeVisible();
  await expect(editor.locator(".cm-cursor-primary")).toHaveCount(0);
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
  await expect(page.getByRole("button", { name: "Preparing Kali…" })).toBeDisabled();
});

test("terminal and notes keep a visible focused caret", async ({ page }) => {
  test.setTimeout(60_000);
  await page.addInitScript(() => localStorage.setItem("nebula.theme", "zero"));
  await openWorkspace(page, "/", "Workbench");

  const terminalSurface = page.locator(".xterm-shell").first();
  await terminalSurface.click({ force: true });
  await expect(page.locator(".xterm").first()).toHaveCSS("cursor", "text");
  const terminalInput = page.getByRole("textbox", { name: "Terminal input" }).first();
  await terminalInput.click({ force: true });
  await expect(terminalInput).toBeFocused();

  await page.getByRole("tab", { name: "Project notes", exact: true }).click();
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

for (const theme of ["light", "dark", "zero", "high-contrast"] as const) {
  test(`critical workspaces meet automated accessibility checks in ${theme} mode`, async ({ page }) => {
    test.setTimeout(60_000);
    await openWorkspace(page, "/", "Workbench");
    await page.evaluate((value) => localStorage.setItem("nebula.theme", value), theme);
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
      if (theme === "zero") {
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
        expect(overflow, `Zero ${route} contains unintended horizontal overflow`).toEqual([]);
      }
    }
  });
}

test("Zero materialization respects the reduced-motion preference", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "desktop", "Motion behavior only needs one browser project.");
  await page.emulateMedia({ reducedMotion: "no-preference" });
  await page.addInitScript(() => localStorage.setItem("nebula.theme", "zero"));
  await openWorkspace(page, "/", "Workbench");

  const materialized = await page.locator(".zero-route-flare").evaluate((element) => {
    const style = getComputedStyle(element);
    return { name: style.animationName, duration: style.animationDuration };
  });
  expect(materialized.name).toContain("zero-materialize");
  expect(Number.parseFloat(materialized.duration)).toBeGreaterThanOrEqual(.3);
  expect(await page.locator(".app-shell").evaluate((element) => getComputedStyle(element, "::after").animationName)).toBe("none");
  expect(await page.locator("body").evaluate((element) => ({
    bodyBefore: getComputedStyle(element, "::before").animationName,
    bodyAfter: getComputedStyle(element, "::after").animationName,
    shellBefore: getComputedStyle(document.querySelector(".app-shell")!, "::before").animationName,
  }))).toEqual({ bodyBefore: "none", bodyAfter: "none", shellBefore: "none" });

  await page.getByRole("button", { name: "Search commands" }).click();
  await expect(page.getByRole("dialog", { name: "Command palette" })).toBeVisible();
  expect(await page.locator(".command-palette").evaluate((element) => getComputedStyle(element).animationName)).toContain("zero-dialog-materialize");

  await page.keyboard.press("Escape");
  await page.emulateMedia({ reducedMotion: "reduce" });
  const reduced = await page.locator(".zero-route-flare").evaluate((element) => {
    const style = getComputedStyle(element);
    return { name: style.animationName, transform: style.transform, clipPath: style.clipPath };
  });
  expect(reduced).toEqual({ name: "none", transform: "none", clipPath: "none" });
});

test("Zero keeps one navigable panoramic shell at every breakpoint", async ({ page }, testInfo) => {
  await page.addInitScript(() => localStorage.setItem("nebula.theme", "zero"));
  await openWorkspace(page, "/", "Workbench");

  await expect(page.getByRole("region", { name: "Zero Layer context" })).toHaveCount(0);
  await expect(page.getByRole("complementary", { name: "Primary navigation" })).toHaveCount(1);
  await expect(page.locator("main#main-content")).toHaveCount(1);
  for (const label of ["Workbench", "Findings", "Reports", "Project", "Settings"]) {
    await expect(page.getByRole("complementary", { name: "Primary navigation" }).getByRole("link", { name: label, exact: true })).toBeVisible();
  }

  const geometry = await page.locator(".app-shell").evaluate((shell) => {
    const viewport = { width: window.innerWidth, height: window.innerHeight };
    const bounds = (selector: string) => {
      const rect = shell.querySelector<HTMLElement>(selector)!.getBoundingClientRect();
      return { top: rect.top, right: rect.right, bottom: rect.bottom, left: rect.left, width: rect.width, height: rect.height };
    };
    return {
      viewport,
      shellOverflow: document.documentElement.scrollWidth > viewport.width || document.documentElement.scrollHeight > viewport.height,
      main: bounds(".main-content"),
      navigation: bounds(".side-nav"),
    };
  });
  expect(geometry.shellOverflow).toBe(false);
  for (const surface of [geometry.main, geometry.navigation]) {
    expect(surface.left).toBeGreaterThanOrEqual(0);
    expect(surface.right).toBeLessThanOrEqual(geometry.viewport.width + 1);
    expect(surface.top).toBeGreaterThanOrEqual(0);
    expect(surface.bottom).toBeLessThanOrEqual(geometry.viewport.height + 1);
    expect(surface.width).toBeGreaterThan(0);
    expect(surface.height).toBeGreaterThan(0);
  }

  const workbenchLink = page.getByRole("complementary", { name: "Primary navigation" }).getByRole("link", { name: "Workbench", exact: true });
  await workbenchLink.focus();
  const focusStyle = await workbenchLink.evaluate((element) => {
    const style = getComputedStyle(element);
    return { style: style.outlineStyle, width: Number.parseFloat(style.outlineWidth), color: style.outlineColor };
  });
  expect(focusStyle.style).toBe("solid");
  expect(focusStyle.width).toBeGreaterThanOrEqual(2);
  expect(focusStyle.color).not.toBe("rgba(0, 0, 0, 0)");
  await workbenchLink.evaluate((element) => element.blur());

  if (testInfo.project.name !== "desktop") {
    await expect(page).toHaveScreenshot("workbench-zero-responsive.png", { fullPage: true });
  }

  const terminal = page.locator(".persistent-terminal");
  await expect(terminal).toBeVisible();
  await terminal.evaluate((element) => { (window as typeof window & { __zeroTerminal?: Element }).__zeroTerminal = element; });
  await page.getByRole("button", { name: "Search commands" }).click();
  await page.getByRole("textbox", { name: "Search commands" }).fill("dark theme");
  await page.getByRole("option", { name: /Use dark theme/ }).click();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  expect(await terminal.evaluate((element) => (window as typeof window & { __zeroTerminal?: Element }).__zeroTerminal === element)).toBe(true);
});

for (const [name, route, heading] of workspaces) {
  test(`Zero preserves the ${name} desktop hierarchy`, async ({ page }, testInfo) => {
    test.skip(testInfo.project.name !== "desktop", "Zero visual baselines are captured at the reference desktop size.");
    await page.addInitScript(() => localStorage.setItem("nebula.theme", "zero"));
    await openWorkspace(page, route, heading);
    await expect(page.locator("html")).toHaveAttribute("data-theme", "zero");
    await page.waitForTimeout(360);
    await expect(page).toHaveScreenshot(`${name}-zero.png`, { fullPage: true });
  });
}

test("Zero preserves representative desktop overlays", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "desktop", "Zero visual baselines are captured at the reference desktop size.");
  await page.addInitScript(() => localStorage.setItem("nebula.theme", "zero"));
  await openWorkspace(page, "/", "Workbench");
  await page.getByRole("button", { name: "Search commands" }).click();
  await expect(page.getByRole("dialog", { name: "Command palette" })).toBeVisible();
  await expect(page).toHaveScreenshot("workbench-zero-command-palette.png", { fullPage: true });

  await page.keyboard.press("Escape");
  await page.keyboard.press("Control+Alt+i");
  await expect(page.getByRole("complementary", { name: "Activity inspector" })).toBeVisible();
  await expect(page).toHaveScreenshot("workbench-zero-activity-drawer.png", { fullPage: true });
  await page.getByRole("button", { name: "Close activity center" }).click();
});

test("Zero preserves a representative resource dialog", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "desktop", "Zero visual baselines are captured at the reference desktop size.");
  await page.addInitScript(() => localStorage.setItem("nebula.theme", "zero"));
  await openWorkspace(page, "/findings", "Findings");
  await page.waitForTimeout(360);
  await page.getByRole("button", { name: "New finding" }).click();
  await expect(page.getByRole("dialog", { name: "Create candidate finding" })).toBeVisible();
  await expect(page).toHaveScreenshot("findings-zero-dialog.png", { fullPage: true });
  await page.getByRole("button", { name: "Close candidate finding dialog" }).click();
});

test("Zero preserves the appearance selector", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "desktop", "Zero visual baselines are captured at the reference desktop size.");
  await page.addInitScript(() => localStorage.setItem("nebula.theme", "zero"));
  await openWorkspace(page, "/settings#appearance-settings", "Settings");
  await expect(page.getByRole("link", { name: "Advanced settings" })).toHaveAttribute("aria-current", "page");
  await expect(page.locator(".appearance-panel")).toBeVisible();
  await expect(page.locator(".appearance-panel")).toHaveScreenshot("settings-zero-appearance.png");
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

test("tool follow-up toggles explain missing runtime setup", async ({ page }) => {
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

test("appearance variants preserve each critical workspace hierarchy", async ({ page }) => {
  test.setTimeout(60_000);
  for (const theme of ["light", "high-contrast"] as const) {
    await openWorkspace(page, "/", "Workbench");
    await page.evaluate((value) => localStorage.setItem("nebula.theme", value), theme);
    for (const [name, route, heading] of workspaces) {
      await openWorkspace(page, route, heading);
      await expect(page.locator("html")).toHaveAttribute("data-theme", theme);
      await expect(page).toHaveScreenshot(`${name}-${theme}.png`, { fullPage: true });
    }
  }
});

test("audit every primary workspace view", async ({ page }, testInfo) => {
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

  await openWorkspace(page, "/", "Workbench");
  for (const [name, label] of [
    ["workbench-terminal", "Terminal"],
    ["workbench-browser", "Project browser"],
    ["workbench-assistant", "Analyst chat"],
    ["workbench-files", "Workspace files"],
    ["workbench-notes", "Project notes"],
    ["workbench-missions", "Autonomous missions"],
    ["workbench-activity", "Activity history"],
  ] as const) {
    await page.getByRole("tab", { name: label, exact: true }).click();
    await capture(name);
  }

  for (const [name, route, heading] of [
    ["findings", "/findings", "Findings"],
    ["reports", "/reports", "Reports"],
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
  ] as const) {
    await page.getByRole("link", { name: label, exact: true }).click();
    await capture(name);
  }
});
