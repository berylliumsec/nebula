import {request as unkeyedRequest} from "@playwright/test";
import {test, expect} from "./fixtures/journey-test";
import {readFile, writeFile} from "node:fs/promises";
import path from "node:path";
import {keyedCoreRequests} from "./fixtures/core-harness";
import {startRealCore, stopRealCore, localNetworkIpv4} from "./fixtures/real-core";
import {startLocalModelStub, stopLocalModelStub} from "./fixtures/model-stub";
import {clickAssistantSettings, pairRealCoreBrowser, clickWorkbenchTab, openRuntimeSettings, navigateOperatorPage} from "./fixtures/operator-ui";
const playwrightRequest = keyedCoreRequests(unkeyedRequest);

// Every case has an independent database, port and browser context.
test.describe.configure({mode: "parallel"});

for (const runtime of ["provider", "harness"] as const) {
  test(`stabilization real Core ${runtime} settings separate saving from health recovery`, async ({page, resources}, testInfo) => {
    test.setTimeout(100_000);
    page.setDefaultTimeout(10_000);
    // Two discovered models keep the saved default distinguishable from the first one.
    const stub = runtime === "provider" ? await startLocalModelStub({models: ["security-model", "security-model-reasoning"]}) : undefined;
    if (stub) resources.own("model service", stub, stopLocalModelStub);
    const core = await startRealCore({bindHost: "0.0.0.0", browserHost: localNetworkIpv4()});
    resources.own("Core", core, stopRealCore, value => value.logs?.() ?? "Core logs unavailable.");
    const api = await playwrightRequest.newContext({baseURL: `${core.origin}/api/v1/`, extraHTTPHeaders: {Authorization: `Bearer ${core.token}`}});
    resources.own("API context", api, value => value.dispose());
    const name = `Local ${runtime} acceptance`;
    const collection = runtime === "harness" ? "harnesses" : "providers";
    {
      const fixture = path.join(core.dataDir, "stabilization_acp.py");
      if (runtime === "harness") await writeFile(fixture, await readFile(path.resolve(import.meta.dirname, "../../scripts/fixtures/stabilization_acp.py")), {mode: 0o700});
      // Chat requires a selected project. The optional bootstrap Scratch project
      // may not exist, so this case owns and selects its own project explicitly.
      const created = await api.post("engagements", {data: {
        name: `${name} project`, status: "active", client_name: null,
        tags: [], workspace_path: null, metadata: {},
      }});
      expect(created.ok(), await created.text()).toBe(true);
      const project = await created.json() as {id: string};
      await pairRealCoreBrowser(page, core, name);
      await page.goto(`${core.origin}/projects/${project.id}/workbench?view=chat`);
      await expect(page.locator(".connection-chip")).toHaveClass(/\b(ready|degraded)\b/, {timeout: 20_000});
      const settingsEntry = await openRuntimeSettings(page, runtime);
      await page.getByRole("button", {name: runtime === "provider" ? "Add provider" : "Add Grok", exact: true}).click();
      const dialog = runtime === "provider"
        ? page.getByRole("dialog", {name: new RegExp(`^(Add model provider|Edit ${name})$`)})
        : page.getByRole("dialog", {name: /^(Add Grok ACP|Edit harness)$/});
      const profileName = dialog.getByRole("textbox", {name: runtime === "provider" ? "Profile name" : "Name", exact: true});
      if (runtime === "provider") {
        await dialog.getByRole("combobox", {name: "Provider type", exact: true}).selectOption("vllm");
        await dialog.getByRole("textbox", {name: "Endpoint", exact: true}).fill(`${stub!.origin}/v1`);
      } else await dialog.getByRole("textbox", {name: "Absolute Grok executable path", exact: true}).fill(fixture);
      await profileName.fill(name);
      let failedSave = false;
      let failedHealth = false;
      await page.route(`**/api/v1/${collection}`, async route => {
        if (route.request().method() === "POST" && !failedSave) {
          failedSave = true;
          await route.fulfill({status: 503, contentType: "application/json", body: JSON.stringify({detail: "Synthetic profile save failure"})});
        } else await route.continue();
      });
      await page.route(`**/api/v1/${collection}/*/health`, async route => {
        if (!failedHealth) {
          failedHealth = true;
          await route.fulfill({status: 503, contentType: "application/json", body: JSON.stringify({detail: "Synthetic health response failure"})});
        } else await route.continue();
      });
      const save = dialog.getByRole("button", {name: runtime === "provider" ? "Add provider" : "Save harness", exact: true});
      await save.click();
      await expect(dialog).toContainText("Synthetic profile save failure");
      await expect(profileName).toHaveValue(name);
      const persisted = page.waitForResponse(r => r.request().method() === "POST" && new URL(r.url()).pathname.endsWith(`/api/v1/${collection}`));
      await save.click();
      const saved = await (await persisted).json();
      // Capability verification re-reads the saved profile once the chat selects a model.
      const profileReads: string[] = [];
      page.on("response", response => {
        if (response.request().method() === "GET" && new URL(response.url()).pathname.endsWith(`/api/v1/${collection}/${saved.id}`)) profileReads.push(response.url());
      });
      await expect.poll(() => failedHealth).toBe(true);
      await expect(dialog).toHaveCount(0);
      const card = page.locator("article.provider-card").filter({has: page.getByRole("heading", {name, exact: true})});
      await expect(card).toBeVisible();
      if ((page.viewportSize()?.width ?? 1440) <= 430) {
        for (const button of await card.getByRole("button").all()) {
          const box = await button.boundingBox();
          if (box) {expect(box.width).toBeGreaterThanOrEqual(44); expect(box.height).toBeGreaterThanOrEqual(44);}
        }
      }
      const check = card.getByRole("button", {name: runtime === "provider" ? `Refresh ${name} health` : "Check", exact: true});
      await check.click();
      await expect(card.locator(runtime === "provider" ? ".health-label" : ".status-dot")).toHaveClass(/healthy/);
      const profiles = await (await api.get(collection)).json();
      expect(profiles.filter((p: {name: string}) => p.name === name)).toHaveLength(1);
      expect(profiles.find((p: {name: string}) => p.name === name).id).toBe(saved.id);
      await card.getByRole("button", {name: `Edit ${name}`, exact: true}).click();
      const models = dialog.getByRole("combobox", {name: "Default model", exact: true});
      await expect.poll(() => models.locator("option").evaluateAll(options => options.filter(option => (option as HTMLOptionElement).value).length)).toBeGreaterThan(0);
      const available = (await models.locator("option").evaluateAll(options => options.map(o => (o as HTMLOptionElement).value).filter(Boolean)))
        .filter(value => value !== "grok-build");
      // The last discovered model proves the saved default wins over the runtime's first one.
      const model = available.at(-1);
      expect(model, "Health must expose the fixture's discovered model").toBeTruthy();
      if (runtime === "provider") expect(available, "Discovery must offer a model the operator would not get by default").toHaveLength(2);
      await models.selectOption(model!);
      await dialog.getByRole("button", {name: runtime === "provider" ? "Save provider" : "Save harness", exact: true}).click();
      await expect(dialog).toHaveCount(0);
      await page.reload();
      // The settings list renders before workspace bootstrap finishes. Its
      // loading banner can move the entry during a mobile click.
      await expect(page.locator(".connection-chip")).toHaveClass(/\b(ready|degraded)\b/, {timeout: 20_000});
      await expect(settingsEntry.or(card).filter({visible: true}).first()).toBeVisible();
      if (await settingsEntry.isVisible()) await settingsEntry.click();
      await expect(card).toBeVisible();
      await card.getByRole("button", {name: `Edit ${name}`, exact: true}).click();
      await expect(models).toHaveValue(model!);
      await dialog.getByRole("button", {name: runtime === "provider" ? "Close provider dialog" : "Close harness dialog", exact: true}).click();
      await card.getByRole("button", {name: runtime === "provider" ? `Disable ${name}` : "Disable", exact: true}).click();
      await expect(card.getByRole("button", {name: runtime === "provider" ? `Enable ${name}` : "Enable", exact: true})).toBeVisible();
      await card.getByRole("button", {name: runtime === "provider" ? `Enable ${name}` : "Enable", exact: true}).click();
      await expect(card.getByRole("button", {name: runtime === "provider" ? `Disable ${name}` : "Disable", exact: true})).toBeVisible();
      const durable = (await (await api.get(collection)).json()).find((p: {id: string}) => p.id === saved.id);
      expect(durable.enabled).toBe(true);
      expect(runtime === "provider" ? durable.metadata.default_model : durable.default_model).toBe(model);
      await testInfo.attach("runtime-settings-durable", {body: JSON.stringify({origin: core.origin, runtime, model, durable}), contentType: "application/json"});
      await testInfo.attach("runtime-settings-saved", {body: await page.screenshot(), contentType: "image/png"});
      await navigateOperatorPage(page, "Workbench");
      await expect(page).toHaveURL(new RegExp(`/projects/${project.id}/workbench(?:\\?|$)`));
      if ((page.viewportSize()?.width ?? 1440) > 760) await clickWorkbenchTab(page, "Analyst chat");
      else await page.getByRole("navigation", {name: "Mobile operator navigation"}).getByRole("button", {name: "Chat", exact: true}).click();
      await page.getByRole("button", {name: "New chat", exact: true}).click();
      await clickAssistantSettings(page);
      await page.getByRole("combobox", {name: "Chat runtime", exact: true}).selectOption(runtime);
      await page.getByRole("combobox", {name: runtime === "provider" ? "Chat provider" : "Chat harness", exact: true}).selectOption(saved.id);
      const chatModel = page.getByRole("combobox", {name: runtime === "provider" ? "Chat model" : "Chat harness model", exact: true});
      await expect(chatModel).toHaveValue(model!);
      if (runtime === "provider") {
        // A profile read carries the allowed models only; it must not empty the discovered picker.
        await expect.poll(() => profileReads.length, {timeout: 20_000}).toBeGreaterThan(0);
        await expect(chatModel).toBeEnabled();
        await expect(chatModel).toHaveValue(model!);
        expect((await chatModel.locator("option").evaluateAll(options => options.map(o => (o as HTMLOptionElement).value).filter(Boolean))).sort())
          .toEqual([...available].sort());
      }
      await page.getByRole("button", {name: "Close assistant settings", exact: true}).click();
      await page.getByRole("textbox", {name: "Message the analyst assistant", exact: true}).fill("Disposable unsent setup acceptance draft. No tool runs.", {timeout: 10_000});
      await expect(page.getByRole("button", {name: "Send message", exact: true})).toBeEnabled();
      await testInfo.attach("runtime-settings-selectable", {body: await page.screenshot(), contentType: "image/png"});
      if (stub) {
        // Selecting a provider may negotiate its existing inert nonce capability
        // probe. No conversation draft or executable tool may be submitted.
        expect(stub.requests.length).toBeLessThanOrEqual(1);
        for (const request of stub.requests) {
          expect(request.tools).toEqual([expect.objectContaining({function: expect.objectContaining({name: "nebula_capability_probe"})})]);
          // Documented allowance for an unknown-limit profile; operator/model
          // caps remain covered by test_inference_timeouts_probe_budget.py.
          expect(request.max_tokens).toBe(2_048);
          expect(JSON.stringify(request.messages)).not.toContain("Disposable unsent");
        }
      }
    }
  });
}
