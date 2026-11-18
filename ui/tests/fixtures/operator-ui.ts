import {expect, request, type Page} from "@playwright/test";
import {keyedCoreRequests} from "./core-harness";
import type {RealCore} from "./real-core";
const playwrightRequest = keyedCoreRequests(request);

export function coreShell(page: Page, chip: RegExp) {
  return page.getByRole("button", { name: chip })
    .or(page.getByRole("navigation", { name: "Mobile operator navigation" }))
    .or(page.getByRole("button", { name: "Open navigation", exact: true }))
    .first();
}

export function coreReady(page: Page) {
  return coreShell(page, /^Nebula Core ready$/);
}

export async function showProjectNavigation(page: Page) {
  const navigation = page.getByRole("button", { name: "Open navigation" });
  if (await navigation.getAttribute("aria-expanded") !== "true") {
    if (await page.evaluate(() => matchMedia("(hover: hover) and (pointer: fine)").matches)) {
      await navigation.hover();
    } else {
      await navigation.click();
    }
  }
  await expect(navigation).toHaveAttribute("aria-expanded", "true");
  await expect(page.getByRole("button", { name: "Switch project", exact: true })).toBeVisible();
}

export async function openProjectSwitcherFromShell(page: Page) {
  const trigger = page.getByRole("button", { name: "Switch project", exact: true });
  if (!await trigger.isVisible()) await showProjectNavigation(page);
  await trigger.click();
  return page.getByRole("dialog", { name: "Project switcher" });
}

export async function openNebulaAssistantFromShell(page: Page) {
  await expect(page.locator(".app-shell")).toBeVisible();
  const assistant = page.getByRole("dialog", {name: "Butler", exact: true});
  if (await assistant.isVisible()) return;
  const butlerLauncherName = /^(Open|Show) Butler(?:;|$)/;
  const mobileLauncher = page.getByRole("navigation", { name: "Mobile operator navigation" })
    .getByRole("button", { name: butlerLauncherName });
  const launcher = page.getByRole("button", { name: butlerLauncherName }).first();
  const topMenu = page.getByRole("button", { name: "Open top menu" });
  await expect.poll(async () => await assistant.isVisible() || await mobileLauncher.isVisible()
    || await launcher.isVisible()
    || await topMenu.isVisible()).toBe(true);
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
  // Move focus to the revealed control before using the pointer on it.
  await launcher.focus();
  await launcher.click();
}

export async function guideEntry(page: Page) {
  const guides = page.getByRole("button", {name: "Guides", exact: true});
  if ((page.viewportSize()?.width ?? 1440) <= 760) {
    if (!await guides.isVisible()) {
      const more = page.getByRole("button", {name: "More workbench views", exact: true});
      await expect(more).toBeVisible();
      await more.click();
    }
  } else {
    // Quiet Lens keeps secondary top-bar actions behind its visible menu.
    const topMenu = page.getByRole("button", {name: "Open top menu"});
    if (!await guides.isVisible() && await topMenu.isVisible()) {
      await topMenu.focus();
      // Escape suppresses hover/focus reveal until the menu is activated again.
      await topMenu.press("Enter");
    }
  }
  await expect(guides).toBeVisible();
  // Move keyboard focus into the opened menu so its lens handle stops
  // intercepting pointer activation of the Guides button.
  await guides.focus();
  return guides;
}

export async function clickTopBarAction(page: Page, name: string) {
  const action = page.getByRole("button", {name, exact: true});
  if (!await action.isVisible()) {
    await page.getByRole("button", {name: "Open top menu", exact: true}).click();
  }
  await expect(action).toBeVisible();
  await action.focus();
  await action.click();
}

export async function clickWorkbenchTab(page: Page, name: string) {
  const tab = page.getByRole("tab", {name, exact: true});
  if (!await tab.isVisible()) {
    await page.getByRole("button", {name: "Open top menu", exact: true}).click();
  }
  await expect(tab).toBeVisible();
  await tab.focus();
  await tab.click();
}

export function coreConnected(page: Page) {
  return coreShell(page, /^Nebula Core (ready|degraded)/);
}

