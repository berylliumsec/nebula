import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Page } from "@playwright/test";

const workspaces = [
  ["home", "/", "Good afternoon, Jordan"],
  ["sessions", "/sessions", "Sessions"],
  ["findings", "/findings", "Findings"],
  ["reports", "/reports", "Reports"],
  ["settings", "/settings", "Settings"],
] as const;

const responsiveWorkspaces = [
  ...workspaces,
  ["missions", "/agents", "Missions"],
  ["assets", "/assets", "Assets"],
  ["evidence", "/evidence", "Evidence"],
  ["knowledge", "/knowledge", "Knowledge"],
] as const;

async function openPreview(page: Page, route: string, heading: string) {
  await page.goto(route);
  await expect(page.getByRole("heading", { name: heading, exact: true })).toBeVisible();
  await expect(page.getByText("Interface preview")).toBeAttached();
  await page.waitForTimeout(120);
}

async function openConnectedOverview(page: Page) {
  const entity = {
    created_at: "2026-07-12T10:00:00Z",
    updated_at: "2026-07-12T11:00:00Z",
    revision: 1,
  };
  await page.unroute("**/api/v1/**");
  await page.route("**/api/v1/**", async (route) => {
    const url = new URL(route.request().url());
    let body: unknown = [];
    if (url.pathname.endsWith("/health")) {
      body = { status: "ok", version: "3.0.0", mode: "local", runner: "unavailable", human_pty: "unavailable" };
    } else if (url.pathname.endsWith("/engagements")) {
      body = [{
        ...entity,
        id: "engagement-live",
        name: "Quarterly identity and payments perimeter assessment",
        description: "",
        status: "active",
        tags: [],
        metadata: {},
      }];
    } else if (url.pathname.endsWith("/runs")) {
      body = [{
        ...entity,
        id: "run-live",
        engagement_id: "engagement-live",
        objective: "Validate external authentication, billing, and administrative control exposure",
        status: "running",
        metadata: {},
      }];
    } else if (url.pathname.endsWith("/providers")) {
      body = [{
        ...entity,
        id: "provider-local",
        name: "Local analyst",
        provider_type: "vllm",
        endpoint: null,
        enabled: true,
        is_local: true,
        secret_ref: null,
        model_allowlist: ["model-1"],
        capabilities: { streaming: true },
        privacy: { local_only: true, residency: [], permits_sensitive_data: false },
        metadata: { default_model: "model-1" },
      }];
    }
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(body),
    });
  });
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "Quarterly identity and payments perimeter assessment", exact: true })).toBeVisible();
  await expect(page.getByText("Interface preview")).toHaveCount(0);
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

test.beforeEach(async ({ page }) => {
  await page.route("**/api/v1/**", (request) => request.abort("failed"));
});

test("critical workspaces remain visually stable", async ({ page }, testInfo) => {
  for (const [name, route, heading] of workspaces) {
    await openPreview(page, route, heading);
    await expect(page).toHaveScreenshot(`${name}-${testInfo.project.name}.png`, { fullPage: true });
  }
});

test("all workspaces keep responsive content inside its owning surface", async ({ page }) => {
  for (const [, route, heading] of responsiveWorkspaces) {
    await openPreview(page, route, heading);
    const overflow = await page.locator("body").evaluate(() => {
      const selector = [
        ".page",
        ".metric-grid",
        ".metric-card",
        ".session-toolbar",
        ".agent-layout",
        ".agent-graph-panel",
        ".knowledge-sources",
        ".settings-tabs",
        ".finding-summary-grid",
        ".summary-strip",
        ".data-toolbar",
        ".callout",
        ".mission-hero",
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

test("live Overview empty activity keeps its copy in a readable content track", async ({ page }) => {
  await openConnectedOverview(page);
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
    await openConnectedOverview(page);
    await expect(page.getByRole("button", { name: "New mission" })).toBeVisible();
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
    await openPreview(page, "/", "Good afternoon, Jordan");
    await page.evaluate((value) => localStorage.setItem("nebula.theme", value), theme);
    for (const [, route, heading] of workspaces) {
      await openPreview(page, route, heading);
      await expect(page.locator("html")).toHaveAttribute("data-theme", theme);
      const results = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa"]).analyze();
      expect(results.violations, results.violations.map((violation) => `${violation.id}: ${violation.help}`).join("\n")).toEqual([]);
      const undersizedText = await page.locator("body").evaluate(() => [...document.querySelectorAll<HTMLElement>("body *")]
        .filter((element) => [...element.childNodes].some((node) => node.nodeType === Node.TEXT_NODE && node.textContent?.trim()))
        .filter((element) => element.getClientRects().length > 0 && getComputedStyle(element).visibility !== "hidden")
        .filter((element) => Number.parseFloat(getComputedStyle(element).fontSize) < 11)
        .map((element) => `${element.tagName.toLowerCase()}.${element.className}:\"${element.textContent?.trim().slice(0, 60)}\":${getComputedStyle(element).fontSize}`));
      expect(undersizedText, `${theme} ${route} renders text below 11px`).toEqual([]);
    }
  });
}

test("appearance variants preserve each critical workspace hierarchy", async ({ page }) => {
  for (const theme of ["light", "high-contrast"] as const) {
    await openPreview(page, "/", "Good afternoon, Jordan");
    await page.evaluate((value) => localStorage.setItem("nebula.theme", value), theme);
    for (const [name, route, heading] of workspaces) {
      await openPreview(page, route, heading);
      await expect(page.locator("html")).toHaveAttribute("data-theme", theme);
      await expect(page).toHaveScreenshot(`${name}-${theme}.png`, { fullPage: true });
    }
  }
});
