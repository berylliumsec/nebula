import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Page } from "@playwright/test";

const workspaces = [
  ["workbench", "/", "Workbench"],
  ["findings", "/findings", "Findings"],
  ["reports", "/reports", "Reports"],
  ["project", "/project", "Scratch Project"],
  ["settings", "/settings", "Settings"],
] as const;

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

      send(): void {}
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
    } else if (path.endsWith("/container-terminal/recover") && request.method() === "POST") {
      body = { active: false };
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
    } else if (path.endsWith("/providers/discover-local")) {
      body = [];
    } else if (path.endsWith("/terminal/commands/status")) {
      body = {
        engagement_id: "scratch-project",
        enabled: true,
        record_count: 0,
        retention_days: 90,
        max_records: 10_000,
        oldest_recorded_at: null,
        newest_recorded_at: null,
      };
    } else if (path.endsWith("/terminal/commands")) {
      body = { records: [], total: 0, offset: 0, limit: 100, next_offset: null };
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
  await expect(page.getByRole("heading", { name: heading, exact: true })).toBeVisible();
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
  return page.locator(".page").evaluate((root) => {
    const issues: string[] = [];
    const candidates = [...root.querySelectorAll<HTMLElement>("h1, h2, h3, p, strong, small, dd")];
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

test.beforeEach(async ({ page }) => installTruthfulCore(page));

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

test("critical workspaces remain visually stable", async ({ page }, testInfo) => {
  for (const [name, route, heading] of workspaces) {
    await openWorkspace(page, route, heading);
    await expect(page).toHaveScreenshot(`${name}-${testInfo.project.name}.png`, { fullPage: true });
  }
});

test("all task workspaces keep responsive content inside its owning surface", async ({ page }) => {
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

for (const theme of ["light", "dark", "high-contrast"] as const) {
  test(`critical workspaces meet automated accessibility checks in ${theme} mode`, async ({ page }) => {
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
    }
  });
}

test("appearance variants preserve each critical workspace hierarchy", async ({ page }) => {
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
