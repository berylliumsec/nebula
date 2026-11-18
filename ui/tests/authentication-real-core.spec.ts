import {test, expect} from "./fixtures/journey-test";
import {writeFile} from "node:fs/promises";
import {startApprovalCore} from "./fixtures/approval-core";
import {localNetworkIpv4} from "./fixtures/real-core";
import {pairRealCoreBrowser, openProjectSwitcherFromShell} from "./fixtures/operator-ui";

// Every case has an independent database, port and browser context.
test.describe.configure({mode: "parallel"});

for (const credential of ["missing", "invalid"] as const) {
  test(`assistant upgrade authentication recovery rejects ${credential} token and restores protected data`, async ({page, resources}, testInfo) => {
    test.setTimeout(90_000);
    page.setDefaultTimeout(15_000);
    // Reuse the owned single-process Core fixture; no inert harness turn is submitted.
    const fixture = resources.own("authentication Core", await startApprovalCore(localNetworkIpv4(), "single"), value => value.stop(), value => value.logs().replaceAll("stabilization-fixture", "[redacted]"));
    const core = {origin: fixture.origin, token: "stabilization-fixture"};
    // The administrator seeds real Core data; the browser never shares this context.
    const admin = fixture.api;
    const projectName = "Private authentication fixture 742";
    const privateDescription = "Protected project content 968: visible only after authentication.";
    const invalidToken = "deliberately-invalid-browser-token";
    {
      const created = await admin.post("engagements", {data: {
        name: projectName, description: privateDescription, status: "active",
        client_name: null, tags: [], workspace_path: null, metadata: {},
      }});
      expect(created.ok(), await created.text()).toBe(true);
      const project = await created.json() as {id: string};
      const route = `${core.origin}/projects/${project.id}/workbench?view=chat`;
      const noAccess = page.getByRole("alert").filter({hasText: /Pair this (browser|phone) to continue/}).first();
      const deniedReads: Array<{status: number; body: string}> = [];

      await test.step("Reject the credential and explain the failure without exposing project data", async () => {
        expect((await page.context().cookies()).filter(cookie => cookie.name === "nebula_device")).toHaveLength(0);
        const deniedHealth = page.waitForResponse(response => new URL(response.url()).pathname === "/api/v1/health" && response.status() === 401);
        await page.goto(`${route}${credential === "invalid" ? `#token=${invalidToken}` : ""}`);
        const health = await deniedHealth;
        expect(health.request().headers().authorization).toBe(credential === "invalid" ? `Bearer ${invalidToken}` : undefined);
        if (credential === "missing") await expect(noAccess).toBeVisible();
        else await expect(page.getByRole("alert").filter({hasText: "valid bearer token required"}).first()).toBeVisible();
        await expect(page).not.toHaveURL(/#token=/);
        await expect(page.getByText(projectName, {exact: true})).toHaveCount(0);
        await expect(page.getByText(privateDescription, {exact: true})).toHaveCount(0);

        // Exercise both collection and direct-object reads from the browser's
        // own origin/cookie jar. A hidden screen alone cannot prove access control.
        const results = await page.evaluate(async ({id, token}) => {
          const headers: Record<string, string> = token ? {Authorization: `Bearer ${token}`} : {};
          return Promise.all(["engagements", `engagements/${id}`].map(async resource => {
            const response = await fetch(`/api/v1/${resource}`, {credentials: "same-origin", headers});
            return {status: response.status, body: await response.text()};
          }));
        }, {id: project.id, token: credential === "invalid" ? invalidToken : ""});
        deniedReads.push(...results);
        for (const result of results) {
          expect(result.status).toBe(401);
          expect(JSON.parse(result.body).detail).toBe("valid bearer token required");
          expect(result.body).not.toContain(projectName);
          expect(result.body).not.toContain(privateDescription);
        }
      });

      if (credential === "invalid") {
        await test.step("Correct the launch token in the same browser and read the protected project", async () => {
          // A fresh launch document consumes the corrected token into memory.
          // Merely changing the hash does not rerun runtime resolution.
          const acceptedHealth = page.waitForResponse(response => new URL(response.url()).pathname === "/api/v1/health" && response.ok());
          await page.goto(`${route}&auth-recovery=1#token=${encodeURIComponent(core.token)}`);
          await acceptedHealth;
          await expect(page).not.toHaveURL(/#token=/);
          const switcher = await openProjectSwitcherFromShell(page);
          await expect(switcher.getByRole("button", {name: `${projectName} active`, exact: true})).toBeVisible();
          await switcher.getByRole("button", {name: `${projectName} active`, exact: true}).click();
          await expect(page.getByRole("alert").filter({hasText: "valid bearer token required"})).toHaveCount(0);
          // Launch tokens deliberately live only in memory. Reload must fail
          // closed; pairing below supplies the supported durable browser session.
          await page.reload();
          await expect(noAccess).toBeVisible();
        });
      }

      await test.step("Pair after the failure and retain authorized access after reload", async () => {
        await pairRealCoreBrowser(page, core, `Recovered ${credential} credential browser`);
        await page.goto(route);
        const switcher = await openProjectSwitcherFromShell(page);
        await expect(switcher.getByRole("button", {name: `${projectName} active`, exact: true})).toBeVisible();
        await switcher.getByRole("button", {name: `${projectName} active`, exact: true}).click();
        await page.reload();
        await expect(page.locator(".app-shell")).toBeVisible();
        await expect(noAccess).toHaveCount(0);
        const recovered = await page.evaluate(async id => {
          const response = await fetch(`/api/v1/engagements/${id}`, {credentials: "same-origin"});
          return {status: response.status, body: await response.json()};
        }, project.id);
        expect(recovered.status).toBe(200);
        expect(recovered.body).toMatchObject({id: project.id, name: projectName, description: privateDescription});
        const restoredSwitcher = await openProjectSwitcherFromShell(page);
        await expect(restoredSwitcher.getByRole("button", {name: `${projectName} active`, exact: true})).toBeVisible();
        const buildResponse = await page.request.get(`${core.origin}/web-build.json`);
        expect(buildResponse.ok()).toBe(true);
        const receipt = {origin: core.origin, credential, deniedStatuses: deniedReads.map(result => result.status), recoveredStatus: recovered.status, build: await buildResponse.json()};
        await writeFile(testInfo.outputPath("authentication-recovery.json"), JSON.stringify(receipt, null, 2));
        await testInfo.attach("authentication-recovery", {body: JSON.stringify(receipt), contentType: "application/json"});
        await testInfo.attach("recovered-project", {body: await page.screenshot(), contentType: "image/png"});
      });
    }
  });
}
