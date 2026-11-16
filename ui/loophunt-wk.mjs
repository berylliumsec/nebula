import { chromium, webkit, devices } from "playwright";
import { execSync } from "node:child_process";
import { readFileSync } from "node:fs";
const base = "http://127.0.0.1:18100";
const token = readFileSync(process.env.HOME + "/nebula-staging/data/runtime/operator-token", "utf8").trim();
const engine = process.argv[2];
const browser = await (engine === "webkit" ? webkit : chromium).launch();
const ctx = await browser.newContext(engine === "webkit" ? devices["iPhone 13"] : { viewport: { width: 1440, height: 900 } });
const page = await ctx.newPage();
const t0 = Date.now(); const ts = () => ((Date.now() - t0) / 1000).toFixed(1);
page.on("requestfailed", r => { if (/\.(js|css)/.test(r.url())) console.log(ts(), "FAILED", r.url().replace(base, ""), r.failure()?.errorText); });
page.on("response", r => { if (/\.(js|css)/.test(r.url()) && r.status() >= 400) console.log(ts(), "HTTP", r.status(), r.url().replace(base, "")); });
page.on("pageerror", e => console.log(ts(), "pageerror", e.message.slice(0, 200)));
await page.goto(`${base}/#token=${token}`);
await page.waitForFunction(() => document.querySelector("nav"), null, { timeout: 60000 });
console.log(ts(), "open", page.url());
execSync("systemctl --user restart nebula-staging.service");
console.log(ts(), "restarted");
let last = "";
while (Date.now() - t0 < 80000) {
  const h = (await page.locator("h1").allTextContents().catch(() => [])).join("|");
  if (h !== last) { console.log(ts(), "h1:", h, page.url().replace(base, "")); last = h; }
  await page.waitForTimeout(200);
}
await browser.close();