export async function clickAssistantSettings(page: Page) {
  const trigger = page.getByRole("button", {name: "Assistant settings", exact: true});
  const idleComposer = await trigger.evaluate(element => Boolean(element.closest(".chat-composer")))
    && await page.evaluate(() => matchMedia("(hover: hover) and (pointer: fine)").matches);
  if (idleComposer) await page.locator(".chat-composer").hover();
  await trigger.click();
}

export async function pairRealCoreBrowser(page: Page, core: Pick<RealCore, "origin" | "token">, name: string, options: { requireReady?: boolean } = {}) {
  const pairingApi = await playwrightRequest.newContext({
    baseURL: `${new URL(core.origin).protocol}//127.0.0.1:${new URL(core.origin).port}/api/v1/`,
    extraHTTPHeaders: { Authorization: `Bearer ${core.token}` },
    ignoreHTTPSErrors: core.origin.startsWith("https://"),
  });
  const pairingResponse = await pairingApi.post("auth/pairings", { data: { name } });
  expect(pairingResponse.ok(), await pairingResponse.text()).toBe(true);
  const pairing = await pairingResponse.json() as { secret: string; confirmation_code: string };
  await pairingApi.dispose();
  await page.goto(`${core.origin}/?view=chat#pair=${encodeURIComponent(pairing.secret)}&code=${encodeURIComponent(pairing.confirmation_code)}`);
  await page.getByLabel("Device name").fill(name);
  await page.getByRole("button", { name: "Pair device" }).click();
  // Redeeming a pair calls location.replace("/"). Wait for that navigation
  // to finish before a test opens its project URL on the same page.
  await expect(page).toHaveURL(`${core.origin}/`, { timeout: 20_000 });
  await expect(page.locator(".app-shell")).toBeVisible({ timeout: 20_000 });
  if (options.requireReady !== false) {
    // The compact Workbench may start on Terminal with its Core chip inside
    // the navigation lens. Pairing is complete once the shell mounts; Core's
    // health endpoint supplies the server-readiness assertion.
    const health = await page.request.get(`${core.origin}/api/v1/health`, {
      headers: { Authorization: `Bearer ${core.token}` },
    });
    expect(health.ok(), await health.text()).toBe(true);
  }
}

export async function expectNoChatStreamFailure(page: Page) {
  await expect(page.getByText(/no longer running|Could not reconnect|could not be completed|Connection lost/)).toHaveCount(0);
}

/** Open the same visible settings entry on desktop and compact navigation. */
export async function navigateOperatorPage(page: Page, name: "Settings" | "Workbench") {
  const close = page.getByRole("button", {name: "Close setting", exact: true});
  if (await close.isVisible()) await close.click();
  const workbench = page.getByRole("button", {name: "Workbench", exact: true});
  if (name === "Workbench" && await workbench.isVisible()) {
    await workbench.click();
    return;
  }
  const more = page.getByRole("button", {name: "More workbench views", exact: true});
  if (name === "Settings" && await more.isVisible()) {
    await more.click();
    await page.getByRole("dialog", {name: "More", exact: true}).getByRole("button", {name, exact: true}).click();
    return;
  }
  const link = page.getByRole("link", {name, exact: true});
  if (!await link.isVisible()) await showProjectNavigation(page);
  await link.click();
}

export async function openRuntimeSettings(page: Page, runtime: "provider" | "harness") {
  await navigateOperatorPage(page, "Settings");
  const advanced = page.getByRole("link", {name: "Advanced settings", exact: true});
  const entry = page.getByRole("button", {name: runtime === "provider" ? /^Model providers / : /^Assistant harnesses /});
  await expect(advanced.or(entry).filter({visible: true}).first()).toBeVisible();
  if (await advanced.isVisible()) await advanced.click();
  else await entry.click();
  if (runtime === "harness") await page.locator("#automation-settings > summary").click();
  await expect(page.getByRole("button", {name: runtime === "provider" ? "Add provider" : "Add Grok", exact: true})).toBeVisible();
  return entry;
}
