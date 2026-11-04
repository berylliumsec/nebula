import {expect, test} from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";

test("a saved MCP selection keeps its healthy harness connection across turns and reload", async ({page, request}, info) => {
  const origin = String(info.project.use.baseURL);
  if (process.env.NEBULA_REQUIRE_LAN_TESTS === "1") expect(new URL(origin).hostname).not.toMatch(/^(?:localhost|127\.|\[?::1\]?$)/);
  const headers = {Authorization: "Bearer universal-test-token"};
  const seeded = await request.post(`${origin}/fixture/mcp-seed`, {headers});
  expect(seeded.ok(), await seeded.text()).toBe(true);
  const fixture = await seeded.json();
  const pairing = await (await request.post(`http://127.0.0.1:${new URL(origin).port}/api/v1/auth/pairings`, {headers, data: {name: "MCP continuity test"}})).json();
  await page.goto(`/#pair=${encodeURIComponent(pairing.secret)}&code=${encodeURIComponent(pairing.confirmation_code)}`);
  await page.getByLabel("Device name").fill("MCP continuity test");
  await page.getByRole("button", {name: "Pair device", exact: true}).click();
  await expect(page.getByLabel("Device name")).toBeHidden({timeout: 20000});
  const payload = {
    backend: "harness", engagement_id: fixture.project, session_id: fixture.harness,
    harness_profile_id: "codex", model: "gpt-test", include_knowledge: false, mcp_server_ids: [fixture.mcp],
    allow_agent_messaging: false,
  };
  const baseline = await request.post(`${origin}/api/v1/chat/completions`, {headers, data: {
    ...payload, mcp_server_ids: [], messages: [{role: "user", content: "MCP continuity probe before settings change."}],
  }});
  expect(baseline.ok(), await baseline.text()).toBe(true);
  const first = await request.post(`${origin}/api/v1/chat/completions`, {headers, data: {
    ...payload, messages: [{role: "user", content: "MCP continuity probe first."}],
  }});
  expect(first.ok(), await first.text()).toBe(true);
  const initial = await first.json();
  const second = await request.post(`${origin}/api/v1/chat/completions`, {headers, data: {
    ...payload, messages: [{role: "user", content: "MCP continuity probe next."}],
  }});
  expect(second.ok(), await second.text()).toBe(true);
  expect((await second.json()).harness_session_id).toBe(initial.harness_session_id);
  const evidence = await (await request.get(`${origin}/fixture/evidence/${fixture.harness}`, {headers})).json();
  expect(evidence.harness).toEqual({
    session_id: initial.harness_session_id, mcp_server_ids: [fixture.mcp], catalog_ids: [], connection_opens: 1,
  });
  await page.goto(`/projects/${fixture.project}/workbench?view=chat&session=${fixture.harness}`);
  await expect(page.locator(".chat-message.assistant")).toHaveCount(3);
  await expect(page.locator(".chat-message.assistant").last()).toContainText("MCP continuity confirmed.");
  await page.reload();
  await expect(page.locator(".chat-message.assistant")).toHaveCount(3);
  await info.attach("mcp-continuity-build", {body: JSON.stringify({origin, bundleSha256: fixture.bundle_sha256, profile: info.project.name, evidence}), contentType: "application/json"});
});

test("all parent and child runtimes retain lifecycle guards and saved choices", async ({page, request}, info) => {
  const origin = String(info.project.use.baseURL);
  const headers = {Authorization: "Bearer universal-test-token"};
  const response = await request.post(`${origin}/fixture/seed`, {headers});
  expect(response.ok(), await response.text()).toBe(true);
  const fixture = await response.json();
  const pairing = await (await request.post(`http://127.0.0.1:${new URL(origin).port}/api/v1/auth/pairings`, {headers, data: {name: "Mixed agent test"}})).json();
  await page.goto(`/#pair=${encodeURIComponent(pairing.secret)}&code=${encodeURIComponent(pairing.confirmation_code)}`);
  await page.getByLabel("Device name").fill("Mixed agent test");
  await page.getByRole("button", {name: "Pair device", exact: true}).click();
  await expect(page.getByLabel("Device name")).toBeHidden({timeout: 20000});
  for (const parent of ["provider", "harness"] as const) {
    const identity = fixture[parent];
    await page.goto(`/projects/${fixture.project}/workbench?view=chat&session=${identity}`);
    const trigger = page.getByRole("button", {name: "Assistant settings", exact: true});
    await expect(trigger).toContainText(parent === "provider" ? "Synthetic provider" : "Synthetic harness", {timeout: 20000});
    for (const child of ["provider", "harness"] as const) {
      if (!info.project.use.hasTouch) await page.locator(".chat-composer").hover();
      await trigger.click();
      const settings = page.getByRole("dialog", {name: "Assistant settings"});
      const guard = settings.getByRole("checkbox", {name: /^guard/});
      await guard.check();
      await expect(guard).toBeEnabled();
      await settings.getByRole("checkbox", {name: /^Subagents/}).check();
      const runtime = settings.getByRole("combobox", {name: "Subagent runtime", exact: true});
      await expect(runtime).toBeEnabled();
      await runtime.selectOption(child === "provider" ? "provider:provider" : "harness:codex");
      await expect(runtime).toBeEnabled();
      await expect(settings.getByRole("combobox", {name: "Subagent model", exact: true})).toHaveValue(child === "provider" ? "model-a" : "gpt-test");
      expect((await new AxeBuilder({page}).include("[role=dialog]").analyze()).violations).toEqual([]);
      await page.keyboard.press("Escape");
      await page.reload();
      await expect(trigger).toContainText(parent === "provider" ? "Synthetic provider" : "Synthetic harness", {timeout: 20000});
      if (!info.project.use.hasTouch) await page.locator(".chat-composer").hover();
      await trigger.click();
      await expect(settings.getByRole("checkbox", {name: /^guard/})).toBeChecked();
      await expect(runtime).toHaveValue(child === "provider" ? "provider:provider" : "harness:codex");
      await page.keyboard.press("Escape");
      const composer = page.locator(".chat-composer textarea");
      await composer.fill(`Run the guarded ${parent} to ${child} task.`);
      await page.getByRole("button", {name: "Send message", exact: true}).click();
      await expect(page.locator(".chat-message.assistant").last()).toContainText("Guarded child finished.", {timeout: 45000});
      await expect.poll(async () => {
        const evidence = await (await request.get(`${origin}/fixture/evidence/${identity}`, {headers})).json();
        return evidence.children.filter((item: {backend: string; status: string}) => item.backend === child && item.status === "completed").length;
      }, {timeout: 30000}).toBe(1);
      const evidence = await (await request.get(`${origin}/fixture/evidence/${identity}`, {headers})).json();
      const record = evidence.children.find((item: {backend: string}) => item.backend === child);
      expect(record.hooks).toEqual(expect.arrayContaining([{event: "chat.turn.started", status: "complete"}, {event: "chat.turn.completed", status: "complete"}]));
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true);
    }
  }
  await info.attach("candidate-build", {body: JSON.stringify({origin, bundleSha256: fixture.bundle_sha256, project: info.project.name, viewport: page.viewportSize()}), contentType: "application/json"});
  await page.screenshot({path: info.outputPath("mixed-agent-guards.png")});
});
