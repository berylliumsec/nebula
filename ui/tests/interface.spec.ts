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
