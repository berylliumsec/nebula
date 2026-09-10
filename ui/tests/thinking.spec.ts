import { expect, test } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";

for (const vendor of ["grok_acp", "codex_app_server"]) {
  test(`${vendor} thinking episodes remain discoverable and readable after reload`, async ({ page, request }, info) => {
    const origin = `http://127.0.0.1:${new URL(String(info.project.use.baseURL)).port}`;
    const pairing = await (await request.post(`${origin}/api/v1/auth/pairings`, { headers: { Authorization: "Bearer model-test-token" }, data: { name: "Thinking test" } })).json();
    await page.goto(`/#pair=${encodeURIComponent(pairing.secret)}&code=${encodeURIComponent(pairing.confirmation_code)}`);
    await page.getByLabel("Device name").fill("Thinking test");
    await page.getByRole("button", { name: "Pair device", exact: true }).click();
    await expect(page.getByRole("button", { name: /Nebula Core (ready|degraded)/ })).toBeVisible({ timeout: 20000 });
    await page.goto(`/projects/thinking-project/workbench?view=chat&session=${vendor}-chat`);
    const openThinking = async () => {
      await expect(page.getByText("Saved final response for " + vendor)).toBeVisible();
      if (vendor === "grok_acp") {
        await expect(page.getByText("Recorded work before the operator stopped.", {exact: true})).toBeVisible();
        await expect(page.getByText("Stopped", {exact: true})).toBeVisible();
        await expect(page.getByText("A partial answer retained after stopping.", {exact: true})).toBeVisible();
        await expect(page.getByText("Reference: pending local diagnostic")).toHaveCount(0);
        const transcript = await page.locator(".chat-message").allTextContents();
        expect(transcript.findIndex(text => text.includes("Recorded work before"))).toBeLessThan(transcript.findIndex(text => text.includes("My next message")));
      }
      await page.getByRole("button", { name: "Show activity", exact: true }).first().click();
      const rows = page.locator(".activity-ledger-audit li").filter({ has: page.locator(".harness-reasoning-summary") });
      await expect(rows).toHaveCount(vendor === "grok_acp" ? 3 : 2);
      for (const marker of ["First thinking episode from ", "Second thinking episode from "]) {
        const row = rows.filter({ has: page.getByText(marker + vendor, { exact: true }) });
        const summary = row.locator(".activity-ledger-entry-content > details > summary");
        if (info.project.use.hasTouch) await summary.tap();
        else { await summary.focus(); await summary.press("Enter"); }
        await expect(row.getByText(marker + vendor, { exact: true })).toBeVisible();
      }
      if (vendor === "grok_acp") {
        const long = rows.filter({ has: page.getByText("Thinking display shortened after 65,536 characters.", { exact: true }) });
        await long.locator(".activity-ledger-entry-content > details > summary").click();
        await expect(long.getByText("Thinking display shortened after 65,536 characters.", { exact: true })).toBeVisible();
      }
      return rows;
    };
    await openThinking();
    expect((await new AxeBuilder({ page }).include(".activity-ledger-audit").analyze()).violations).toEqual([]);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true);
    await page.screenshot({ path: info.outputPath(`${vendor}-thinking.png`), fullPage: true });
    await page.reload();
    await openThinking();
  });
}


test("expanded review queue can scroll to its last action without clipping", async ({page, request}, info) => {
  const origin = `http://127.0.0.1:${new URL(String(info.project.use.baseURL)).port}`;
  const pairing = await (await request.post(`${origin}/api/v1/auth/pairings`, {headers: {Authorization: "Bearer model-test-token"}, data: {name: "Queue scroll test"}})).json();
  await page.goto(`/#pair=${encodeURIComponent(pairing.secret)}&code=${encodeURIComponent(pairing.confirmation_code)}`);
  await page.getByLabel("Device name").fill("Queue scroll test");
  await page.getByRole("button", {name: "Pair device", exact: true}).click();
  await expect(page.getByRole("button", {name: /Nebula Core (ready|degraded)/})).toBeVisible({timeout: 20000});
  await page.goto("/projects/thinking-project/workbench?view=chat&session=queue-chat");
  const queue = page.getByRole("region", {name: "Core follow-up queue"});
  const details = queue.locator("details");
  await expect(details).toHaveAttribute("open", "");
  await details.scrollIntoViewIfNeeded();
  await details.evaluate(element => {element.scrollTop = element.scrollHeight;});
  const remove = queue.getByRole("button", {name: "Remove", exact: true});
  await expect(remove).toBeInViewport({ratio: 1});
  expect(await remove.evaluate(element => {
    const rect = element.getBoundingClientRect();
    return element.contains(document.elementFromPoint(rect.x + rect.width / 2, rect.y + rect.height / 2));
  })).toBe(true);
  await queue.getByRole("button", {name: "Edit queued message 1"}).click();
  await queue.getByLabel("Edit queued text").focus();
  await queue.getByRole("button", {name: "Cancel edit"}).click();
  await expect(queue.getByLabel("Edit queued text")).toHaveCount(0);
  expect((await new AxeBuilder({page}).include(".chat-follow-up-queue").analyze()).violations).toEqual([]);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true);
  await page.screenshot({path: info.outputPath("queue-scroll.png"), fullPage: true});
});
